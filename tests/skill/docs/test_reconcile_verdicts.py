"""Tests for reconcile_verdicts.py — evidence verification and consensus.

Every behavior here traces to a failure that was silent in the wool PoC:
fabricated-evidence acceptance, the 45 valid citations rejected for a
single-element span, the global-vs-per-bundle unanimity bug, and the rungs of
the downgrade ladder that decide whether a finding survives as a scope note.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tests.skill.docs.conftest import (
    MIRROR_SOURCE,
    evidence,
    make_bundle,
    make_claim,
    make_pass,
    verdict_entry,
    write_json,
)

GOOD_LINES = [6, 7]
GOOD_CODE = "if count == 0:\n    count = cpu_count()"
LIMIT_CODE = "return [Worker() for _ in range(count)]"
# The payload a legacy four-verdict pass emitted. `asWritten` and `readerImpact`
# are ignored now; `asSupported` becomes the scope note.
LEGACY_OVERSTATEMENT = {
    "asWritten": "spawns the requested quantity",
    "asSupported": "spawns the requested quantity unless it is zero",
    "readerImpact": "A reader passing 0 gets cpu-count workers, never fewer.",
}
NARROWED = LEGACY_OVERSTATEMENT["asSupported"]


# ── verify_evidence ───────────────────────────────────────────────────────


def test_verify_evidence_should_accept_when_quote_matches_lines(
    reconcile_verdicts, mirror
):
    """Test citation verification on an exact match.

    Given:
        A mirror file and a citation quoting its lines 6-7 verbatim.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=True.
    """
    # Act
    ok, reason = reconcile_verdicts.verify_evidence(
        evidence(GOOD_LINES, GOOD_CODE), mirror
    )

    # Assert
    assert (ok, reason) == (True, "ok")


def test_verify_evidence_should_reject_when_quote_absent_from_lines(
    reconcile_verdicts, mirror
):
    """Test rejection of a fabricated citation.

    Given:
        A citation whose quoted code does not appear near the cited lines.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=False with a reason counting the missing lines.
    """
    # Act
    ok, reason = reconcile_verdicts.verify_evidence(
        evidence(GOOD_LINES, "return worker.halt()"), mirror
    )

    # Assert
    assert not ok
    assert "1/1 quoted lines" in reason


def test_verify_evidence_should_reject_when_range_outside_file(
    reconcile_verdicts, mirror
):
    """Test the line-range bounds check.

    Given:
        A citation naming lines beyond the end of the file.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=False.
    """
    # Act
    ok, reason = reconcile_verdicts.verify_evidence(
        evidence([40, 42], GOOD_CODE), mirror
    )

    # Assert
    assert not ok
    assert "outside file" in reason


def test_verify_evidence_should_accept_when_span_is_single_element(
    reconcile_verdicts, mirror
):
    """Test the single-element span normalisation.

    Given:
        A citation whose span is written [6] rather than [6, 6].
    When:
        verify_evidence checks the citation.
    Then:
        It should treat the span as one line and return ok=True.
    """
    # Act
    ok, _ = reconcile_verdicts.verify_evidence(
        evidence([6], "if count == 0:"), mirror
    )

    # Assert
    assert ok


def test_verify_evidence_should_reject_when_quote_contains_stripped_marker(
    reconcile_verdicts, mirror
):
    """Test the anti-circularity guard on quoted docstring markers.

    Given:
        A citation whose quote contains the stripped-docstring marker.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=False.
    """
    # Act
    ok, reason = reconcile_verdicts.verify_evidence(
        evidence(GOOD_LINES, '"""<docstring removed>"""'), mirror
    )

    # Assert
    assert not ok
    assert "marker" in reason


def test_verify_evidence_should_reject_when_quote_is_only_stub_body(
    reconcile_verdicts, mirror
):
    """Test the stub-body guard.

    Given:
        A citation quoting only a bare `...` stub body.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=False.
    """
    # Act
    ok, reason = reconcile_verdicts.verify_evidence(
        evidence([17], "..."), mirror
    )

    # Assert
    assert not ok
    assert "stub" in reason


def test_verify_evidence_should_accept_when_lines_off_within_slop(
    reconcile_verdicts, mirror
):
    """Test the small-slop tolerance for off-by-a-little citations.

    Given:
        A citation two lines above where its quoted code actually sits.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=True.
    """
    # Act
    ok, _ = reconcile_verdicts.verify_evidence(
        evidence([4, 5], "if count == 0:"), mirror
    )

    # Assert
    assert ok


@pytest.mark.parametrize(
    "item",
    [
        evidence(GOOD_LINES, GOOD_CODE, file="pkg/nope.py"),
        {**evidence(GOOD_LINES, GOOD_CODE), "lines": "6-7"},
        {**evidence(GOOD_LINES, GOOD_CODE), "lines": [7, "8"]},
        {**evidence(GOOD_LINES, GOOD_CODE), "file": ""},
        {**evidence(GOOD_LINES, GOOD_CODE), "code": "   "},
    ],
    ids=["missing-file", "string-span", "mixed-span", "empty-file", "empty-quote"],
)
def test_verify_evidence_should_reject_when_input_malformed(
    reconcile_verdicts, mirror, item
):
    """Test input hygiene on malformed citations.

    Given:
        A citation with a missing file, malformed span, or empty quote.
    When:
        verify_evidence checks the citation.
    Then:
        It should return ok=False.
    """
    # Act
    ok, _ = reconcile_verdicts.verify_evidence(item, mirror)

    # Assert
    assert not ok


@settings(max_examples=50)
@given(data=st.data())
def test_verify_evidence_should_accept_any_verbatim_span(
    reconcile_verdicts, tmp_path_factory, data
):
    """Test verification soundness over arbitrary spans.

    Given:
        Any contiguous line span of the mirror file, quoted verbatim, with at
        least one non-blank, non-stub line.
    When:
        verify_evidence checks the citation built from that span.
    Then:
        It should return ok=True.
    """
    # Arrange
    root = tmp_path_factory.mktemp("mirror-pbt")
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "pool.py").write_text(MIRROR_SOURCE)
    raw = MIRROR_SOURCE.splitlines()
    start = data.draw(st.integers(min_value=1, max_value=len(raw)))
    end = data.draw(st.integers(min_value=start, max_value=len(raw)))
    code = "\n".join(raw[start - 1 : end])
    quoted = [l.strip() for l in code.splitlines() if l.strip()]
    assume(quoted and not all(q in {"...", "pass"} for q in quoted))

    # Act
    ok, reason = reconcile_verdicts.verify_evidence(
        evidence([start, end], code), root
    )

    # Assert
    assert ok, reason


# ── scope_note ────────────────────────────────────────────────────────────


def test_scope_note_should_return_narrowing_and_bound_when_both_present(
    reconcile_verdicts
):
    """Test scope-note construction from a vote and its verified bounds.

    Given:
        A vote carrying an asSupported narrowing and a verified limiting citation.
    When:
        scope_note builds the note.
    Then:
        It should return the narrowing together with the citation.
    """
    # Arrange
    bound = evidence([8], LIMIT_CODE, role="limits")

    # Act
    note = reconcile_verdicts.scope_note(
        {"overstatement": LEGACY_OVERSTATEMENT}, [bound]
    )

    # Assert
    assert note["asSupported"] == NARROWED
    assert note["bound"] == [{"file": "pkg/pool.py", "lines": [8], "role": "limits"}]


@pytest.mark.parametrize(
    ("vote", "bounds", "why"),
    [
        ({"overstatement": LEGACY_OVERSTATEMENT}, [], "no verified bound"),
        ({"overstatement": {"asSupported": "   "}}, ["b"], "blank narrowing"),
        ({"overstatement": {}}, ["b"], "no narrowing"),
        ({}, ["b"], "no payload at all"),
        ({"overstatement": "not a dict"}, ["b"], "wrong payload type"),
    ],
    ids=["no-bound", "blank", "no-narrowing", "no-payload", "wrong-type"],
)
def test_scope_note_should_return_none_when_incomplete(
    reconcile_verdicts, vote, bounds, why
):
    """Test that an incomplete scope note is dropped rather than half-built.

    Given:
        A vote missing either its narrowing or its verified limiting citation.
    When:
        scope_note builds the note.
    Then:
        It should return None, because an uncited narrowing is an opinion.
    """
    # Act & assert
    assert reconcile_verdicts.scope_note(vote, bounds) is None, why


def test_scope_note_should_accept_the_current_field_name(reconcile_verdicts):
    """Test that a current-taxonomy vote needs no legacy key.

    Given:
        A vote carrying its narrowing under scopeNote rather than overstatement.
    When:
        scope_note builds the note.
    Then:
        It should read the current field.
    """
    # Act
    note = reconcile_verdicts.scope_note(
        {"scopeNote": {"asSupported": NARROWED}},
        [evidence([8], LIMIT_CODE, role="limits")],
    )

    # Assert
    assert note["asSupported"] == NARROWED


# ── main() — consensus pipeline ───────────────────────────────────────────


def run_main(module, tmp_path, monkeypatch, mirror, pass_files, extra=()):
    """Invoke reconcile_verdicts.main() on a tmp corpus; return the artifact."""
    out = tmp_path / "out"
    argv = [
        "reconcile_verdicts.py",
        *[str(p) for p in pass_files],
        "--bundles", str(tmp_path / "bundles"),
        "--mirror", str(mirror),
        "--out", str(out),
        "--label", "t",
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    artifact = json.loads((out / "grounded-t.json").read_text())
    report = (out / "grounding-report-t.md").read_text()
    return artifact, report


def two_pass_corpus(tmp_path, verdict_a, verdict_b, claim_id="c1"):
    """One bundle, one claim, two passes with the given verdict entries."""
    bundles = tmp_path / "bundles"
    make_bundle(bundles, "b1", [make_claim(claim_id, "Pool.spawn spawns workers.")])
    passes = tmp_path / "verdicts"
    return [
        make_pass(passes, "b1-pass1", [verdict_a]),
        make_pass(passes, "b1-pass2", [verdict_b]),
    ]


def test_main_should_report_unanimous_when_passes_agree(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test the consensus happy path.

    Given:
        Two passes agreeing on `supported` with verified evidence.
    When:
        main() reconciles the corpus.
    Then:
        It should record agreement `unanimous` and keep the verdict.
    """
    # Arrange
    entry = verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)])
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert claim["agreement"] == "unanimous"


