"""Tests for reconcile_claims.py — N-pass extraction reconciliation.

The two invariants that matter most: quorum counts distinct pass indices
(never a sum — a single pass must not clear quorum with two wordings of the
same assertion), and the sourceQuote gate is what keeps disjunction halves
apart when lexical similarity alone would merge them.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.skill.docs.conftest import write_json

UNIT = "docunit:u1"


def extraction_claim(text: str, quote: str, ctype: str = "factual") -> dict:
    return {
        "docUnitId": UNIT,
        "claimText": text,
        "claimType": ctype,
        "quantifier": "particular",
        "sourceQuote": quote,
        "fieldIndex": None,
    }


def write_passes(root: Path, per_pass: list[list[dict]]) -> list[Path]:
    """Write pass-<n>.json files; sorted filename order defines pass index."""
    return [
        write_json(root / f"pass-{i}.json",
                   {"bundle": "b", "pass": i, "subsystem": "layer:t", "claims": claims})
        for i, claims in enumerate(per_pass, start=1)
    ]


def run_main(module, monkeypatch, tmp_path, pass_files, extra=()):
    out = tmp_path / "out"
    argv = ["reconcile_claims.py", *[str(p) for p in pass_files],
            "--out", str(out), "--label", "t", "--quorum", "3", *extra]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    survivors = json.loads((out / "claims-t.json").read_text())
    rejected = json.loads((out / "claims-rejected-t.json").read_text())
    return survivors, rejected, out


def test_main_should_keep_claim_when_three_of_five_passes_agree(
    reconcile_claims, monkeypatch, tmp_path
):
    """Test tier-1 quorum on byte-identical wordings.

    Given:
        The identical claim text emitted by passes 1-3 of 5.
    When:
        main() reconciles at quorum 3.
    Then:
        It should keep the claim with passCount 3 and distinct pass indices.
    """
    # Arrange
    c = extraction_claim("Pool.spawn spawns workers.", "Spawns workers")
    files = write_passes(tmp_path, [[c], [c], [c], [], []])

    # Act
    survivors, rejected, _ = run_main(reconcile_claims, monkeypatch, tmp_path, files)

    # Assert
    (claim,) = survivors["claims"]
    assert claim["passCount"] == 3
    assert claim["extractionPasses"] == [1, 2, 3]
    assert rejected["claims"] == []


def test_main_should_reject_claim_when_single_pass_emits_two_wordings(
    reconcile_claims, monkeypatch, tmp_path
):
    """Test that quorum counts distinct pass indices, never a sum.

    Given:
        One pass emitting two near-identical wordings of one assertion with the
        same quote, at quorum 2.
    When:
        main() reconciles the corpus.
    Then:
        It should reject the merged claim — one pass cannot clear quorum alone.
    """
    # Arrange
    a = extraction_claim("Task is single use.", "single-use as a context manager")
    b = extraction_claim("Task is single-use.", "single-use as a context manager")
    files = write_passes(tmp_path, [[a, b]])

    # Act
    survivors, rejected, _ = run_main(
        reconcile_claims, monkeypatch, tmp_path, files, extra=("--quorum", "2")
    )

    # Assert
    assert survivors["claims"] == []
    (claim,) = rejected["claims"]
    assert claim["passCount"] == 1


def test_main_should_merge_wordings_when_quotes_overlap(
    reconcile_claims, monkeypatch, tmp_path
):
    """Test tier-2 merging of near-identical wordings across passes.

    Given:
        Two highly similar wordings sharing the same sourceQuote, spread across
        three distinct passes.
    When:
        main() reconciles at quorum 3.
    Then:
        It should merge them into one surviving claim carrying both variants.
    """
    # Arrange
    quote = "Spawns the requested quantity of workers."
    a = extraction_claim("Pool.spawn creates the requested quantity of workers.", quote)
    b = extraction_claim(
        "Pool.spawn creates the requested quantity of worker processes.", quote
    )
    files = write_passes(tmp_path, [[a], [a], [b]])

    # Act
    survivors, rejected, _ = run_main(reconcile_claims, monkeypatch, tmp_path, files)

    # Assert
    (claim,) = survivors["claims"]
    assert claim["passCount"] == 3
    assert len(claim["variants"]) == 2
    assert rejected["claims"] == []


def test_main_should_not_merge_when_quotes_differ(
    reconcile_claims, monkeypatch, tmp_path
):
    """Test the quote gate on disjunction halves.

    Given:
        Two wordings lexically similar above the merge threshold but quoting
        different spans of the docstring — the two halves of a disjunction.
    When:
        main() reconciles at quorum 3 with each half emitted by 3 passes.
    Then:
        It should keep them as two separate surviving claims.
    """
    # Arrange
    a = extraction_claim("Pool.stop cancels the running tasks.", "cancel running tasks")
    b = extraction_claim("Pool.stop cancels the pending tasks.", "or drop pending tasks")
    files = write_passes(tmp_path, [[a, b], [a, b], [a, b]])

    # Act
    survivors, _, _ = run_main(reconcile_claims, monkeypatch, tmp_path, files)

    # Assert
    assert len(survivors["claims"]) == 2


def test_main_should_write_subquorum_claims_to_rejected_file(
    reconcile_claims, monkeypatch, tmp_path
):
    """Test that sub-quorum claims are retained, never silently dropped.

    Given:
        A claim emitted by only one pass of three, at quorum 3.
    When:
        main() reconciles the corpus.
    Then:
        It should write the claim to the rejected artifact with its pass count.
    """
    # Arrange
    a = extraction_claim("Pool.spawn spawns workers.", "Spawns workers")
    lone = extraction_claim("Pool.spawn is idempotent.", "idempotent")
    files = write_passes(tmp_path, [[a, lone], [a], [a]])

    # Act
    survivors, rejected, _ = run_main(reconcile_claims, monkeypatch, tmp_path, files)

    # Assert
    assert len(survivors["claims"]) == 1
    (dropped,) = rejected["claims"]
    assert dropped["claimText"] == "Pool.spawn is idempotent."
    assert dropped["passCount"] == 1


def test_main_should_exclude_aspirational_claims_when_default_types(
    reconcile_claims, monkeypatch, tmp_path
):
    """Test claimType filtering before reconciliation.

    Given:
        An aspirational claim emitted by three passes, default --types.
    When:
        main() reconciles the corpus.
    Then:
        It should exclude it into the excluded artifact and count it by type,
        and keep it when --types all is passed.
    """
    # Arrange
    asp = extraction_claim("Workers should feel effortless.", "effortless", "aspirational")
    files = write_passes(tmp_path, [[asp], [asp], [asp]])

    # Act
    survivors, _, out = run_main(reconcile_claims, monkeypatch, tmp_path, files)
    excluded = json.loads((out / "claims-excluded-t.json").read_text())

    # Assert
    assert survivors["claims"] == []
    assert survivors["stats"]["excludedByType"] == {"aspirational": 3}
    assert len(excluded["claims"]) == 3

    # Act — again with the filter disabled
    survivors_all, _, _ = run_main(
        reconcile_claims, monkeypatch, tmp_path, files, extra=("--types", "all")
    )

    # Assert
    assert len(survivors_all["claims"]) == 1


@settings(max_examples=20, deadline=None)
@given(perm=st.permutations(range(3)))
def test_main_should_produce_identical_survivors_for_any_pass_order(
    reconcile_claims, perm
):
    """Test order-independence of reconciliation.

    Given:
        The same three pass contents assigned to pass files in any permutation.
    When:
        main() reconciles each arrangement.
    Then:
        It should produce the identical survivor claim-ID set.
    """
    # Arrange
    quote = "Spawns the requested quantity of workers."
    a = extraction_claim("Pool.spawn creates the requested quantity of workers.", quote)
    b = extraction_claim(
        "Pool.spawn creates the requested quantity of worker processes.", quote
    )
    lone = extraction_claim("Pool.spawn is idempotent.", "idempotent")
    contents = [[a], [a, lone], [b]]

    def survivor_ids(arrangement):
        # Hypothesis re-runs the body without resetting function-scoped
        # fixtures, so argv patching uses a local MonkeyPatch context instead.
        with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as mp:
            tmp = Path(td)
            files = write_passes(tmp, arrangement)

            # Act
            survivors, _, _ = run_main(reconcile_claims, mp, tmp, files)
        return sorted((c["id"], c["passCount"]) for c in survivors["claims"])

    # Assert
    assert survivor_ids([contents[i] for i in perm]) == survivor_ids(contents)
