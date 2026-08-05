"""Builder tests — one per major pipeline mode, each validated in isolation.

These run before the pairwise suite in file order so `pytest -x` reports a
broken mode rather than a hundred broken combinations of it. Each asserts the
one thing that mode exists to establish; the covering array in
`test_integration.py` then checks that the modes compose.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    CLAIM_ID,
    NARROWED,
    Adjudication,
    Bound,
    Linkage,
    Narrowing,
    Scenario,
    Verdict,
    build_from_scenario,
    claim_node,
)

pytestmark = pytest.mark.integration


def test_main_should_carry_a_scope_note_to_the_graph_when_the_claim_is_bounded(
    pipeline_modules, tmp_path
):
    """Test the whole wire, from a grader's verdict to a graph node.

    Given:
        Two graders casting `supported` with a verified supporting citation, a
        verified limiting citation, and the narrowing the code supports.
    When:
        reconcile_verdicts, design_lens and emit_claim_graph run in sequence.
    Then:
        The claim node should read `supported`, be tagged verified, and carry
        both the narrowing and its limiting citation — this is the payload that
        was computed and silently dropped between two stages for months.
    """
    # Arrange
    scenario = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.PRESENT,
                        narrowing=Narrowing.PRESENT,
                        adjudication=Adjudication.NONE, linkage=Linkage.SYMBOL)

    # Act
    with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
        node = claim_node(graph)

    # Assert
    assert node["groundingVerdict"] == "supported"
    assert "verified" in node["tags"]
    assert node["scopeNote"]["asSupported"] == NARROWED
    assert node["scopeNote"]["bound"][0]["role"] == "limits"
    assert f"Holds as far as: {NARROWED}" in node["knowledgeMeta"]["content"]


def test_main_should_fold_the_legacy_verdict_when_graders_use_the_old_taxonomy(
    pipeline_modules, tmp_path
):
    """Test that a frozen archive still replays through the current pipeline.

    Given:
        Two graders casting the withdrawn `overstated` label with a verified
        bound and a narrowing.
    When:
        The three stages run in sequence.
    Then:
        The node should read `supported` and keep the bound as a scope note —
        the label was withdrawn, the located boundary was not.
    """
    # Arrange
    scenario = Scenario(verdict=Verdict.LEGACY_OVERSTATED, bound=Bound.PRESENT,
                        narrowing=Narrowing.PRESENT,
                        adjudication=Adjudication.NONE, linkage=Linkage.SYMBOL)

    # Act
    with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
        node = claim_node(graph)

    # Assert
    assert node["groundingVerdict"] == "supported"
    assert node["scopeNote"]["asSupported"] == NARROWED
    assert "OVERSTATED" not in node["tags"]


def test_main_should_drop_the_scope_note_when_an_adjudicator_overturns(
    pipeline_modules, tmp_path
):
    """Test that an overturned claim reaches the graph without a boundary.

    Given:
        A consensus `supported` claim carrying a scope note, overturned to
        `contradicted` by a tie-break vote.
    When:
        The three stages run in sequence.
    Then:
        The node should be tagged DRIFT and carry no scope note — a node
        asserting both "this is wrong" and "here is how far it holds" is
        incoherent, and this is the arm that produced it.
    """
    # Arrange
    scenario = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.PRESENT,
                        narrowing=Narrowing.PRESENT,
                        adjudication=Adjudication.OVERTURNS,
                        linkage=Linkage.SYMBOL)

    # Act
    with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
        node = claim_node(graph)

    # Assert
    assert node["groundingVerdict"] == "contradicted"
    assert "DRIFT" in node["tags"]
    assert "scopeNote" not in node
    assert "Holds as far as" not in node["knowledgeMeta"]["content"]


def test_main_should_omit_the_note_entirely_when_there_is_no_boundary(
    pipeline_modules, tmp_path
):
    """Test the negative path across the same three hops.

    Given:
        Two graders casting `supported` with a supporting citation only — no
        narrowing and no limiting citation.
    When:
        The three stages run in sequence.
    Then:
        No stage should emit a scope-note key, an empty note, or a boundary
        sentence. A truthy-but-empty note reads downstream as a finding.
    """
    # Arrange
    scenario = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.ABSENT,
                        narrowing=Narrowing.ABSENT,
                        adjudication=Adjudication.NONE, linkage=Linkage.SYMBOL)

    # Act
    with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
        node = claim_node(graph)

    # Assert
    assert "scopeNote" not in node
    assert "Holds as far as" not in node["knowledgeMeta"]["content"]


def test_main_should_link_to_the_containing_file_when_the_symbol_has_no_node(
    pipeline_modules, tmp_path
):
    """Test the weaker of the two linkage strengths.

    Given:
        A knowledge graph carrying a node for the claim's file but not for its
        qualname.
    When:
        The three stages run in sequence.
    Then:
        The claim should still reach the graph, attached to the containing
        file — a claim with no symbol node is worth less, not worth nothing.
    """
    # Arrange
    scenario = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.ABSENT,
                        narrowing=Narrowing.ABSENT,
                        adjudication=Adjudication.NONE,
                        linkage=Linkage.CONTAINING_FILE)

    # Act
    with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
        node = claim_node(graph)
        edges = [e for e in graph["edges"] if e["source"] == CLAIM_ID]

    # Assert
    assert node is not None
    assert edges, "a claim with only a file node should still draw an edge"