def test_main_should_display_contradicted_when_split_ties(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test deterministic tie-breaking on a 1-1 split.

    Given:
        One pass voting `supported` and one voting `contradicted`, both with
        verified evidence.
    When:
        main() reconciles the corpus.
    Then:
        It should display `contradicted` (TIE_PRIORITY surfaces the finding)
        with agreement `split`.
    """
    # Arrange
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)]),
        verdict_entry("c1", "contradicted", [evidence(GOOD_LINES, GOOD_CODE)]),
    )

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "contradicted"
    assert claim["agreement"] == "split"


def test_main_should_fold_legacy_overstated_and_keep_the_bound(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that a legacy overstated vote survives as a supported scope note.

    Given:
        Two passes voting `overstated` under the withdrawn four-verdict
        taxonomy, each citing a verified supports item, a verified limits item,
        and a narrowing.
    When:
        main() reconciles the corpus.
    Then:
        It should record `supported` and keep the narrowing and its citation as
        a scope note, so the finding is not lost with the label.
    """
    # Arrange
    entry = verdict_entry(
        "c1",
        "overstated",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        overstatement=LEGACY_OVERSTATEMENT,
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert claim["scopeNote"]["asSupported"] == NARROWED
    assert claim["scopeNote"]["bound"][0]["role"] == "limits"
    assert artifact["stats"]["foldedOverstated"] == 2
    assert artifact["stats"]["scopeNotes"] == 1


def test_main_should_fold_legacy_overstated_without_a_note_when_bound_missing(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that an uncited narrowing is dropped rather than recorded.

    Given:
        Two passes voting `overstated` with verified supports evidence but no
        limits-role citation.
    When:
        main() reconciles the corpus.
    Then:
        It should record `supported` with no scope note — a narrowing nobody
        cited is an opinion, and the fold is silent about it.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "overstated", [evidence(GOOD_LINES, GOOD_CODE)],
        overstatement=LEGACY_OVERSTATEMENT,
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert claim["scopeNote"] is None
    assert artifact["stats"]["foldedOverstated"] == 2
    assert artifact["stats"]["scopeNotes"] == 0


def test_main_should_downgrade_to_unverifiable_when_evidence_fails(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test the evidence-required rung of the ladder.

    Given:
        Two passes voting `supported` whose only citation quotes code that is
        not in the mirror.
    When:
        main() reconciles the corpus.
    Then:
        It should downgrade to `unverifiable` and record downgrades entries.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "supported", [evidence(GOOD_LINES, "return worker.halt()")]
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "unverifiable"
    assert artifact["stats"]["downgraded"] == 2


def test_main_should_accept_a_current_taxonomy_scope_note(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test the path a current-taxonomy grader takes.

    Given:
        Two passes voting `supported` and attaching a scopeNote with a verified
        limits citation — no legacy verdict involved.
    When:
        main() reconciles the corpus.
    Then:
        It should keep `supported`, record the scope note, and report nothing
        folded, since no legacy verdict was seen.
    """
    # Arrange
    entry = verdict_entry(
        "c1",
        "supported",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
    )
    entry["scopeNote"] = {"asSupported": NARROWED}
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert claim["scopeNote"]["asSupported"] == NARROWED
    assert artifact["stats"]["foldedOverstated"] == 0


def test_main_should_override_split_when_tiebreak_supplied(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test tie-break adjudication.

    Given:
        A 1-1 split and a --tiebreak file adjudicating the claim `supported`
        with verified evidence.
    When:
        main() reconciles with the tie-break.
    Then:
        It should replace the verdict and mark agreement `adjudicated`.
    """
    # Arrange
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)]),
        verdict_entry("c1", "contradicted", [evidence(GOOD_LINES, GOOD_CODE)]),
    )
    tb = write_json(tmp_path / "tiebreak.json", {
        "verdicts": [verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)])],
    })

    # Act
    artifact, _ = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert claim["agreement"] == "adjudicated"
    assert artifact["stats"]["adjudicated"] == 1


