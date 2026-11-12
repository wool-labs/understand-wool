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

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.skill.docs.conftest import (
    CLAIM_ID,
    DOC_UNIT_ID,
    design_claim,
    make_design_claims,
    make_doc_units,
    make_graph,
)

NARROWED = "spawns the requested quantity unless it is zero"
NOTE = {"asSupported": NARROWED,
        "bound": [{"file": "pkg/pool.py", "lines": [8], "role": "limits"}]}


def grounding(verdict: str = "supported", note: dict | None = None,
              agreement: str = "unanimous") -> dict:
    entry = {"verdict": verdict, "agreement": agreement,
             "evidence": [{"file": "pkg/pool.py", "lines": [6, 7],
                           "role": "supports"}]}
    if note is not None:
        entry["scopeNote"] = note
    return entry


OTHER_UNIT_ID = "docunit:u2"
SPAWN = "function:pkg/pool.py:Pool.spawn"
STOP = "function:pkg/pool.py:Pool.stop"
# Two symbols in one file, so one sentence can occur on each and be graded
# against each separately.
TWO_UNITS = [
    {"id": DOC_UNIT_ID, "sourceFile": "pkg/pool.py", "sourceLine": 5,
     "docLineRange": [6, 7], "attachedKind": "function", "attachedTo": SPAWN,
     "text": "Spawns workers."},
    {"id": OTHER_UNIT_ID, "sourceFile": "pkg/pool.py", "sourceLine": 10,
     "docLineRange": [11, 12], "attachedKind": "function", "attachedTo": STOP,
     "text": "Stops workers."},
]
SPAWN_AT = {"filePath": "pkg/pool.py", "lineRange": [6, 7]}
STOP_AT = {"filePath": "pkg/pool.py", "lineRange": [11, 12]}
SHARED_PREFIX = ("DispatchingLoadBalancerLike.dispatch raises "
                 "NoWorkersAvailable when the pool is ")


def run_main(module, monkeypatch, tmp_path, claims: list[dict],
             units: list[dict] | None = None, graph_path=None):
    """Run emit_claim_graph.main(); return the claim nodes by claimId.

    ``graph_path`` feeds an existing graph in as ``--graph`` — a previous
    run's output, for the re-emit tests — instead of a fresh structural one.
    """
    out = tmp_path / "kg.json"
    monkeypatch.setattr(sys, "argv", [
        "emit_claim_graph.py",
        "--graph", str(graph_path or make_graph(tmp_path / "graph.json")),
        "--design-claims", str(make_design_claims(tmp_path / "dc.json", claims)),
        "--doc-units", str(make_doc_units(tmp_path / "du.json", units)),
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


@pytest.mark.parametrize("ungraded", [None, {}], ids=["absent", "empty"])
def test_main_should_mark_the_node_ungrounded_when_no_grader_examined_it(
    emit_claim_graph, monkeypatch, tmp_path, ungraded
):
    """Test that "never checked" is stated rather than left as an absence.

    Given:
        A claim whose grounding record is absent or empty.
    When:
        main() merges it into the knowledge graph.
    Then:
        It should carry groundingVerdict `ungrounded` and the matching tag, and
        should not carry the `unverified` tag that marks an examined claim.
    """
    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=ungraded)])

    # Assert
    node = nodes[CLAIM_ID]
    assert node["groundingVerdict"] == "ungrounded"
    assert "ungrounded" in node["tags"]
    assert "unverified" not in node["tags"]


def test_main_should_say_ungrounded_on_the_documents_edge_when_no_grader_examined_it(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that the edge does not imply a grading that never ran.

    Given:
        A claim with no grounding record.
    When:
        main() merges it into the knowledge graph.
    Then:
        It should describe the `documents` edge as ungrounded.
    """
    # Act
    _, graph = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=None)])

    # Assert
    edge = next(e for e in graph["edges"] if e["source"] == CLAIM_ID)
    assert ", ungrounded;" in edge["description"]


def test_main_should_report_grounding_counts(
    emit_claim_graph, monkeypatch, tmp_path, capsys
):
    """Test that the coverage gap is visible to the operator.

    Given:
        One supported claim and one claim with no grounding record.
    When:
        main() merges them into the knowledge graph.
    Then:
        It should print how many claim nodes carry each grounding status.
    """
    # Arrange
    claims = [design_claim("claim:a", grounding("supported")),
              design_claim("claim:b", None, text="B holds.")]

    # Act
    run_main(emit_claim_graph, monkeypatch, tmp_path, claims)
    out = capsys.readouterr().out

    # Assert
    assert "grounding: {'supported': 1, 'ungrounded': 1}" in out


def test_main_should_keep_the_full_claim_text_as_the_name_when_the_text_is_long(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that the identity-bearing field is not abbreviated.

    Given:
        Three distinct claims that share a prefix longer than the width the
        name used to be cut to.
    When:
        main() merges them into the knowledge graph.
    Then:
        It should name each node with its full claim text, so the three names
        differ and none ends in an ellipsis.
    """
    # Arrange
    texts = [SHARED_PREFIX + tail for tail in
             ("exhausted.", "empty on entry.",
              "fully evicted across one rotation.")]
    claims = [design_claim(f"claim:{i}", grounding("unverifiable"), text=t)
              for i, t in enumerate(texts)]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims)

    # Assert
    assert sorted(n["name"] for n in nodes.values()) == sorted(texts)


