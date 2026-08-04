"""Scenario model, filter, and builder for the claim-pipeline integration suite.

The system under test is the four-hop journey a grader's verdict makes:
`reconcile_verdicts` → `design_lens` → `emit_claim_graph` → a claim node in the
knowledge graph. Every stage reads the previous stage's JSON off disk, so this
is a real serialization boundary and nothing here is mocked.

The dimensions below are the ones that genuinely vary a run. They were chosen
because each independently changes what the final node carries:

- `Verdict` — what the two graders cast, including the withdrawn legacy label
  the reconciler still folds.
- `Bound` — whether a verified `role: "limits"` citation exists. Without one a
  narrowing is an unsupported opinion and is dropped.
- `Narrowing` — whether the grader wrote the `asSupported` half of a scope note.
- `Adjudication` — whether a tie-break vote ran, and whether it agreed with
  consensus or overturned it. The overturn arm is the one that historically
  resurrected a stale scope note onto a `contradicted` claim.
- `Linkage` — whether the graph carries a node for the claim's qualname or only
  for its containing file, which decides the edge the emitter can draw.

Docstrings here are plain reST: the project test guide scopes Given-When-Then to
test functions and methods, never to fixtures or helpers.
"""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, fields
from enum import Enum, auto
from pathlib import Path

import pytest