def test_main_should_downgrade_tiebreak_when_its_evidence_fails(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that the adjudicator is held to the same evidence ladder.

    Given:
        A 1-1 split and a --tiebreak vote whose citation does not verify.
    When:
        main() reconciles with the tie-break.
    Then:
        It should downgrade the adjudicated verdict to `unverifiable`.
    """
    # Arrange
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)]),
        verdict_entry("c1", "contradicted", [evidence(GOOD_LINES, GOOD_CODE)]),
    )
    tb = write_json(tmp_path / "tiebreak.json", {
        "verdicts": [verdict_entry(
            "c1", "contradicted", [evidence(GOOD_LINES, "return worker.halt()")]
        )],
    })

    # Act
    artifact, _ = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "unverifiable"
    assert any(d["pass"] == "tiebreak" for d in artifact["downgrades"])


def test_main_should_compute_unanimity_per_bundle_when_multiple_bundles(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test the per-bundle unanimity denominator.

    Given:
        Two bundles with one claim each, covered by two passes apiece — four
        pass files in total.
    When:
        main() reconciles the corpus.
    Then:
        It should mark both claims unanimous at 2/2 votes rather than majority
        against the global pass count of 4.
    """
    # Arrange
    bundles = tmp_path / "bundles"
    make_bundle(bundles, "b1", [make_claim("c1", "Pool.spawn spawns workers.")])
    make_bundle(bundles, "b2", [make_claim("c2", "Pool.stop stops workers.")],
                qualname="Pool.stop")
    passes = tmp_path / "verdicts"
    e1 = verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)])
    e2 = verdict_entry("c2", "supported", [evidence([11, 12],
        "for worker in self._workers:\n    worker.cancel(timeout)")])
    files = [
        make_pass(passes, "b1-pass1", [e1]),
        make_pass(passes, "b1-pass2", [e1]),
        make_pass(passes, "b2-pass1", [e2]),
        make_pass(passes, "b2-pass2", [e2]),
    ]

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    assert [c["agreement"] for c in artifact["claims"]] == ["unanimous", "unanimous"]
    assert artifact["stats"]["agreement"] == {"unanimous": 2}


