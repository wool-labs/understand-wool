"""Pairwise and property-based exploration of the claim pipeline.

The covering array exercises every pairwise combination of the five dimensions;
the Hypothesis strategy then samples the same space randomly to catch
interactions a pairwise array does not guarantee. Both assert against the
oracle in `conftest.py`, which is derived from the taxonomy rather than from the
implementation — a test that recomputes what the code does proves only that the
code equals itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from allpairspy import AllPairs
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from tests.integration.conftest import (
    NARROWED,
    Adjudication,
    Bound,
    Linkage,
    Narrowing,
    Scenario,
    Verdict,
    build_from_scenario,
    claim_node,
    expected_verdict,
    expects_scope_note,
    is_valid,
)

pytestmark = pytest.mark.integration

DIMENSIONS = [list(Verdict), list(Bound), list(Narrowing),
              list(Adjudication), list(Linkage)]

PAIRWISE_SCENARIOS = [
    Scenario(verdict=row[0], bound=row[1], narrowing=row[2],
             adjudication=row[3], linkage=row[4])
    for row in AllPairs(DIMENSIONS, filter_func=is_valid)
]


# ── known bugs ───────────────────────────────────────────────────────────────
#
# Empty by design, and kept because the shape is what matters: a scenario that
# hits an open bug belongs here with an issue reference, NOT in `is_valid`.
# Filtering a bug out of the covering array makes the suite green by pretending
# the combination is structurally impossible, which is how a regression becomes
# permanent. Nothing in this pipeline is currently in that state.

@dataclass(frozen=True)
class _KnownBug:
    match: Callable[[Scenario], bool]
    raises: tuple[type[BaseException], ...]
    reason: str


_KNOWN_BUGS: list[_KnownBug] = []


@pytest.fixture()
def xfail_known_bugs():
    """Run a test body, converting a registered known failure into an xfail."""
    def run(scenario: Scenario, body: Callable[[], None]) -> None:
        try:
            body()
        except BaseException as exc:  # noqa: BLE001 — re-raised unless registered
            for bug in _KNOWN_BUGS:
                if bug.match(scenario) and isinstance(exc, bug.raises):
                    pytest.xfail(bug.reason)
            raise
    return run


def assert_chain_holds(scenario: Scenario, graph: dict) -> None:
    """Assert the emitted node matches what the taxonomy says it should be."""
    node = claim_node(graph)
    assert node is not None, f"no claim node emitted for {scenario}"

    verdict = expected_verdict(scenario)
    assert node["groundingVerdict"] == verdict

    if expects_scope_note(scenario):
        assert node["scopeNote"]["asSupported"] == NARROWED
        assert node["scopeNote"]["bound"], "a note must carry its citation"
        assert all(b["role"] in ("limits", "contradicts")
                   for b in node["scopeNote"]["bound"])
        assert f"Holds as far as: {NARROWED}" in node["knowledgeMeta"]["content"]
    else:
        assert node.get("scopeNote") in (None, {}), (
            f"{scenario} should carry no scope note")
        assert "Holds as far as" not in node["knowledgeMeta"]["content"]

    # The withdrawn label must not survive anywhere in the graph, whatever the
    # graders cast.
    assert "OVERSTATED" not in node["tags"]
    assert verdict != "overstated"


@pytest.mark.parametrize("scenario", PAIRWISE_SCENARIOS, ids=str)
def test_main_should_match_the_taxonomy_for_every_pairwise_scenario(
    scenario, pipeline_modules, tmp_path, xfail_known_bugs
):
    """Test the three-stage chain across a pairwise covering array.

    Given:
        Every pairwise combination of grader verdict, bound presence, narrowing
        presence, adjudication outcome, and graph linkage.
    When:
        reconcile_verdicts, design_lens and emit_claim_graph run in sequence
        over a corpus built from that combination.
    Then:
        The emitted claim node should carry the verdict and scope note the
        taxonomy predicts, and never the withdrawn label.
    """
    # Arrange
    def body():
        with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
            # Act & assert
            assert_chain_holds(scenario, graph)

    # Act & assert
    xfail_known_bugs(scenario, body)


@st.composite
def scenarios(draw) -> Scenario:
    """Draw a complete scenario that satisfies the same constraints as the array."""
    verdict = draw(st.sampled_from(list(Verdict)))
    adjudication = draw(st.sampled_from(list(Adjudication)))
    if verdict is Verdict.CONTRADICTED and adjudication is Adjudication.OVERTURNS:
        adjudication = Adjudication.AGREES
    return Scenario(
        verdict=verdict,
        bound=draw(st.sampled_from(list(Bound))),
        narrowing=draw(st.sampled_from(list(Narrowing))),
        adjudication=adjudication,
        linkage=draw(st.sampled_from(list(Linkage))),
    )


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])
@example(scenario=Scenario(verdict=Verdict.LEGACY_OVERSTATED, bound=Bound.PRESENT,
                           narrowing=Narrowing.PRESENT,
                           adjudication=Adjudication.NONE,
                           linkage=Linkage.SYMBOL))
@given(scenario=scenarios())
def test_main_should_match_the_taxonomy_for_any_drawn_scenario(
    scenario, pipeline_modules, tmp_path_factory
):
    """Test the chain against randomly drawn points in the same space.

    Given:
        Any complete scenario the strategy can draw, with the smoke case of a
        bounded legacy verdict pinned as an explicit example.
    When:
        The three stages run in sequence over a corpus built from it.
    Then:
        The emitted node should satisfy the taxonomy oracle — pairwise coverage
        guarantees every pair, not every interaction, so this samples the rest.
    """
    # Arrange
    tmp_path = tmp_path_factory.mktemp("chain")

    # Act
    with build_from_scenario(scenario, tmp_path, pipeline_modules) as graph:
        # Assert
        assert_chain_holds(scenario, graph)


def test_VERDICTS_should_match_the_emitters_tag_map(pipeline_modules):
    """Test that the reconciler and the emitter speak one vocabulary.

    Given:
        Both modules, loaded but not run.
    When:
        The reconciler's verdicts and the emitter's tag map are compared.
    Then:
        Their key sets should be equal and the withdrawn verdict should appear
        only among the legacy labels — the cheapest guard against the
        divergence this issue exists to fix, needing no corpus at all.
    """
    # Arrange
    reconcile, _, emit = pipeline_modules

    # Act
    verdicts = set(reconcile.VERDICTS)
    tagged = set(emit.VERDICT_TAG)

    # Assert
    assert verdicts == tagged
    assert "overstated" not in verdicts
    assert "overstated" in reconcile.LEGACY_VERDICTS