def test_main_should_share_the_verdict_when_merged_occurrences_agree(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test a merged node whose occurrences were graded alike.

    Given:
        One sentence stated on two symbols, both graded `supported`.
    When:
        main() merges the two occurrences into one node.
    Then:
        It should carry the shared verdict and list both occurrences, the
        canonical one first, each with the symbol it was graded against.
    """
    # Arrange
    claims = [design_claim("claim:a", grounding("supported")),
              design_claim("claim:b", grounding("supported"), unit=OTHER_UNIT_ID)]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims,
                        TWO_UNITS)

    # Assert
    assert list(nodes) == ["claim:a"]
    node = nodes["claim:a"]
    assert node["groundingVerdict"] == "supported"
    assert node["groundingAgreement"] == "unanimous"
    assert node["groundingOccurrences"] == [
        {"claimId": "claim:a", "attachedTo": SPAWN, **SPAWN_AT,
         "groundingVerdict": "supported", "groundingAgreement": "unanimous"},
        {"claimId": "claim:b", "attachedTo": STOP, **STOP_AT,
         "groundingVerdict": "supported", "groundingAgreement": "unanimous"},
    ]


def test_main_should_omit_the_occurrence_list_when_the_claim_occurs_once(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test the negative control for the occurrence list.

    Given:
        A claim stated in exactly one docstring.
    When:
        main() merges it into the knowledge graph.
    Then:
        It should carry no groundingOccurrences key.
    """
    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=grounding())])

    # Assert
    assert "groundingOccurrences" not in nodes[CLAIM_ID]


def test_main_should_mark_the_node_mixed_when_merged_occurrences_disagree(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that a merge cannot attribute one occurrence's verdict to another.

    Given:
        One sentence stated on two symbols, the canonical occurrence graded
        `supported` with a scope note and the other never graded.
    When:
        main() merges the two occurrences into one node.
    Then:
        It should read `mixed`, keep each occurrence's own verdict and scope
        note in the occurrence list, and carry no node-level scope note,
        agreement, or `verified` tag.
    """
    # Arrange
    claims = [design_claim("claim:a", grounding("supported", NOTE)),
              design_claim("claim:b", None, unit=OTHER_UNIT_ID)]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims,
                        TWO_UNITS)

    # Assert
    node = nodes["claim:a"]
    assert node["groundingVerdict"] == "mixed"
    assert node["groundingOccurrences"] == [
        {"claimId": "claim:a", "attachedTo": SPAWN, **SPAWN_AT,
         "groundingVerdict": "supported", "groundingAgreement": "unanimous",
         "scopeNote": NOTE},
        {"claimId": "claim:b", "attachedTo": STOP, **STOP_AT,
         "groundingVerdict": "ungrounded"},
    ]
    assert "scopeNote" not in node
    assert "groundingAgreement" not in node
    assert "mixed" in node["tags"]
    assert "verified" not in node["tags"]
    assert "Holds as far as" not in node["knowledgeMeta"]["content"]


def test_main_should_flag_drift_when_any_merged_occurrence_is_contradicted(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that a merge cannot hide a finding behind a clean sibling.

    Given:
        One sentence stated on two symbols, the canonical occurrence graded
        `supported` and the other `contradicted`.
    When:
        main() merges the two occurrences into one node.
    Then:
        It should read `mixed`, carry the DRIFT tag, and be `complex`.
    """
    # Arrange
    claims = [design_claim("claim:a", grounding("supported")),
              design_claim("claim:b", grounding("contradicted"),
                           unit=OTHER_UNIT_ID)]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims,
                        TWO_UNITS)

    # Assert
    node = nodes["claim:a"]
    assert node["groundingVerdict"] == "mixed"
    assert "DRIFT" in node["tags"]
    assert node["complexity"] == "complex"


