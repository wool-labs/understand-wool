"""Unit tests for the Scenario model itself.

Fast, synchronous, and no pipeline: these validate the test infrastructure
before the pairwise suite trusts it. A broken `__or__` or `is_complete` would
silently narrow what the covering array actually exercises.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import (
    Adjudication,
    Bound,
    Linkage,
    Narrowing,
    Scenario,
    Verdict,
    expected_verdict,
    expects_scope_note,
    is_valid,
)


def test___or___should_merge_when_fields_are_disjoint():
    """Test algebraic merge of two partial scenarios.

    Given:
        Two scenarios setting different dimensions.
    When:
        They are merged with `|`.
    Then:
        It should return a scenario carrying both.
    """
    # Arrange
    a = Scenario(verdict=Verdict.SUPPORTED)
    b = Scenario(bound=Bound.PRESENT)

    # Act
    merged = a | b

    # Assert
    assert merged.verdict is Verdict.SUPPORTED
    assert merged.bound is Bound.PRESENT


def test___or___should_raise_when_fields_conflict():
    """Test that a merge cannot silently discard a value.

    Given:
        Two scenarios setting the same dimension to different values.
    When:
        They are merged with `|`.
    Then:
        It should raise ValueError rather than pick a winner.
    """
    # Arrange
    a = Scenario(verdict=Verdict.SUPPORTED)
    b = Scenario(verdict=Verdict.CONTRADICTED)

    # Act & assert
    with pytest.raises(ValueError, match="conflicting values for verdict"):
        a | b


def test___or___should_succeed_when_values_are_identical():
    """Test that agreeing scenarios merge without complaint.

    Given:
        Two scenarios setting the same dimension to the same value.
    When:
        They are merged with `|`.
    Then:
        It should return that value rather than treat equality as conflict.
    """
    # Arrange
    a = Scenario(verdict=Verdict.SUPPORTED)

    # Act
    merged = a | Scenario(verdict=Verdict.SUPPORTED)

    # Assert
    assert merged.verdict is Verdict.SUPPORTED


def test___or___should_return_the_original_when_merging_an_empty_scenario():
    """Test the identity element of the merge.

    Given:
        A populated scenario and an empty one.
    When:
        They are merged with `|`.
    Then:
        It should equal the populated scenario.
    """
    # Arrange
    a = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.ABSENT)

    # Act
    merged = a | Scenario()

    # Assert
    assert merged == a


def test_is_complete_should_return_false_when_a_dimension_is_unset():
    """Test the completeness guard the builder asserts on.

    Given:
        A scenario missing one dimension.
    When:
        is_complete is read.
    Then:
        It should be False, so the builder refuses a partial corpus.
    """
    # Arrange
    s = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.PRESENT,
                 narrowing=Narrowing.PRESENT, adjudication=Adjudication.NONE)

    # Act & assert
    assert s.is_complete is False


def test_is_complete_should_return_true_when_every_dimension_is_set():
    """Test the positive case of the completeness guard.

    Given:
        A scenario with every dimension set.
    When:
        is_complete is read.
    Then:
        It should be True.
    """
    # Arrange
    s = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.PRESENT,
                 narrowing=Narrowing.PRESENT, adjudication=Adjudication.NONE,
                 linkage=Linkage.SYMBOL)

    # Act & assert
    assert s.is_complete is True


def test___str___should_render_dimension_names_for_pytest_ids():
    """Test the id rendering the parametrized suite relies on.

    Given:
        A complete scenario.
    When:
        It is rendered with str().
    Then:
        It should be a dash-separated list of enum member names, so a failing
        pairwise case names its own configuration.
    """
    # Arrange
    s = Scenario(verdict=Verdict.SUPPORTED, bound=Bound.PRESENT,
                 narrowing=Narrowing.ABSENT, adjudication=Adjudication.NONE,
                 linkage=Linkage.SYMBOL)

    # Act
    rendered = str(s)

    # Assert
    assert rendered == "SUPPORTED-PRESENT-ABSENT-NONE-SYMBOL"


def test_is_valid_should_reject_overturning_an_already_contradicted_claim():
    """Test the one structural exclusion in the covering array.

    Given:
        A row whose consensus is already `contradicted` and whose adjudicator
        overturns.
    When:
        is_valid inspects it.
    Then:
        It should be rejected — that is agreement, not an overturn, and would
        duplicate the AGREES arm under a misleading name.
    """
    # Arrange
    row = [Verdict.CONTRADICTED, Bound.PRESENT, Narrowing.ABSENT,
           Adjudication.OVERTURNS]

    # Act & assert
    assert is_valid(row) is False


def test_is_valid_should_accept_overturning_a_supported_claim():
    """Test that the exclusion is narrow.

    Given:
        A row whose consensus is `supported` and whose adjudicator overturns.
    When:
        is_valid inspects it.
    Then:
        It should be accepted — this is the arm that historically resurrected a
        stale scope note, so filtering it out would hide the regression.
    """
    # Arrange
    row = [Verdict.SUPPORTED, Bound.PRESENT, Narrowing.PRESENT,
           Adjudication.OVERTURNS]

    # Act & assert
    assert is_valid(row) is True


def test_expected_verdict_should_fold_the_legacy_label_to_supported():
    """Test the oracle's treatment of the withdrawn verdict.

    Given:
        A scenario whose graders cast the legacy `overstated` label with no
        adjudication.
    When:
        expected_verdict is computed.
    Then:
        It should be `supported` — the claim was always true; only the label
        was withdrawn.
    """
    # Arrange
    s = Scenario(verdict=Verdict.LEGACY_OVERSTATED, bound=Bound.PRESENT,
                 narrowing=Narrowing.PRESENT, adjudication=Adjudication.NONE,
                 linkage=Linkage.SYMBOL)

    # Act & assert
    assert expected_verdict(s) == "supported"


def test_expects_scope_note_should_require_a_bound_and_a_narrowing_and_a_hold():
    """Test all three necessary conditions for a scope note.

    Given:
        Scenarios dropping each of the three conditions in turn.
    When:
        expects_scope_note is computed.
    Then:
        Only the scenario satisfying all three should expect a note.
    """
    # Arrange
    base = dict(verdict=Verdict.SUPPORTED, bound=Bound.PRESENT,
                narrowing=Narrowing.PRESENT, adjudication=Adjudication.NONE,
                linkage=Linkage.SYMBOL)

    # Act & assert
    assert expects_scope_note(Scenario(**base)) is True
    assert expects_scope_note(Scenario(**{**base, "bound": Bound.ABSENT})) is False
    assert expects_scope_note(
        Scenario(**{**base, "narrowing": Narrowing.ABSENT})) is False
    assert expects_scope_note(
        Scenario(**{**base, "adjudication": Adjudication.OVERTURNS})) is False
