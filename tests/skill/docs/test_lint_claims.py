"""Tests for lint_claims.py — extraction-artifact detection.

The checks guard the seam between extraction and grounding: a claim that
asserts more than its docstring did makes grounding report a defect the
documentation never committed. Precision matters as much as recall here — the
two claim-against-quote checks carry suppressions (governed-clause overlap,
sibling claims), and those suppressions are what the first group of tests pins
down. The second group covers the check that the quote is in the docstring at
all, which the other two take for granted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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
        `contradicted`, and --verdicts pointing at that artifact.
    When:
        main() lints with the default --verdict contradicted.
    Then:
        It should lint only the contradicted claim.
    """
    # Arrange
    f = claims_file(tmp_path, [
        row("c1", "quorum_timeout is meaningful.",
            "Only meaningful when quorum is a positive integer"),
        row("c2", "WorkerLike.start starts the worker.",
            "Start the worker and register it with the pool.", unit="docunit:u2"),
    ])
    grounded = write_json(tmp_path / "grounded.json", {"claims": [
        {"claimId": "claim:c1", "verdict": "contradicted"},
        {"claimId": "claim:c2", "verdict": "supported"},
    ]})

    # Act
    _, out = run_main(
        lint_claims, monkeypatch, capsys, [f, "--verdicts", grounded]
    )

    # Assert
    assert "1 claims (restricted to verdict `contradicted`)" in out
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


# ── the sourceQuote check ─────────────────────────────────────────────────

UNIT_TEXT = ("Spawn the requested number of workers.\n\n"
             "A count of zero means one worker\n    per CPU, via ``os.cpu_count``.")


def doc_units_file(tmp_path: Path, text: str = UNIT_TEXT) -> Path:
    return write_json(tmp_path / "doc-units.json",
                      {"docUnits": [{"id": UNIT, "text": text}]})


@pytest.mark.parametrize(
    "quote",
    ["Spawn the requested number of workers",
     "one worker per CPU",
     "one worker\n    per CPU, via ``os.cpu_count``"],
    ids=["one-line", "across-a-line-break", "wrapped-as-written"],
)
def test_main_should_accept_a_quote_that_is_in_the_doc_unit(
    lint_claims, monkeypatch, capsys, tmp_path, quote
):
    """Test that a verbatim quote passes, however the docstring wraps it.

    Given:
        A claim whose quote appears in its doc unit's text, on one line or
        across a line break.
    When:
        main() lints the claim with --doc-units.
    Then:
        It should report no quote missing from its unit.
    """
    # Arrange
    f = claims_file(tmp_path, [row("a", "Pool.spawn spawns workers.", quote)])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys,
                         [f, "--doc-units", doc_units_file(tmp_path)])

    # Assert
    assert code == 0
    assert "quote not in unit : 0" in out


@pytest.mark.parametrize(
    ("quote", "unit", "why"),
    [
        ("Spawn exactly the requested number of workers", UNIT,
         "the quote is not in the doc unit's text"),
        ("spawn the requested number of workers", UNIT,
         "the quote is not in the doc unit's text"),
        ("via os.cpu_count", UNIT, "the quote is not in the doc unit's text"),
        ("Spawn the requested number of workers", "docunit:elsewhere",
         "doc unit docunit:elsewhere is not in --doc-units"),
        ("   ", UNIT, "the claim has no sourceQuote"),
    ],
    ids=["reworded", "case-changed", "markup-dropped", "unknown-unit", "empty"],
)
def test_main_should_flag_a_quote_that_is_not_in_the_doc_unit(
    lint_claims, monkeypatch, capsys, tmp_path, quote, unit, why
):
    """Test each way a quote can fail to be what the docstring said.

    Given:
        A claim whose quote rewords the docstring, changes its case, drops its
        markup, names a doc unit the file does not hold, or is empty.
    When:
        main() lints the claim with --doc-units.
    Then:
        It should count one quote missing from its unit and say why.
    """
    # Arrange
    f = claims_file(tmp_path, [row("a", "Pool.spawn spawns workers.", quote, unit)])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys,
                      [f, "--doc-units", doc_units_file(tmp_path)])

    # Assert
    assert "quote not in unit : 1" in out
    assert f"QUOTE {why}" in out


def test_main_should_exit_nonzero_when_any_quote_is_missing(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that the quote check fails a run on its own.

    Given:
        One claim whose quote is not in its doc unit and no other flag.
    When:
        main() lints it with --doc-units and no --max-flags.
    Then:
        It should exit 1 and say how many quotes are missing.
    """
    # Arrange
    f = claims_file(tmp_path, [row("a", "Pool.spawn spawns workers.", "invented text")])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys,
                         [f, "--doc-units", doc_units_file(tmp_path)])

    # Assert
    assert code == 1
    assert "FAIL: 1 sourceQuote(s) not in their doc unit" in out


def test_main_should_not_count_review_flags_as_failures_without_max_flags(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that the two review checks do not fail a run on their own.

    Given:
        One claim with a verbatim quote that drops the quote's qualifier.
    When:
        main() lints it with --doc-units and no --max-flags.
    Then:
        It should flag the dropped qualifier and exit 0.
    """
    # Arrange
    text = "Raises KeyError when the worker is missing from the registry."
    f = claims_file(tmp_path, [row("a", "Pool.get raises KeyError.", text)])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys,
                         [f, "--doc-units", doc_units_file(tmp_path, text)])

    # Assert
    assert code == 0
    assert "quote not in unit : 0" in out
    assert "dropped qualifier : 1" in out