# Reuse the unit suite's corpus builders rather than reimplementing them. They
# already encode the exact on-disk shapes each stage expects, and a second
# hand-rolled copy would drift from the real artifacts — which is the class of
# defect this suite exists to catch.
from tests.skill.docs.conftest import (  # noqa: E402
    CLAIM_ID,
    MIRROR_SOURCE,
    evidence,
    make_bundle,
    make_claim,
    make_claims_file,
    make_doc_units,
    make_graph,
    make_pass,
    reconciled_claim,
    verdict_entry,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "understand-anything-plugin" / "skills" / "understand-docs"

# Line numbers into MIRROR_SOURCE. Load-bearing: `verify_evidence` re-reads
# these exact lines, so a citation that names the wrong ones is rejected.
SUPPORT_LINES = [6, 7]
SUPPORT_CODE = "if count == 0:\n            count = cpu_count()"
LIMIT_LINES = [8]
LIMIT_CODE = "return [Worker() for _ in range(count)]"

QUALNAME = "Pool.spawn"
SOURCE_FILE = "pkg/pool.py"
NARROWED = "spawns the requested quantity unless it is zero"


class Verdict(Enum):
    """What both graders cast on the claim."""

    SUPPORTED = auto()
    CONTRADICTED = auto()
    UNVERIFIABLE = auto()
    LEGACY_OVERSTATED = auto()


class Bound(Enum):
    """Whether a verified limiting citation accompanies the supporting one."""

    PRESENT = auto()
    ABSENT = auto()


class Narrowing(Enum):
    """Whether the grader wrote the `asSupported` half of a scope note."""

    PRESENT = auto()
    ABSENT = auto()


class Adjudication(Enum):
    """Whether a tie-break vote ran, and what it decided."""

    NONE = auto()
    AGREES = auto()
    OVERTURNS = auto()


class Linkage(Enum):
    """What the knowledge graph offers the emitter to attach the claim to."""

    SYMBOL = auto()
    CONTAINING_FILE = auto()


@dataclass(frozen=True)
class Scenario:
    """One point in the pipeline's configuration space."""

    verdict: Verdict | None = None
    bound: Bound | None = None
    narrowing: Narrowing | None = None
    adjudication: Adjudication | None = None
    linkage: Linkage | None = None

    def __or__(self, other: Scenario) -> Scenario:
        """Merge two partial scenarios; raise on conflicting non-None fields."""
        merged = {}
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            if mine is not None and theirs is not None and mine != theirs:
                raise ValueError(
                    f"conflicting values for {f.name}: {mine} vs {theirs}")
            merged[f.name] = mine if mine is not None else theirs
        return Scenario(**merged)

    @property
    def is_complete(self) -> bool:
        """True when every dimension is set."""
        return all(getattr(self, f.name) is not None for f in fields(self))

    def __str__(self) -> str:
        return "-".join(
            getattr(self, f.name).name if getattr(self, f.name) else "ANY"
            for f in fields(self))


def is_valid(scenario_values) -> bool:
    """Reject structurally impossible dimension combinations.

    `allpairspy` calls this with a growing prefix of the row, so index defensively.

    Only one constraint is structural rather than merely uninteresting: an
    adjudicator cannot *overturn* a claim to `contradicted` when the consensus
    already reads `contradicted` — that is agreement, not an overturn, and the
    scenario would silently duplicate the AGREES arm.
    """
    n = len(scenario_values)
    if n >= 4:
        verdict, adjudication = scenario_values[0], scenario_values[3]
        if (adjudication is Adjudication.OVERTURNS
                and verdict is Verdict.CONTRADICTED):
            return False
    return True


# ── the oracle ───────────────────────────────────────────────────────────────
#
# Derived from the taxonomy, NOT from the implementation. Recomputing what the
# code does would make every assertion a tautology — the mistake the strip()
# property test made.

def expected_verdict(s: Scenario) -> str:
    """The verdict the final claim node should carry."""
    if s.adjudication is Adjudication.OVERTURNS:
        return "contradicted"
    if s.verdict is Verdict.LEGACY_OVERSTATED:
        # Folds to `supported`: the claim is true, only the label was withdrawn.
        # Evidence always verifies in this suite, so the unverifiable arm of the
        # ladder cannot fire here.
        return "supported"
    return {Verdict.SUPPORTED: "supported",
            Verdict.CONTRADICTED: "contradicted",
            Verdict.UNVERIFIABLE: "unverifiable"}[s.verdict]


def expects_scope_note(s: Scenario) -> bool:
    """Whether the final node should carry a scope note.

    Three conditions, all necessary: the claim must still hold, the grader must
    have written the narrowing, and a limiting citation must back it.
    """
    return (expected_verdict(s) == "supported"
            and s.narrowing is Narrowing.PRESENT
            and s.bound is Bound.PRESENT)


# ── corpus construction ──────────────────────────────────────────────────────

VERDICT_WIRE = {
    Verdict.SUPPORTED: "supported",
    Verdict.CONTRADICTED: "contradicted",
    Verdict.UNVERIFIABLE: "unverifiable",
    Verdict.LEGACY_OVERSTATED: "overstated",
}


def _vote(verdict: str, s: Scenario) -> dict:
    ev = [evidence(SUPPORT_LINES, SUPPORT_CODE)]
    if s.bound is Bound.PRESENT:
        ev.append(evidence(LIMIT_LINES, LIMIT_CODE, role="limits"))
    return verdict_entry(
        "c1", verdict, ev,
        scope_note={"asSupported": NARROWED}
        if s.narrowing is Narrowing.PRESENT else None,
    )


@contextmanager
def build_from_scenario(scenario: Scenario, tmp_path: Path, modules):
    """Resolve every dimension to a concrete corpus and run the three stages.

    Yields the emitted knowledge graph. Each stage reads the previous stage's
    artifact from disk, so the seams under test are real.
    """
    assert scenario.is_complete, f"incomplete scenario: {scenario}"
    reconcile, lens, emit = modules

    mirror = tmp_path / "mirror"
    (mirror / "pkg").mkdir(parents=True)
    (mirror / SOURCE_FILE).write_text(MIRROR_SOURCE)

    wire = VERDICT_WIRE[scenario.verdict]
    make_bundle(tmp_path / "bundles", "b1",
                [make_claim("c1", "Pool.spawn spawns workers.",
                            quantifier="universal")],
                source_file=SOURCE_FILE, qualname=QUALNAME)
    passes = [make_pass(tmp_path / "verdicts", f"b1-pass{i}",
                        [_vote(wire, scenario)]) for i in (1, 2)]

    argv = ["reconcile_verdicts.py", *map(str, passes),
            "--bundles", str(tmp_path / "bundles"), "--mirror", str(mirror),
            "--out", str(tmp_path / "grounded"), "--label", "t"]
    if scenario.adjudication is not Adjudication.NONE:
        decision = ("contradicted"
                    if scenario.adjudication is Adjudication.OVERTURNS else wire)
        tb = write_json(tmp_path / "tiebreak.json",
                        {"verdicts": [_vote(decision, scenario)]})
        argv += ["--tiebreak", str(tb)]

    _run(reconcile, argv)

    claims = make_claims_file(tmp_path / "claims-t.json",
                              [reconciled_claim(cid=CLAIM_ID)])
    _run(lens, ["design_lens.py", str(claims),
                "--grounded", str(tmp_path / "grounded" / "grounded-t.json"),
                "--out", str(tmp_path / "lens")])

    graph = make_graph(tmp_path / "graph.json")
    if scenario.linkage is Linkage.SYMBOL:
        payload = json.loads(graph.read_text())
        payload["nodes"].append({
            "id": f"function:{SOURCE_FILE}:{QUALNAME}", "type": "function",
            "name": QUALNAME, "filePath": SOURCE_FILE, "summary": "spawn",
            "tags": [], "complexity": "simple"})
        write_json(graph, payload)

    out = tmp_path / "kg.json"
    _run(emit, ["emit_claim_graph.py", "--graph", str(graph),
                "--design-claims", str(tmp_path / "lens" / "design-claims.json"),
                "--doc-units", str(make_doc_units(tmp_path / "doc-units.json")),
                "--out", str(out)])

    yield json.loads(out.read_text())


def _run(module, argv):
    """Invoke a stage's main() with the given argv, asserting a clean exit."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sys, "argv", argv)
        assert module.main() == 0, f"{argv[0]} exited non-zero"


def claim_node(graph: dict) -> dict | None:
    """The single claim node the chain should have produced, if any."""
    return next((n for n in graph["nodes"]
                 if n.get("type") == "claim" and n["id"] == CLAIM_ID), None)


@pytest.fixture(scope="session")
def pipeline_modules():
    """The three stage modules, loaded once from the skill directory."""
    import importlib.util

    mods = []
    for name in ("reconcile_verdicts", "design_lens", "emit_claim_graph"):
        spec = importlib.util.spec_from_file_location(name, SKILL / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods.append(mod)
    return tuple(mods)
