#!/usr/bin/env python3
"""check_prompts.py — guard the invariants the agent prompts rely on.

Not a step of the `understand-docs` pipeline: a maintainer check, run by the
test suite.

Run: python3 check_prompts.py          check
     python3 check_prompts.py --sync   rewrite the shared block, then check

These are not style checks. Each one guards a failure that has actually cost
something, or that would be invisible if it happened.

`--sync` copies `prompts/_taxonomy.md` into the marked region of both grounding
prompts, `ground-claims.md` and `tiebreak-verdicts.md`.
The block was kept identical by hand-pasting, which is exactly the thing that
drifts — and a drifted taxonomy means the adjudicator applies a different rule
than the passes it adjudicates, on the claims that were hardest to judge.

The step-13 checks tie `prompts/design-edges.md` to the code on both sides of
it, and test behaviour rather than match text. The edge types the prompt's
table offers must be exactly the keys of `EDGE_MAP` in the imported emitter.
The prompt's own `## Output` example is then run through the emitter's
`read_design_edges` inside a dict that logs every key read from it: every key
the emitter reads must be one the example shows, every edge and principle of
the example must be accepted, and probe edges of a type the prompt does not
offer must be dropped. How the emitter spells a read does not matter, since
only the reads it performs are logged. On the input side, the claim fields the
prompt lists must be exactly `BUNDLE_FIELDS` in `design_lens.py`, which writes
them into each bundle.
"""

from __future__ import annotations

import collections
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
PROMPTS = HERE / "prompts"

BEGIN = "<!-- BEGIN SHARED TAXONOMY -->"
END = "<!-- END SHARED TAXONOMY -->"
CONSUMERS = ("ground-claims.md", "tiebreak-verdicts.md")

FAILURES: list[str] = []


def sync() -> int:
    taxonomy = (PROMPTS / "_taxonomy.md").read_text().rstrip("\n")
    for name in CONSUMERS:
        path = PROMPTS / name
        text = path.read_text()
        if BEGIN not in text or END not in text:
            print(f"  FAIL  {name} has no {BEGIN} / {END} markers")
            return 1
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        new = f"{head}{BEGIN}\n{taxonomy}\n{END}{tail}"
        if new == text:
            print(f"  ok    {name} already in sync")
        else:
            path.write_text(new)
            print(f"  SYNC  {name} taxonomy block rewritten")
    return 0