def test_main_should_render_the_scope_note_in_the_report(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test report rendering of a located boundary.

    Given:
        A supported claim carrying a scope note.
    When:
        main() writes the grounding report.
    Then:
        It should render the narrowing under its own section, so a triaging
        reader sees where the claim stops holding.
    """
    # Arrange
    entry = verdict_entry(
        "c1",
        "supported",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
    )
    entry["scopeNote"] = {"asSupported": NARROWED}
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    _, report = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    assert "## Scope notes" in report
    assert f"holds as far as: {NARROWED}" in report


# ── the fold ladder and its ledgers ───────────────────────────────────────


def test_main_should_record_a_legacy_vote_whose_evidence_all_fails(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that a legacy verdict with no verifiable evidence is not lost.

    Given:
        Two passes voting the withdrawn `overstated` verdict whose only citation
        quotes code absent from the mirror.
    When:
        main() reconciles the corpus.
    Then:
        It should resolve to `unverifiable` and record the vote in both ledgers
        — the label was legacy and its evidence failed, and `foldedOverstated`
        is the stale-prompt detector, so it must not read zero for exactly the
        votes whose citations were worst.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "overstated", [evidence(GOOD_LINES, "return worker.halt()")],
        overstatement=LEGACY_OVERSTATEMENT,
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "unverifiable"
    assert artifact["stats"]["foldedOverstated"] == 2
    assert artifact["stats"]["downgraded"] == 2
    assert all(f["to"] == "unverifiable" for f in artifact["foldedOverstated"])


def test_main_should_fold_a_legacy_tiebreak_vote_and_keep_its_bound(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test the tie-break path's fold of a withdrawn verdict.

    Given:
        A split claim and a --tiebreak vote of `overstated` carrying verified
        supports and limits citations plus a narrowing.
    When:
        main() reconciles with the tie-break.
    Then:
        It should record `supported`, mark the claim adjudicated, attach the
        scope note, and attribute the fold to the tie-break pass.
    """
    # Arrange
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)]),
        verdict_entry("c1", "contradicted", [evidence(GOOD_LINES, GOOD_CODE)]),
    )
    tb = write_json(tmp_path / "tiebreak.json", {"verdicts": [verdict_entry(
        "c1", "overstated",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        overstatement=LEGACY_OVERSTATEMENT,
    )]})

    # Act
    artifact, _ = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert claim["agreement"] == "adjudicated"
    assert claim["scopeNote"]["asSupported"] == NARROWED
    assert [f["pass"] for f in artifact["foldedOverstated"]] == ["tiebreak"]


