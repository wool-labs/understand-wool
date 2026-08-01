"""Tests for lint_claims.py — extraction-artifact detection.

The two checks guard the seam between extraction and grounding: a claim that
asserts more than its docstring did makes grounding report a defect the
documentation never committed. Precision matters as much as recall here — both
checks carry suppressions (governed-clause overlap, sibling claims) that took
the wool false-positive rate from ~38 flags to 10, and those suppressions are
what these tests pin down.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.skill.docs.conftest import write_json

UNIT = "docunit:u1"


def claims_file(tmp_path: Path, rows: list[dict], name: str = "claims-t.json") -> Path:
    return write_json(tmp_path / name, {"claims": rows})


def row(cid: str, text: str, quote: str, unit: str = UNIT) -> dict:
    return {"id": f"claim:{cid}", "docUnitId": unit,
            "claimText": text, "sourceQuote": quote}


def run_main(module, monkeypatch, capsys, argv_tail):
    monkeypatch.setattr(sys, "argv", ["lint_claims.py", *map(str, argv_tail)])
    code = module.main()
    return code, capsys.readouterr().out


def test_main_should_flag_dropped_qualifier_when_governed_clause_lost(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test detection of a qualifier stripped during extraction.

    Given:
        A quote narrowing with "when" and a claim carrying neither the word nor
        any of the content it governs.
    When:
        main() lints the claim.
    Then:
        It should report one dropped-qualifier flag.
    """
    # Arrange
    f = claims_file(tmp_path, [row(
        "c1", "quorum_timeout is meaningful.",
        "Only meaningful when quorum is a positive integer",
    )])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys, [f])

    # Assert
    assert code == 0
    assert "dropped qualifier : 1" in out


def test_main_should_not_flag_qualifier_when_condition_rephrased(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test governed-clause precision on rephrased conditions.

    Given:
        A quote conditioned with "If True" and a claim that carries the
        condition as "set to True" without the word "if".
    When:
        main() lints the claim.
    Then:
        It should not flag a dropped qualifier.
    """
    # Arrange
    f = claims_file(tmp_path, [row(
        "c1",
        "keepalive set to True sends keepalive pings even when there are no active RPCs.",
        "If True, send keepalive pings even when there are no active RPCs.",
    )])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys, [f])

    # Assert
    assert "dropped qualifier : 0" in out


def test_main_should_flag_one_sided_split_when_no_sibling_carries_other_half(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test detection of a mangled coordination.

    Given:
        A coordinated quote whose claim keeps one side, with no sibling claim
        in the doc unit carrying the dropped side.
    When:
        main() lints the claim.
    Then:
        It should report one one-sided-split flag naming the dropped words.
    """
    # Arrange
    f = claims_file(tmp_path, [row(
        "c1", "WorkerLike.start starts the worker.",
        "Start the worker and register it with the pool.",
    )])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys, [f])

    # Assert
    assert "one-sided split   : 1" in out
    assert "no sibling claim carries it" in out


def test_main_should_suppress_split_when_sibling_carries_other_half(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test sibling suppression — a proper split is two one-sided claims.

    Given:
        The same one-sided claim plus a sibling claim in the same doc unit
        carrying the dropped side of the coordination.
    When:
        main() lints the claims.
    Then:
        It should not flag either claim.
    """
    # Arrange
    f = claims_file(tmp_path, [
        row("c1", "WorkerLike.start starts the worker.",
            "Start the worker and register it with the pool."),
        row("c2", "The pool registers the worker after start returns.",
            "Start the worker and register it with the pool."),
    ])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys, [f])

    # Assert
    assert "one-sided split   : 0" in out


def test_main_should_not_flag_split_when_claim_itself_coordinates(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the own-coordination exemption.

    Given:
        A claim that reproduces the quote's coordination in full.
    When:
        main() lints the claim.
    Then:
        It should not flag a one-sided split.
    """
    # Arrange
    f = claims_file(tmp_path, [row(
        "c1", "WorkerLike.start starts the worker and registers it with the pool.",
        "Start the worker and register it with the pool.",
    )])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys, [f])

    # Assert
    assert "one-sided split   : 0" in out


def test_main_should_exit_nonzero_when_flags_exceed_max(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the CI gate on the flag count.

    Given:
        A corpus producing one flag and a --max-flags of 0, then of 1.
    When:
        main() lints the corpus with each threshold.
    Then:
        It should exit 1 above the threshold and 0 at it.
    """
    # Arrange
    f = claims_file(tmp_path, [row(
        "c1", "WorkerLike.start starts the worker.",
        "Start the worker and register it with the pool.",
    )])

    # Act
    over, _ = run_main(lint_claims, monkeypatch, capsys, [f, "--max-flags", "0"])
    at, _ = run_main(lint_claims, monkeypatch, capsys, [f, "--max-flags", "1"])

    # Assert
    assert over == 1
    assert at == 0


def test_main_should_restrict_to_verdict_when_grounded_file_supplied(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the verdict restriction.

    Given:
        Two flaggable claims, a grounded artifact marking only one of them
        `overstated`, and --verdicts pointing at that artifact.
    When:
        main() lints with the default --verdict overstated.
    Then:
        It should lint only the overstated claim.
    """
    # Arrange
    f = claims_file(tmp_path, [
        row("c1", "quorum_timeout is meaningful.",
            "Only meaningful when quorum is a positive integer"),
        row("c2", "WorkerLike.start starts the worker.",
            "Start the worker and register it with the pool.", unit="docunit:u2"),
    ])
    grounded = write_json(tmp_path / "grounded.json", {"claims": [
        {"claimId": "claim:c1", "verdict": "overstated"},
        {"claimId": "claim:c2", "verdict": "supported"},
    ]})

    # Act
    _, out = run_main(
        lint_claims, monkeypatch, capsys, [f, "--verdicts", grounded]
    )

    # Assert
    assert "1 claims (restricted to verdict `overstated`)" in out
    assert "dropped qualifier : 1" in out
    assert "one-sided split   : 0" in out


def test_main_should_count_claim_once_when_present_in_both_input_kinds(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test dedupe across the two input schemas.

    Given:
        The same claim supplied via a reconciled claims file (keyed `id`) and
        via a grounding bundle (keyed `claimId`).
    When:
        main() lints both inputs together.
    Then:
        It should count the claim once.
    """
    # Arrange
    f = claims_file(tmp_path, [row(
        "c1", "Pool.spawn spawns workers.", "Spawns workers"
    )])
    bundles = tmp_path / "bundles"
    write_json(bundles / "ground-t.json", {"units": [{
        "docUnitId": UNIT,
        "claims": [{"claimId": "claim:c1",
                    "claimText": "Pool.spawn spawns workers.",
                    "sourceQuote": "Spawns workers"}],
    }]})

    # Act
    _, out = run_main(
        lint_claims, monkeypatch, capsys, [f, "--bundles", bundles]
    )

    # Assert
    assert "lint_claims: 1 claims" in out