def test_main_should_print_the_quote_count_before_the_review_counts(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the order of the count lines.

    Given:
        One clean claim.
    When:
        main() lints it with --doc-units.
    Then:
        It should print the quote count first of the three count lines.
    """
    # Arrange
    f = claims_file(tmp_path, [row("a", "Pool.spawn spawns workers.",
                                   "Spawn the requested number of workers")])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys,
                      [f, "--doc-units", doc_units_file(tmp_path)])

    # Assert
    lines = [line.strip() for line in out.splitlines()]
    assert lines[1:4] == ["quote not in unit : 0", "dropped qualifier : 0",
                          "one-sided split   : 0"]


def test_main_should_say_the_quote_check_did_not_run_without_doc_units(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that an unchecked quote is not reported as a passing one.

    Given:
        A claim whose quote is invented.
    When:
        main() lints it without --doc-units.
    Then:
        It should say the quote check was not run, and report no count for it.
    """
    # Arrange
    f = claims_file(tmp_path, [row("a", "Pool.spawn spawns workers.", "invented text")])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys, [f])

    # Assert
    assert code == 0
    assert "quote not in unit : not checked (pass --doc-units)" in out


def test_main_should_check_quotes_of_claims_read_from_bundles(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the quote check on the bundle input form.

    Given:
        A grounding bundle holding one claim with a verbatim quote and one
        with an invented quote.
    When:
        main() lints the bundle directory with --doc-units.
    Then:
        It should flag the invented quote only.
    """
    # Arrange
    bundles = tmp_path / "bundles"
    write_json(bundles / "ground-t-1.json", {"units": [{"docUnitId": UNIT, "claims": [
        {"claimId": "claim:a", "claimText": "Pool.spawn spawns workers.",
         "sourceQuote": "Spawn the requested number of workers"},
        {"claimId": "claim:b", "claimText": "Pool.spawn retries twice.",
         "sourceQuote": "retries twice"},
    ]}]})

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys,
                      ["--bundles", bundles, "--doc-units", doc_units_file(tmp_path)])

    # Assert
    assert "quote not in unit : 1" in out
    assert "claim: Pool.spawn retries twice." in out


# ── which files are a claim set ───────────────────────────────────────────

SPLIT_QUOTE = "Start the worker and register it with the pool"


def test_main_should_skip_the_reconcilers_rejected_and_excluded_files(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that a glob over the claims directory does not mask a real flag.

    Given:
        A survivors file with one claim that kept one half of a coordination,
        and a rejected file, marked as the reconciler marks it, whose claim
        carries the other half.
    When:
        main() lints both files.
    Then:
        It should skip the rejected file with a notice on stderr, lint one
        claim, and still flag the one-sided split.
    """
    # Arrange
    survivors = claims_file(tmp_path, [row("a", "Worker.start starts the worker.",
                                           SPLIT_QUOTE)])
    rejected = write_json(tmp_path / "claims-rejected-t.json", {
        "note": "sub-quorum; retained deliberately",
        "claims": [row("b", "Worker.start registers the worker with the pool.",
                       SPLIT_QUOTE)]})
    monkeypatch.setattr(sys, "argv", ["lint_claims.py", str(survivors), str(rejected)])

    # Act
    code = lint_claims.main()

    # Assert
    captured = capsys.readouterr()
    assert code == 0
    assert "SKIPPED claims-rejected-t.json" in captured.err
    assert "lint_claims: 1 claims" in captured.out
    assert "one-sided split   : 1" in captured.out


def test_main_should_not_take_a_sibling_from_another_input_file(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that a sibling is a claim from the same claim set.

    Given:
        Two claim files about one doc unit, each holding one half of a
        coordination.
    When:
        main() lints both files together.
    Then:
        It should flag the one-sided split in each, since neither file holds
        the other half.
    """
    # Arrange
    first = claims_file(tmp_path, [row("a", "Worker.start starts the worker.",
                                       SPLIT_QUOTE)], name="claims-one.json")
    second = claims_file(tmp_path, [row("b", "The pool registers it.",
                                        SPLIT_QUOTE)], name="claims-two.json")

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys, [first, second])

    # Assert
    assert "one-sided split   : 2" in out


# ── --quotes-only, for extraction pass files ──────────────────────────────


def pass_file(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    """An extraction pass file: claims with no id yet."""
    return write_json(tmp_path / name, {"claims": [
        {k: v for k, v in r.items() if k != "id"} for r in rows]})


def test_main_should_check_every_passes_quote_when_passes_word_a_claim_alike(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that the quote check is not de-duplicated across passes.

    Given:
        Two pass files stating the same claim in the same words, one with a
        verbatim quote and one with an invented quote.
    When:
        main() checks them with --quotes-only.
    Then:
        It should check both claims, flag the invented quote naming its pass
        file, skip the other two checks, and exit 1.
    """
    # Arrange
    claim = "Pool.spawn spawns workers."
    good = pass_file(tmp_path, "claims-t-pass1.json",
                     [row("x", claim, "Spawn the requested number of workers")])
    bad = pass_file(tmp_path, "claims-t-pass2.json", [row("x", claim, "spawns N workers")])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys, [
        good, bad, "--doc-units", doc_units_file(tmp_path), "--quotes-only"])

    # Assert
    assert code == 1
    assert "lint_claims: 2 claims" in out
    assert "quote not in unit : 1" in out
    assert "[claims-t-pass2.json]" in out
    assert "dropped qualifier : not checked (--quotes-only)" in out


def test_main_should_check_both_claims_when_two_doc_units_share_a_wording(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the identity of a claim that has no id.

    Given:
        One pass file with the same claim text on two doc units, one quote
        verbatim in its unit and one not in its unit.
    When:
        main() checks it with --quotes-only.
    Then:
        It should check both claims and flag one.
    """
    # Arrange
    units = write_json(tmp_path / "doc-units.json", {"docUnits": [
        {"id": "docunit:u1", "text": "Return the worker metadata."},
        {"id": "docunit:u2", "text": "Give back what is known of the worker."},
    ]})
    quote = "Return the worker metadata"
    f = pass_file(tmp_path, "claims-t-pass1.json", [
        row("x", "metadata returns the worker metadata.", quote, "docunit:u1"),
        row("y", "metadata returns the worker metadata.", quote, "docunit:u2"),
    ])

    # Act
    _, out = run_main(lint_claims, monkeypatch, capsys,
                      [f, "--doc-units", units, "--quotes-only"])

    # Assert
    assert "lint_claims: 2 claims" in out
    assert "quote not in unit : 1" in out


def test_main_should_refuse_quotes_only_without_doc_units(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test the one argument combination that could check nothing.

    Given:
        A claims file.
    When:
        main() runs with --quotes-only and no --doc-units.
    Then:
        It should exit 2 without linting.
    """
    # Arrange
    f = claims_file(tmp_path, [row("a", "Pool.spawn spawns workers.", "anything")])

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys, [f, "--quotes-only"])

    # Assert
    assert code == 2
    assert "lint_claims:" not in out


@pytest.mark.parametrize(
    "case", ["duplicate-wording", "note-key", "elided-quote"],
)
def test_main_should_not_let_a_pass_files_quote_go_unchecked(
    lint_claims, monkeypatch, capsys, tmp_path, case
):
    """Test the ways a quote could slip past the gate.

    Given:
        A pass file holding a verbatim quote and a bad one, where the bad one
        is on a second claim worded like the first, or the file carries a
        top-level `note` key, or the quote joins two parts of the docstring
        with an ellipsis.
    When:
        main() checks the file with --quotes-only.
    Then:
        It should read both claims, flag the bad quote, and exit 1.
    """
    # Arrange
    good = row("x", "Pool.spawn spawns workers.", "Spawn the requested number of workers")
    bad_quote = ("Spawn ... workers" if case == "elided-quote" else "spawns N workers")
    bad_text = good["claimText"] if case == "duplicate-wording" else "Pool.spawn is fast."
    bad = row("y", bad_text, bad_quote)
    f = pass_file(tmp_path, "claims-t-pass1.json", [good, bad])
    if case == "note-key":
        payload = {"note": "written by the agent", "claims": [
            {k: v for k, v in r.items() if k != "id"} for r in (good, bad)]}
        write_json(f, payload)

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys, [
        f, "--doc-units", doc_units_file(tmp_path), "--quotes-only"])

    # Assert
    assert code == 1
    assert "claims-t-pass1.json: 2 claims" in out
    assert "quote not in unit : 1" in out


def test_main_should_fail_the_gate_when_a_pass_file_holds_no_claims(
    lint_claims, monkeypatch, capsys, tmp_path
):
    """Test that an unreadable pass is not counted as a checked one.

    Given:
        One pass file with a verbatim quote and one with no claims.
    When:
        main() checks both with --quotes-only.
    Then:
        It should list both files with their claim counts, name the empty
        one as unchecked, and exit 1.
    """
    # Arrange
    good = pass_file(tmp_path, "claims-t-pass1.json", [
        row("x", "Pool.spawn spawns workers.", "Spawn the requested number of workers")])
    empty = write_json(tmp_path / "claims-t-pass2.json", {"claims": []})

    # Act
    code, out = run_main(lint_claims, monkeypatch, capsys, [
        good, empty, "--doc-units", doc_units_file(tmp_path), "--quotes-only"])

    # Assert
    assert code == 1
    assert "claims-t-pass1.json: 1 claims" in out
    assert "claims-t-pass2.json: 0 claims" in out
    assert "FAIL: no claims read from claims-t-pass2.json" in out
