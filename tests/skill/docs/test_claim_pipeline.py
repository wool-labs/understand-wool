"""The scope note's four-hop journey: agent verdict → reconcile → lens → graph.

Why this file exists, when every stage already has unit tests: the historical
bug it guards against was invisible to any unit test written in good faith.
`design_lens` correctly carried a field its own spec named; `emit_claim_graph`
correctly emitted the fields *its* spec named. Neither spec mentioned the other,
so both stages passed their own tests while the payload died between them. The
violated contract existed only in the gap, and only a chained run can encode it.

It does not replace the unit tests. One chained path cannot cover truncation
ordering, duplicate collapse, or unmapped verdicts — those stay where they are.

Deviation from the project test guide, deliberate: the guide's integration
section prescribes `tests/integration/` with a Scenario dataclass, pairwise
covering arrays, and an xfail registry. That apparatus exists for systems with
orthogonal runtime dimensions; this is a fixed three-stage chain that runs in
milliseconds on tmp_path with no subprocess, network, or concurrency. These live
beside their unit siblings under a registered `pipeline` marker instead.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.skill.docs.conftest import (
    CLAIM_ID,
    DOC_UNIT_ID,
    evidence,
    make_bundle,
    make_claim,
    make_claims_file,
    make_doc_units,
    make_graph,
    make_pass,
    reconciled_claim,
    verdict_entry,
)

pytestmark = pytest.mark.pipeline

GOOD_LINES = [6, 7]
GOOD_CODE = "if count == 0:\n    count = cpu_count()"
LIMIT_CODE = "return [Worker() for _ in range(count)]"
NARROWED = "spawns the requested quantity unless it is zero"


def run_chain(reconcile, lens, emit, monkeypatch, mirror, tmp_path, entry):
    """Run all three stages in order on one claim; return its graph node."""
    bundles = tmp_path / "bundles"
    make_bundle(bundles, "b1", [make_claim("c1", "Pool.spawn spawns workers.")],
                source_file="pkg/pool.py", qualname="Pool.spawn")
    passes = tmp_path / "verdicts"
    files = [make_pass(passes, "b1-pass1", [entry]),
             make_pass(passes, "b1-pass2", [entry])]
    grounded_dir = tmp_path / "grounded"

    monkeypatch.setattr(sys, "argv", [
        "reconcile_verdicts.py", *[str(f) for f in files],
        "--bundles", str(bundles), "--mirror", str(mirror),
        "--out", str(grounded_dir), "--label", "t",
    ])
    assert reconcile.main() == 0

    # The reconciler keys claims by content hash; the lens joins on that id, so
    # the corpus it reads must carry the same one.
    claims_file = make_claims_file(tmp_path / "claims-pool.json",
                                   [reconciled_claim(cid=CLAIM_ID)])
    grounded = json.loads((grounded_dir / "grounded-t.json").read_text())
    for c in grounded["claims"]:
        c["claimId"] = CLAIM_ID
    (grounded_dir / "grounded-t.json").write_text(json.dumps(grounded))

    lens_out = tmp_path / "lens"
    monkeypatch.setattr(sys, "argv", [
        "design_lens.py", str(claims_file),
        "--grounded", str(grounded_dir / "grounded-t.json"),
        "--out", str(lens_out),
    ])
    assert lens.main() == 0

    kg = tmp_path / "kg.json"
    monkeypatch.setattr(sys, "argv", [
        "emit_claim_graph.py",
        "--graph", str(make_graph(tmp_path / "graph.json")),
        "--design-claims", str(lens_out / "design-claims.json"),
        "--doc-units", str(make_doc_units(tmp_path / "du.json")),
        "--out", str(kg),
    ])
    assert emit.main() == 0

    graph = json.loads(kg.read_text())
    nodes = [n for n in graph["nodes"] if n["type"] == "claim"]
    return next(n for n in nodes if n["id"] == CLAIM_ID), graph


def test_scope_note_should_survive_from_verdict_to_claim_node(
    reconcile_verdicts, design_lens, emit_claim_graph, monkeypatch, mirror, tmp_path
):
    """Test the whole wire, from an agent's verdict to a graph node.

    Given:
        Two passes voting the withdrawn `overstated` verdict, each citing a
        verified supporting item and a verified limiting item, and carrying the
        narrowing the code supports.
    When:
        reconcile_verdicts, design_lens, and emit_claim_graph run in sequence.
    Then:
        The final claim node should read `supported`, be tagged verified, and
        carry both the narrowing and its limiting citation — the fold's only
        surviving output, across every hop that could drop it.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "overstated",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        scope_note={"asSupported": NARROWED},
    )

    # Act
    node, _ = run_chain(reconcile_verdicts, design_lens, emit_claim_graph,
                        monkeypatch, mirror, tmp_path, entry)

    # Assert
    assert node["groundingVerdict"] == "supported"
    assert "verified" in node["tags"]
    assert node["scopeNote"]["asSupported"] == NARROWED
    assert node["scopeNote"]["bound"][0]["role"] == "limits"
    assert f"Holds as far as: {NARROWED}" in node["knowledgeMeta"]["content"]


def test_no_stage_should_invent_a_partial_note_when_there_is_no_boundary(
    reconcile_verdicts, design_lens, emit_claim_graph, monkeypatch, mirror, tmp_path
):
    """Test the negative path across the same three hops.

    Given:
        Two passes voting `supported` with a supporting citation only — no
        narrowing, no limiting citation.
    When:
        reconcile_verdicts, design_lens, and emit_claim_graph run in sequence.
    Then:
        No stage should emit a scope-note key, an empty note, or a boundary
        sentence. A positive-only chain would miss a stage that manufactures a
        truthy-but-empty note, which reads downstream as a finding.
    """
    # Arrange
    entry = verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)])

    # Act
    node, _ = run_chain(reconcile_verdicts, design_lens, emit_claim_graph,
                        monkeypatch, mirror, tmp_path, entry)

    # Assert
    assert "scopeNote" not in node
    assert "Holds as far as" not in node["knowledgeMeta"]["content"]


def test_verdict_vocabulary_should_match_across_stages(
    reconcile_verdicts, emit_claim_graph
):
    """Test that the reconciler and the emitter speak one vocabulary.

    Given:
        Both modules, loaded but not run.
    When:
        The reconciler's verdicts and the emitter's tag map are compared.
    Then:
        Their key sets should be equal, and the withdrawn verdict should appear
        only among the legacy labels — the cheapest possible guard against the
        divergence this issue exists to fix, needing no corpus at all.
    """
    # Act
    verdicts = set(reconcile_verdicts.VERDICTS)
    tagged = set(emit_claim_graph.VERDICT_TAG)

    # Assert
    assert verdicts == tagged
    assert "overstated" not in verdicts
    assert "overstated" in reconcile_verdicts.LEGACY_VERDICTS