def test_main_should_not_discard_a_legacy_tiebreak_vote_when_evidence_fails(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that an adjudication is never dropped without a trace.

    Given:
        A split claim and a --tiebreak vote of `overstated` whose only citation
        is fabricated.
    When:
        main() reconciles with the tie-break.
    Then:
        It should apply the adjudication as `unverifiable` and count it, rather
        than silently skipping the row and leaving the split standing.
    """
    # Arrange
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)]),
        verdict_entry("c1", "contradicted", [evidence(GOOD_LINES, GOOD_CODE)]),
    )
    tb = write_json(tmp_path / "tiebreak.json", {"verdicts": [verdict_entry(
        "c1", "overstated", [evidence(GOOD_LINES, "return worker.halt()")],
        overstatement=LEGACY_OVERSTATEMENT,
    )]})

    # Act
    artifact, _ = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "unverifiable"
    assert claim["agreement"] == "adjudicated"
    assert artifact["stats"]["adjudicated"] == 1


def test_main_should_prefer_the_vote_that_wrote_the_narrowing(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that note presence outranks bound count when picking a vote.

    Given:
        Two agreeing `supported` votes — one citing two verified limiting
        citations with no narrowing, one citing a single limiting citation and
        carrying the narrowing.
    When:
        main() reconciles the corpus.
    Then:
        It should keep the narrowing-bearing vote's scope note, because a bound
        without a narrowing records no finding.
    """
    # Arrange
    boundless = verdict_entry("c1", "supported", [
        evidence(GOOD_LINES, GOOD_CODE),
        evidence([8], LIMIT_CODE, role="limits"),
        evidence([11, 12], "for worker in self._workers:\n    worker.cancel(timeout)",
                 role="limits"),
    ])
    with_note = verdict_entry(
        "c1", "supported",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        scope_note={"asSupported": NARROWED},
    )
    files = two_pass_corpus(tmp_path, boundless, with_note)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["scopeNote"]["asSupported"] == NARROWED
    assert artifact["stats"]["scopeNotes"] == 1


def test_main_should_not_attach_a_scope_note_to_a_contradicted_claim(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that only a claim which holds can carry a boundary.

    Given:
        Two passes voting `contradicted`, citing a `contradicts`-role item —
        which counts as a bound — and carrying a narrowing.
    When:
        main() reconciles the corpus.
    Then:
        It should attach no scope note, since a note renders under a heading
        that reads "claims that hold" and this claim does not.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "contradicted",
        [evidence(GOOD_LINES, GOOD_CODE),
         evidence([8], LIMIT_CODE, role="contradicts")],
        scope_note={"asSupported": NARROWED},
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "contradicted"
    assert claim["scopeNote"] is None
    assert artifact["stats"]["scopeNotes"] == 0


def test_main_should_keep_the_consensus_note_when_the_adjudicator_omits_one(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that adjudication does not blank an established boundary.

    Given:
        A consensus `supported` claim carrying a scope note, adjudicated
        `supported` by a tie-break vote that carries no narrowing.
    When:
        main() reconciles with the tie-break.
    Then:
        It should preserve the existing scope note.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "supported",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        scope_note={"asSupported": NARROWED},
    )
    files = two_pass_corpus(tmp_path, entry, entry)
    tb = write_json(tmp_path / "tiebreak.json", {"verdicts": [
        verdict_entry("c1", "supported", [evidence(GOOD_LINES, GOOD_CODE)]),
    ]})

    # Act
    artifact, _ = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["scopeNote"]["asSupported"] == NARROWED


def test_main_should_drop_the_consensus_note_when_the_adjudicator_contradicts(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that an overturned claim does not keep its boundary.

    Given:
        A consensus `supported` claim carrying a scope note, adjudicated
        `contradicted` by a tie-break vote citing a limiting item.
    When:
        main() reconciles with the tie-break.
    Then:
        It should carry no scope note and count none, because a claim the
        adjudicator ruled a defect cannot also hold as far as anything.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "supported",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        scope_note={"asSupported": NARROWED},
    )
    files = two_pass_corpus(tmp_path, entry, entry)
    tb = write_json(tmp_path / "tiebreak.json", {"verdicts": [
        verdict_entry("c1", "contradicted",
                      [evidence([8], LIMIT_CODE, role="contradicts")]),
    ]})

    # Act
    artifact, report = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "contradicted"
    assert claim["scopeNote"] is None
    assert artifact["stats"]["scopeNotes"] == 0
    assert NARROWED not in report.split("## Scope notes")[1]


def test_main_should_adjudicate_when_the_tiebreak_verdict_is_unrecognised(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that no adjudication is discarded for its label alone.

    Given:
        A split claim and a tie-break vote whose verdict string is outside the
        taxonomy but whose citation verifies.
    When:
        main() reconciles with the tie-break.
    Then:
        It should coerce the verdict to `unverifiable`, mark the row
        adjudicated, and record the original in downgrades — the evidence was
        already counted, so dropping the row would leave the ledgers
        disagreeing about whether it was processed.
    """
    # Arrange
    ev = [evidence(GOOD_LINES, GOOD_CODE)]
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", ev),
        verdict_entry("c1", "contradicted", ev),
    )
    tb = write_json(tmp_path / "tiebreak.json", {"verdicts": [
        verdict_entry("c1", "partially-supported", ev),
    ]})

    # Act
    artifact, _ = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "unverifiable"
    assert claim["agreement"] == "adjudicated"
    assert artifact["stats"]["adjudicated"] == 1
    assert [d for d in artifact["downgrades"]
            if d["from"] == "partially-supported"
            and d["reason"] == "unrecognised verdict"]


def test_main_should_render_adjudicated_rows_in_the_agreement_table(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that the report accounts for every claim after a tie-break.

    Given:
        A split claim resolved by a tie-break vote.
    When:
        main() writes the grounding report.
    Then:
        The agreement table should carry an `adjudicated` row, so its rows
        still sum to the claim total rather than silently under-counting.
    """
    # Arrange
    ev = [evidence(GOOD_LINES, GOOD_CODE)]
    files = two_pass_corpus(
        tmp_path,
        verdict_entry("c1", "supported", ev),
        verdict_entry("c1", "contradicted", ev),
    )
    tb = write_json(tmp_path / "tiebreak.json", {"verdicts": [
        verdict_entry("c1", "supported", ev),
    ]})

    # Act
    artifact, report = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files,
        extra=("--tiebreak", str(tb)),
    )

    # Assert — the agreement section only; later sections carry tables too.
    table = report.split("## Verdict agreement across passes")[1].split("## ")[0]
    rows = dict(re.findall(r"^\| (\w+) \| (\d+) \|", table, re.M))
    assert rows.get("adjudicated") == "1"
    assert sum(int(n) for n in rows.values()) == artifact["stats"]["claims"]


def test_main_should_name_both_arms_of_the_fold_in_the_report(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that the fold summary does not claim an outcome it did not produce.

    Given:
        Two legacy `overstated` votes whose every citation fails verification,
        so both fold to `unverifiable` rather than `supported`.
    When:
        main() writes the grounding report.
    Then:
        The report should say so, and the by-outcome breakdown should record
        no `supported` — telling a reader a bound survived as a scope note
        when nothing survived is the failure this replaces.
    """
    # Arrange
    entry = verdict_entry("c1", "overstated", [evidence(GOOD_LINES, "not in the mirror")])
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, report = run_main(
        reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    by_outcome = artifact["stats"]["foldedOverstatedByOutcome"]
    assert by_outcome == {"unverifiable": 2}
    assert "2 to `unverifiable`" in report or "2 to `unverifiable" in report
    assert "0 to `supported`" in report


def test_scope_note_should_prefer_the_current_key_over_the_legacy_one(
    reconcile_verdicts
):
    """Test key precedence when a vote carries both payloads.

    Given:
        A vote carrying a current `scopeNote` and a legacy `overstatement` whose
        narrowings differ, plus a verified bound.
    When:
        scope_note builds the note.
    Then:
        It should use the current key's narrowing.
    """
    # Arrange
    vote = {"scopeNote": {"asSupported": "current wording"},
            "overstatement": LEGACY_OVERSTATEMENT}

    # Act
    note = reconcile_verdicts.scope_note(
        vote, [evidence([8], LIMIT_CODE, role="limits")], "supported"
    )

    # Assert
    assert note["asSupported"] == "current wording"


def test_scope_note_should_return_none_when_the_verdict_does_not_hold(
    reconcile_verdicts
):
    """Test the verdict guard on note construction.

    Given:
        A complete narrowing and a verified bound, on a claim the graders did
        not mark `supported`.
    When:
        scope_note builds the note for `contradicted` and for `unverifiable`.
    Then:
        It should return None for both — nothing was established, so nothing
        can be bounded.
    """
    # Arrange
    bounds = [evidence([8], LIMIT_CODE, role="limits")]
    vote = {"scopeNote": {"asSupported": NARROWED}}

    # Act & assert
    assert reconcile_verdicts.scope_note(vote, bounds, "contradicted") is None
    assert reconcile_verdicts.scope_note(vote, bounds, "unverifiable") is None


def test_VERDICTS_should_be_consistent_with_the_other_verdict_constants(
    reconcile_verdicts
):
    """Test the module-level verdict contract.

    Given:
        The loaded reconciler module.
    When:
        Its verdict constants are compared.
    Then:
        TIE_PRIORITY should cover exactly VERDICTS, EVIDENCE_REQUIRED should be
        a subset of it, legacy labels should be disjoint from it, and the
        withdrawn harm-test helper should be gone.
    """
    # Act
    verdicts = set(reconcile_verdicts.VERDICTS)

    # Assert
    assert set(reconcile_verdicts.TIE_PRIORITY) == verdicts
    assert len(reconcile_verdicts.TIE_PRIORITY) == len(verdicts)
    assert set(reconcile_verdicts.EVIDENCE_REQUIRED) <= verdicts
    assert not set(reconcile_verdicts.LEGACY_VERDICTS) & verdicts
    assert not hasattr(reconcile_verdicts, "has_impact")


# ── fold invariants (property-based) ──────────────────────────────────────
#
# Hypothesis re-runs the body without resetting function-scoped fixtures, so
# these build their own tmp corpus and argv context, matching the pattern in
# test_reconcile_claims.py.

VOTE_SHAPES = st.sampled_from(["supported", "contradicted", "unverifiable"])
ANY_VERDICT = st.sampled_from(
    ["supported", "contradicted", "unverifiable", "overstated",
     "OVERSTATED", " supported", "partially-supported", ""]
)


def reconcile_in_tmp(module, mirror_src, entries_a, entries_b, tiebreak=None):
    """Run main() over a fresh one-claim, two-pass corpus. Returns the artifact."""
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as mp:
        tmp = Path(td)
        (tmp / "mirror" / "pkg").mkdir(parents=True)
        (tmp / "mirror" / "pkg" / "pool.py").write_text(mirror_src)
        bundles = tmp / "bundles"
        make_bundle(bundles, "b1", [make_claim("c1", "Pool.spawn spawns workers.")])
        passes = tmp / "verdicts"
        files = [make_pass(passes, "b1-pass1", entries_a),
                 make_pass(passes, "b1-pass2", entries_b)]
        out = tmp / "out"
        extra = []
        if tiebreak is not None:
            extra = ["--tiebreak",
                     str(write_json(tmp / "tiebreak.json", {"verdicts": tiebreak}))]
        mp.setattr(sys, "argv", [
            "reconcile_verdicts.py", *[str(f) for f in files],
            "--bundles", str(bundles), "--mirror", str(tmp / "mirror"),
            "--out", str(out), "--label", "t", *extra,
        ])
        assert module.main() == 0
        return json.loads((out / "grounded-t.json").read_text())


@settings(max_examples=30, deadline=None)
@given(verdict=ANY_VERDICT)
def test_main_should_apply_every_tiebreak_vote_whatever_its_verdict_string(
    reconcile_verdicts, verdict
):
    """Test that the adjudication path never discards a vote for its label.

    Given:
        A split claim and a tie-break vote carrying any verdict string an agent
        could emit — including junk, wrong case, and the withdrawn label.
    When:
        main() reconciles with the tie-break.
    Then:
        The row should always come back adjudicated with a verdict in the
        taxonomy. The pass path already had this guarantee; driving the same
        strategy through the tie-break is what would have caught the `continue`
        that discarded an adjudicator's answer outright.
    """
    # Arrange
    ev = [evidence(GOOD_LINES, GOOD_CODE)]

    # Act
    artifact = reconcile_in_tmp(
        reconcile_verdicts, MIRROR_SOURCE,
        [verdict_entry("c1", "supported", ev)],
        [verdict_entry("c1", "contradicted", ev)],
        tiebreak=[verdict_entry("c1", verdict, ev)],
    )

    # Assert
    (claim,) = artifact["claims"]
    assert claim["agreement"] == "adjudicated"
    assert claim["verdict"] in reconcile_verdicts.VERDICTS
    assert artifact["stats"]["adjudicated"] == 1


@settings(max_examples=30, deadline=None)
@given(verdict=VOTE_SHAPES, second=VOTE_SHAPES)
def test_main_should_report_no_folds_when_no_vote_is_legacy(
    reconcile_verdicts, verdict, second
):
    """Test that the fold is invisible to claims it does not concern.

    Given:
        Any pair of current-taxonomy verdicts, each citing verified evidence.
    When:
        main() reconciles the corpus.
    Then:
        It should report no folds and no scope notes, and every emitted verdict
        should be one the graders actually cast.
    """
    # Arrange
    ev = [evidence(GOOD_LINES, GOOD_CODE)]

    # Act
    artifact = reconcile_in_tmp(
        reconcile_verdicts, MIRROR_SOURCE,
        [verdict_entry("c1", verdict, ev)],
        [verdict_entry("c1", second, ev)],
    )

    # Assert
    assert artifact["stats"]["foldedOverstated"] == 0
    assert artifact["stats"]["scopeNotes"] == 0
    assert artifact["claims"][0]["verdict"] in {verdict, second}


@settings(max_examples=30, deadline=None)
@given(bound_first=st.booleans(), narrowing=st.text(min_size=1, max_size=40))
def test_main_should_preserve_evidence_across_the_fold(
    reconcile_verdicts, bound_first, narrowing
):
    """Test that folding rewrites labels and never touches citations.

    Given:
        A corpus authored with the withdrawn `overstated` verdict, and the same
        corpus with every such label rewritten to `supported` and everything
        else — citations, ordering, narrowing — left identical.
    When:
        main() reconciles both.
    Then:
        The evidence on the claim, the scope note, and the evidence-check counts
        should be identical; only the fold counter differs.
    """
    # Arrange
    ev = [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")]
    if bound_first:
        ev = list(reversed(ev))
    note = {"asSupported": narrowing}

    def entries(label):
        return [verdict_entry("c1", label, ev, scope_note=note)]

    # Act
    legacy = reconcile_in_tmp(reconcile_verdicts, MIRROR_SOURCE,
                              entries("overstated"), entries("overstated"))
    current = reconcile_in_tmp(reconcile_verdicts, MIRROR_SOURCE,
                               entries("supported"), entries("supported"))

    # Assert
    assert legacy["claims"][0]["evidence"] == current["claims"][0]["evidence"]
    assert legacy["claims"][0]["scopeNote"] == current["claims"][0]["scopeNote"]
    assert legacy["stats"]["evidenceChecks"] == current["stats"]["evidenceChecks"]
    assert legacy["stats"]["foldedOverstated"] == 2
    assert current["stats"]["foldedOverstated"] == 0


@settings(max_examples=40, deadline=None)
@given(verdict=ANY_VERDICT, verifiable=st.booleans())
def test_main_should_emit_only_known_verdicts_for_any_vote_string(
    reconcile_verdicts, verdict, verifiable
):
    """Test the coercion that guards every downstream verdict lookup.

    Given:
        Any verdict string an agent could emit — current, withdrawn, wrongly
        cased, unknown, or empty — with citations that either verify or do not.
    When:
        main() reconciles the corpus.
    Then:
        It should never raise, and every emitted verdict and vote-count key
        should be one the taxonomy declares.
    """
    # Arrange
    code = GOOD_CODE if verifiable else "return worker.halt()"
    entries = [verdict_entry("c1", verdict, [evidence(GOOD_LINES, code)])]

    # Act
    artifact = reconcile_in_tmp(reconcile_verdicts, MIRROR_SOURCE, entries, entries)

    # Assert
    known = set(reconcile_verdicts.VERDICTS)
    (claim,) = artifact["claims"]
    assert claim["verdict"] in known
    assert set(claim["voteCounts"]) <= known
