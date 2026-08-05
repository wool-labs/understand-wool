"""Tests for emit_claim_graph.py — the hop that drops things silently.

This stage had no test module, and it is where the scope note's predecessor was
computed upstream and thrown away here for months without a symptom: each stage
was correct against its own contract, and the contract they violated existed
only between them.

So these tests assert the *inter-stage* facts — the field arrives, the tag does
not, a stale verdict is loud rather than mapped — rather than re-testing the
emitter's whole surface.
"""

from __future__ import annotations

import json
import sys

from tests.skill.docs.conftest import (
    CLAIM_ID,
    design_claim,
    make_design_claims,
    make_doc_units,
    make_graph,
)

NARROWED = "spawns the requested quantity unless it is zero"
NOTE = {"asSupported": NARROWED,
        "bound": [{"file": "pkg/pool.py", "lines": [8], "role": "limits"}]}


def grounding(verdict: str = "supported", note: dict | None = None) -> dict:
    entry = {"verdict": verdict, "agreement": "unanimous",
             "evidence": [{"file": "pkg/pool.py", "lines": [6, 7],
                           "role": "supports"}]}
    if note is not None:
        entry["scopeNote"] = note
    return entry


def run_main(module, monkeypatch, tmp_path, claims: list[dict]):
    """Run emit_claim_graph.main(); return the claim nodes by claimId."""
    out = tmp_path / "kg.json"
    monkeypatch.setattr(sys, "argv", [
        "emit_claim_graph.py",
        "--graph", str(make_graph(tmp_path / "graph.json")),
        "--design-claims", str(make_design_claims(tmp_path / "dc.json", claims)),
        "--doc-units", str(make_doc_units(tmp_path / "du.json")),
        "--out", str(out),
    ])
    assert module.main() == 0
    graph = json.loads(out.read_text())
    return {n["id"]: n for n in graph["nodes"] if n["type"] == "claim"}, graph


def test_main_should_tag_verdicts_without_ever_emitting_the_withdrawn_one(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test the verdict-to-tag mapping after the fourth verdict's withdrawal.

    Given:
        Claims verdicted supported, contradicted, and unverifiable.
    When:
        main() merges them into the knowledge graph.
    Then:
        Their tags should be verified, DRIFT, and unverified, and no node
        anywhere in the graph should carry an OVERSTATED tag.
    """
    # Arrange
    claims = [
        design_claim("claim:a", grounding("supported")),
        design_claim("claim:b", grounding("contradicted"), text="B holds."),
        design_claim("claim:c", grounding("unverifiable"), text="C holds."),
    ]

    # Act
    nodes, graph = run_main(emit_claim_graph, monkeypatch, tmp_path, claims)

    # Assert
    assert "verified" in nodes["claim:a"]["tags"]
    assert "DRIFT" in nodes["claim:b"]["tags"]
    assert "unverified" in nodes["claim:c"]["tags"]
    assert not any("OVERSTAT" in t for n in graph["nodes"] for t in n.get("tags", []))


def test_main_should_attach_the_scope_note_to_the_claim_node(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that the boundary reaches the graph.

    Given:
        A supported claim whose grounding carries a scope note.
    When:
        main() merges it into the knowledge graph.
    Then:
        The node should carry the note, and its readable content should state
        where the claim stops holding — the payload this stage used to discard.
    """
    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=grounding(note=NOTE))])

    # Assert
    node = nodes[CLAIM_ID]
    assert node["scopeNote"] == NOTE
    assert node["knowledgeMeta"]["content"].endswith(f"Holds as far as: {NARROWED}")


def test_main_should_not_raise_complexity_for_a_scoped_claim(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that a located boundary is a qualification, not a defect.

    Given:
        A supported claim carrying a scope note, and a contradicted claim.
    When:
        main() merges them into the knowledge graph.
    Then:
        Only the contradicted claim should be `complex`; a scoped claim keeps
        its ordinary level, because putting it in the same visual bucket as
        DRIFT is the mistake the withdrawn verdict made.
    """
    # Arrange
    claims = [
        design_claim("claim:a", grounding(note=NOTE)),
        design_claim("claim:b", grounding("contradicted"), text="B holds."),
    ]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims)

    # Assert
    assert nodes["claim:a"]["complexity"] != "complex"
    assert nodes["claim:b"]["complexity"] == "complex"


def test_main_should_omit_the_scope_note_when_the_claim_has_none(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test the negative control at the graph hop.

    Given:
        An ordinary supported claim with no scope note.
    When:
        main() merges it into the knowledge graph.
    Then:
        The node should carry no scopeNote key and no boundary sentence.
    """
    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=grounding())])

    # Assert
    node = nodes[CLAIM_ID]
    assert "scopeNote" not in node
    assert "Holds as far as" not in node["knowledgeMeta"]["content"]


def test_main_should_report_a_stale_verdict_rather_than_mapping_it(
    emit_claim_graph, monkeypatch, tmp_path, capsys
):
    """Test that a four-verdict artifact replayed here is loud.

    Given:
        A design-claims file still carrying the withdrawn `overstated` verdict,
        as the frozen archive does.
    When:
        main() merges it into the knowledge graph.
    Then:
        It should report the verdict as unmapped rather than inventing a tag
        for it — the fold is enforced upstream, and a stale input reaching this
        stage is a fact the operator needs told.
    """
    # Act
    run_main(emit_claim_graph, monkeypatch, tmp_path,
             [design_claim(grounding=grounding("overstated"))])
    out = capsys.readouterr().out

    # Assert
    assert "unmapped" in out.lower()
    assert "overstated" in out


def test_main_should_not_raise_when_a_scope_note_lacks_its_narrowing(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test robustness against a malformed note from an outside producer.

    Given:
        A scope note carrying only its bound, with no narrowing.
    When:
        main() merges the claim into the knowledge graph.
    Then:
        It should complete and omit the boundary sentence, rather than aborting
        the whole graph build — design-claims files are inputs, not necessarily
        this pipeline's own output.
    """
    # Arrange
    malformed = {"bound": NOTE["bound"]}

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=grounding(note=malformed))])

    # Assert
    assert "Holds as far as" not in nodes[CLAIM_ID]["knowledgeMeta"]["content"]