def load_script(path: Path):
    """The module at `path`, imported under a private name."""
    spec = importlib.util.spec_from_file_location(f"_check_prompts_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder(dict):
    """A dict that logs every key read from it, and from every dict inside it."""

    def __init__(self, data: dict, log: set):
        super().__init__({k: record(v, log) for k, v in data.items()})
        self._log = log

    def __getitem__(self, key):
        self._log.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self._log.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self._log.add(key)
        return super().__contains__(key)


def record(value, log: set):
    if isinstance(value, dict):
        return Recorder(value, log)
    if isinstance(value, list):
        return [record(v, log) for v in value]
    return value


class HoldsEverything(dict):
    """A claim index that holds every id, as itself."""

    def __contains__(self, key):
        return isinstance(key, str)

    def __getitem__(self, key):
        return key

    def get(self, key, default=None):
        return key if isinstance(key, str) else default


def keys_in(value) -> set:
    """Every object key anywhere in a JSON value."""
    if isinstance(value, dict):
        return set(value) | set().union(*(keys_in(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_in(v) for v in value))
    return set()


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


def main() -> int:
    if "--sync" in sys.argv:
        print("syncing shared taxonomy block:")
        if sync() != 0:
            return 1
        print()

    taxonomy = (PROMPTS / "_taxonomy.md").read_text()
    ground = (PROMPTS / "ground-claims.md").read_text()
    tiebreak = (PROMPTS / "tiebreak-verdicts.md").read_text()
    reconcile = (HERE / "reconcile_verdicts.py").read_text()
    edges_prompt = (PROMPTS / "design-edges.md").read_text()
    extract = (PROMPTS / "extract-claims.md").read_text()

    print("prompt invariants:")

    # If these drift, the adjudicator applies a different rule than the passes it
    # adjudicates — on precisely the claims that were hardest to judge.
    check("taxonomy block is byte-identical in both prompts",
          taxonomy.rstrip("\n") in ground and taxonomy.rstrip("\n") in tiebreak)
    for name, text in (("ground", ground), ("tiebreak", tiebreak)):
        check(f"{name} prompt carries the sync markers",
              BEGIN in text and END in text)

    # The prompts define the vocabulary the script enforces; a mismatch means
    # agents emit a verdict the reconciler silently rewrites.
    verdicts = re.search(r"^VERDICTS = \((.*?)\)", reconcile, re.M | re.S)
    declared = set(re.findall(r'"(\w+)"', verdicts.group(1))) if verdicts else set()
    check("reconcile_verdicts.VERDICTS == 3 known values",
          declared == {"supported", "contradicted", "unverifiable"},
          str(sorted(declared)))
    for v in sorted(declared):
        check(f"  prompt documents verdict `{v}`", f"`{v}`" in taxonomy)

    # A scope note without a limiting citation is dropped by the reconciler.
    # If the prompt does not say so, agents lose findings without knowing why.
    check("prompt names the `limits` role", '"limits"' in taxonomy or "`limits`" in taxonomy)
    check("prompt warns that a bound-less scope note is dropped",
          "a scope note without a verified limiting citation is dropped" in taxonomy)
    check("BOUND_ROLES in the script matches the prompt",
          'BOUND_ROLES = ("limits", "contradicts")' in reconcile)

    # The fourth verdict was withdrawn after a control arm measured it
    # reproducing at 17/31 (of the claims both passes had graded `overstated`)
    # against `contradicted`'s 14/14. Nothing should
    # reintroduce it, and the taxonomy should say why so the next reader does
    # not re-propose it from first principles.
    check("no verdict named `overstated` survives in the taxonomy",
          "| `overstated`" not in taxonomy)
    check("taxonomy explains why the fourth verdict was withdrawn",
          "withdrawn" in taxonomy and "17 times in 31" in taxonomy)
    check("taxonomy documents scope notes", "scopeNote" in taxonomy)
    for name, text in (("ground", ground), ("tiebreak", tiebreak)):
        check(f"{name} output schema carries scopeNote",
              '"scopeNote"' in text.split("## Output", 1)[-1])
    check("reconcile_verdicts builds scope notes and folds legacy verdicts",
          "def scope_note(" in reconcile and "LEGACY_VERDICTS" in reconcile)

    # Calibration examples move inter-rater agreement more than definitions do.
    # A and E are the pair that matters: same shape, opposite verdict, and the
    # difference is only what a reader does with the sentence.
    for label in ("A —", "B —", "C —", "D —", "E —"):
        check(f"calibration example {label.strip(' —')} present", label in taxonomy)

    # Run-specific counts made the old tie-break prompt single-use.
    hardcoded = re.findall(r"\((\d+) claims\)", tiebreak) + re.findall(r"all (\d+)\b", tiebreak)
    check("tie-break prompt carries no hard-coded run counts", not hardcoded, str(hardcoded))

    # Paths must be injected, or the prompt pins itself to one run's scratch dir.
    for token in ("{MIRROR}", "{INPUT}", "{OUTPUT}"):
        check(f"tie-break prompt uses {token} placeholder", token in tiebreak)
        check(f"ground prompt uses {token} placeholder", token in ground)
    check("no absolute /tmp paths remain in prompts",
          "/tmp/" not in ground and "/tmp/" not in tiebreak)

    # Step 13. The emitter maps each edge type through EDGE_MAP and drops any
    # other, so the prompt's table and the map must be the same set.
    emit = load_script(HERE / "emit_claim_graph.py")
    mapped = set(getattr(emit, "EDGE_MAP", {}))
    part1 = edges_prompt.split("## Part 1", 1)[-1].split("## Part 2", 1)[0]
    offered = set(re.findall(r"^\| `([^`]+)` \|", part1, re.M))
    check("design-edges prompt offers exactly the EDGE_MAP types",
          bool(mapped) and offered == mapped,
          f"prompt only {sorted(offered - mapped)}, EDGE_MAP only {sorted(mapped - offered)}")
    # Run the prompt's own example through the emitter's reader.
    block = re.search(r"```json\n(.*?)\n```", edges_prompt.split("## Output", 1)[-1], re.S)
    try:
        example = json.loads(block.group(1)) if block else None
    except ValueError:
        example = None
    check("design-edges output example is valid JSON", isinstance(example, dict))
    if isinstance(example, dict):
        edges = example.get("edges") or []
        ends = (edges[0].get("from"), edges[0].get("to")) if edges else ("claim:x", "claim:y")
        probes = [{"from": ends[0], "to": ends[1], "type": t, "rationale": "probe"}
                  for t in ("supersedes", "undescribed-type")]
        payload = {**example, "edges": edges + probes,
                   "principles": (example.get("principles") or []) * 2}
        log: set = set()
        principles: dict = {}
        try:
            fault = emit.design_payload_fault(record(payload, log))
            accepted = emit.read_design_edges("example", record(payload, log), HoldsEverything(),
                                              0, principles, collections.Counter(), [])
        except Exception as exc:  # a read of a key the example lacks raises here
            fault, accepted = f"{type(exc).__name__}: {exc}", []
        statements = {p.get("statement") for p in example.get("principles") or []}
        check("emitter accepts every edge and principle of the design-edges example",
              fault is None and len(accepted) == len(edges) and len(principles) == len(statements),
              f"fault {fault!r}; {len(accepted)} of {len(edges)} edges, "
              f"{len(principles)} of {len(statements)} principles")
        types = {e.get("type") for e in edges}
        check("design-edges example uses only the types the prompt offers",
              bool(types) and types <= offered, str(sorted(map(str, types - offered))))
        unshown = sorted(map(str, log - keys_in(example)))
        check("emitter reads only keys the design-edges example shows",
              bool(log) and not unshown, str(unshown))
    # The claim fields the step-13 agent is told it has are the fields
    # design_lens.py writes into each bundle.
    lens = load_script(HERE / "design_lens.py")
    claims_line = re.search(r"^- `claims` — ([^.\n]*)", edges_prompt, re.M)
    listed = set(re.findall(r"`(\w+)`", claims_line.group(1))) if claims_line else set()
    bundled = set(getattr(lens, "BUNDLE_FIELDS", ()))
    check("design-edges prompt lists exactly the claim fields design_lens bundles",
          bool(bundled) and listed == bundled,
          f"prompt only {sorted(listed - bundled)}, BUNDLE_FIELDS only {sorted(bundled - listed)}")
    for token in ("{MIRROR}", "{INPUT}", "{OUTPUT}"):
        check(f"design-edges prompt uses {token} placeholder", token in edges_prompt)
    check("design-edges prompt allows one contradicts edge per pair and no supersedes",
          "one `contradicts` edge per pair" in edges_prompt
          and "never write a `supersedes` edge" in edges_prompt)
    for token in ("{INPUT}", "{OUTPUT}"):
        check(f"extract prompt uses {token} placeholder", token in extract)
    # The bundle label names the files; `subsystem` is the only record of the
    # subsystem a divided subsystem's bundles share, and reconcile_claims.py
    # carries it into the claim set, where design_lens.py and the emitter read it.
    extract_output = extract.split("## Output", 1)[-1]
    for key in ("bundle", "subsystem", "pass", "claims"):
        check(f"extract output schema names `{key}`", f'"{key}"' in extract_output)

    # Nothing addressed to maintainers reaches an agent. An HTML comment is
    # invisible in a rendered view and read in full by an agent given the file.
    for path in sorted(PROMPTS.glob("*.md")):
        if path.name == "_taxonomy.md":
            continue
        text = path.read_text().replace(BEGIN, "").replace(END, "")
        check(f"{path.name} carries no HTML comment", "<!--" not in text)

    # SKILL.md places each script by step number. A script that names a stage, a
    # phase, or the plan document the pipeline was designed in sends an agent
    # resolving a question to terms that map to nothing it has been given.
    # Matched over the whole text, so a docstring wrapped between the word and
    # its number or letter is caught. The word and a number match in any case;
    # a lettered phase matches only as a capital, so "stage a file" is English.
    # A section sign followed by a number is how the plan was cited.
    stale = re.compile(r"(?i:\b(?:stage|phase))[\s\-]*\d+[a-z]?\b"
                       r"|(?i:\bphase)[\s\-]*[A-E]\b"
                       r"|(?i:claim[\s\-_]*graph[\s\-_]*plan)|§\s*\d")
    for path in sorted(HERE.glob("*.py")):
        text = path.read_text()
        hits = [f"{text.count(chr(10), 0, m.start()) + 1}: {' '.join(m.group().split())}"
                for m in stale.finditer(text)]
        check(f"{path.name} names no stage, phase, or plan document", not hits,
              "; ".join(hits))

    # The anti-circularity control is the load-bearing one.
    check("ground prompt forbids prose as evidence",
          "Evidence must be executable code" in ground)
    check("ground prompt points at the stripped mirror",
          "<docstring removed>" in ground)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all prompt invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