def test_main_should_draw_a_documents_edge_to_each_symbol_when_occurrences_merge(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that a merge leaves no symbol without its claim.

    Given:
        A graph carrying a node for each of two functions, and one sentence
        stated on both, graded `supported` on one and `contradicted` on the
        other.
    When:
        main() merges the two occurrences into one node.
    Then:
        It should draw a `documents` edge to each function, each described
        with the verdict earned on that function.
    """
    # Arrange
    graph_path = make_graph(tmp_path / "graph.json")
    payload = json.loads(graph_path.read_text())
    for node_id in (SPAWN, STOP):
        payload["nodes"].append({
            "id": node_id, "type": "function", "name": node_id.rsplit(":", 1)[1],
            "filePath": "pkg/pool.py", "summary": "s", "tags": [],
            "complexity": "simple"})
    graph_path.write_text(json.dumps(payload))
    claims = [design_claim("claim:a", grounding("supported")),
              design_claim("claim:b", grounding("contradicted"),
                           unit=OTHER_UNIT_ID)]

    # Act
    _, graph = run_main(emit_claim_graph, monkeypatch, tmp_path, claims,
                        TWO_UNITS, graph_path=graph_path)

    # Assert
    described = {e["target"]: e["description"] for e in graph["edges"]
                 if e["source"] == "claim:a" and e["type"] == "documents"}
    assert set(described) == {SPAWN, STOP}
    assert "grounded supported" in described[SPAWN]
    assert "grounded contradicted" in described[STOP]


def test_main_should_report_occurrence_counts_when_a_merge_hides_an_ungraded_one(
    emit_claim_graph, monkeypatch, tmp_path, capsys
):
    """Test that the coverage measure survives merging.

    Given:
        One sentence stated on two symbols, graded on one and not the other.
    When:
        main() merges the two occurrences into one `mixed` node.
    Then:
        It should print per-occurrence counts that show the ungraded
        occurrence the node-level counts do not.
    """
    # Arrange
    claims = [design_claim("claim:a", grounding("supported")),
              design_claim("claim:b", None, unit=OTHER_UNIT_ID)]

    # Act
    run_main(emit_claim_graph, monkeypatch, tmp_path, claims, TWO_UNITS)
    out = capsys.readouterr().out

    # Assert
    assert "grounding: {'mixed': 1}" in out
    assert "occurrences: {'supported': 1, 'ungrounded': 1}" in out


def test_main_should_remove_an_unstamped_pipeline_claim_node_when_its_claim_is_gone(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test re-emission over a graph written before the provenance stamp.

    Given:
        An input graph holding a claim node with a content-hash id, no
        emittedBy stamp, no verdict, and the same name as a current claim
        whose id differs.
    When:
        main() merges the current claim into it.
    Then:
        It should remove the legacy node, leaving one claim node under that
        name and none without a verdict.
    """
    # Arrange
    graph_path = make_graph(tmp_path / "graph.json")
    payload = json.loads(graph_path.read_text())
    payload["nodes"].append({
        "id": "claim:" + "0" * 64, "type": "claim",
        "name": "Pool.spawn spawns workers.", "summary": "old", "tags": [],
        "complexity": "simple"})
    graph_path.write_text(json.dumps(payload))

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=grounding())],
                        graph_path=graph_path)

    # Assert
    assert list(nodes) == [CLAIM_ID]
    assert all("groundingVerdict" in n for n in nodes.values())


@pytest.mark.parametrize("canonical_has_note", [True, False],
                         ids=["note-on-canonical", "note-on-other"])
def test_main_should_keep_a_scope_note_per_occurrence_when_only_one_carries_it(
    emit_claim_graph, monkeypatch, tmp_path, canonical_has_note
):
    """Test that an agreeing merge cannot lend one symbol's bound to another.

    Given:
        One sentence stated on two symbols, both graded `supported`, with a
        scope note on exactly one of the two occurrences.
    When:
        main() merges the two occurrences into one node.
    Then:
        It should read `supported`, carry the note only on the occurrence that
        earned it, and state no boundary at node level.
    """
    # Arrange
    first, second = (NOTE, None) if canonical_has_note else (None, NOTE)
    claims = [design_claim("claim:a", grounding("supported", first)),
              design_claim("claim:b", grounding("supported", second),
                           unit=OTHER_UNIT_ID)]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims,
                        TWO_UNITS)

    # Assert
    node = nodes["claim:a"]
    assert node["groundingVerdict"] == "supported"
    assert "scopeNote" not in node
    assert "Holds as far as" not in node["knowledgeMeta"]["content"]
    assert [o.get("scopeNote") for o in node["groundingOccurrences"]] == [
        first, second]


def test_main_should_keep_agreement_per_occurrence_when_the_levels_differ(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that an agreeing merge cannot lend one grading's confidence to another.

    Given:
        One sentence stated on two symbols, both graded `supported`, one
        unanimously and one by an unadjudicated split.
    When:
        main() merges the two occurrences into one node.
    Then:
        It should carry no node-level groundingAgreement and list each
        occurrence's own level.
    """
    # Arrange
    claims = [design_claim("claim:a", grounding("supported")),
              design_claim("claim:b", grounding("supported", agreement="split"),
                           unit=OTHER_UNIT_ID)]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims,
                        TWO_UNITS)

    # Assert
    node = nodes["claim:a"]
    assert "groundingAgreement" not in node
    assert [o["groundingAgreement"] for o in node["groundingOccurrences"]] == [
        "unanimous", "split"]


def test_main_should_replace_its_earlier_claim_nodes_when_fed_its_own_output(
    emit_claim_graph, monkeypatch, tmp_path, capsys
):
    """Test the emit, ground, emit-again workflow.

    Given:
        A graph already emitted with two ungraded claims, used as the input
        graph of a second run in which one claim is graded `supported` and the
        other no longer exists.
    When:
        main() merges the second run's claims into it.
    Then:
        It should leave one node per current claim carrying the second run's
        verdict, one `documents` edge for it, no node for the withdrawn claim,
        and should report the replacement.
    """
    # Arrange
    first = tmp_path / "first"
    first.mkdir()
    run_main(emit_claim_graph, monkeypatch, first,
             [design_claim("claim:a", None),
              design_claim("claim:gone", None, text="Gone holds.")])
    capsys.readouterr()

    # Act
    nodes, graph = run_main(emit_claim_graph, monkeypatch, tmp_path,
                            [design_claim("claim:a", grounding("supported"))],
                            graph_path=first / "kg.json")
    out = capsys.readouterr().out

    # Assert
    claim_nodes = [n for n in graph["nodes"] if n["type"] == "claim"]
    assert [n["id"] for n in claim_nodes] == ["claim:a"]
    assert nodes["claim:a"]["groundingVerdict"] == "supported"
    assert len([e for e in graph["edges"] if e["source"] == "claim:a"]) == 1
    assert not any("claim:gone" in (e["source"], e["target"])
                   for e in graph["edges"])
    assert "replaced 2 node(s)" in out


def test_main_should_keep_claim_nodes_another_producer_wrote(
    emit_claim_graph, monkeypatch, tmp_path
):
    """Test that the replacement is confined to this emitter's own nodes.

    Given:
        An input graph carrying a claim node that this emitter did not write
        and whose id is not among the claims being emitted.
    When:
        main() merges a claim into it.
    Then:
        It should keep the foreign claim node unchanged.
    """
    # Arrange
    graph_path = make_graph(tmp_path / "graph.json")
    payload = json.loads(graph_path.read_text())
    foreign = {"id": "claim:paper:1", "type": "claim", "name": "A paper claim",
               "summary": "From a publication.", "tags": [],
               "complexity": "simple"}
    payload["nodes"].append(foreign)
    graph_path.write_text(json.dumps(payload))

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path,
                        [design_claim(grounding=grounding())],
                        graph_path=graph_path)

    # Assert
    assert nodes["claim:paper:1"] == foreign


@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(texts=st.lists(st.text(min_size=1, max_size=200), min_size=1, max_size=8),
       graded=st.lists(st.sampled_from(
           [None, "supported", "contradicted", "unverifiable", "overstated"]),
           min_size=8, max_size=8))
def test_main_should_emit_unique_names_and_a_verdict_on_every_claim_node(
    emit_claim_graph, monkeypatch, tmp_path, texts, graded
):
    """Test the two trust-surface invariants over arbitrary claim sets.

    Given:
        Any list of claim texts, duplicates and near-duplicates included, each
        graded with any current verdict, the withdrawn legacy verdict, or not
        graded at all.
    When:
        main() merges them into the knowledge graph.
    Then:
        No two claim nodes should share a name, and every claim node should
        carry one of the five grounding statuses — or the legacy verdict
        verbatim, the one documented exception, which only a stale artifact
        that bypassed the reconciler can produce.
    """
    # Arrange
    claims = [design_claim(f"claim:{i}", grounding(v) if v else None, text=t)
              for i, (t, v) in enumerate(zip(texts, graded))]

    # Act
    nodes, _ = run_main(emit_claim_graph, monkeypatch, tmp_path, claims)

    # Assert
    names = [n["name"] for n in nodes.values()]
    assert len(names) == len(set(names))
    assert all(n["groundingVerdict"] in {"supported", "contradicted",
                                         "unverifiable", "ungrounded", "mixed",
                                         "overstated"}
               for n in nodes.values())
