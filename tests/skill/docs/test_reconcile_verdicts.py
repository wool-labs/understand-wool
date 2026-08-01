"""Tests for reconcile_verdicts.py — evidence verification and consensus.

Every behavior here traces to a failure that was silent in the wool PoC:
fabricated-evidence acceptance, the 45 valid citations rejected for a
single-element span, the global-vs-per-bundle unanimity bug, and the
bound/impact rungs of the downgrade ladder.
"""

from __future__ import annotations

import json
import sys
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
IMPACT = {
    "asWritten": "spawns the requested quantity",
    "asSupported": "spawns the requested quantity unless it is zero",
    "readerImpact": "A reader passing 0 gets cpu-count workers, never fewer.",
}


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


# ── has_impact ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overstatement", "expected"),
    [
        (IMPACT, True),
        ({**IMPACT, "readerImpact": ""}, False),
        ({**IMPACT, "readerImpact": "   "}, False),
        ({"asWritten": "x", "asSupported": "y"}, False),
        (None, False),
        ("not a dict", False),
    ],
    ids=["filled", "empty", "whitespace", "missing-key", "none", "wrong-type"],
)
def test_has_impact_should_require_nonempty_reader_impact(
    reconcile_verdicts, overstatement, expected
):
    """Test the harm-test presence predicate.

    Given:
        Overstatement payloads with and without a written readerImpact.
    When:
        has_impact evaluates each payload.
    Then:
        It should return True only when readerImpact is present and non-blank.
    """
    # Act & assert
    assert reconcile_verdicts.has_impact(overstatement) is expected


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


def test_main_should_keep_overstated_when_bound_verified(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that two-sided evidence sustains an overstated verdict.

    Given:
        Two passes voting `overstated`, each citing a verified supports item, a
        verified limits item, and a complete overstatement object.
    When:
        main() reconciles the corpus.
    Then:
        It should keep the verdict and carry the overstatement into the artifact.
    """
    # Arrange
    entry = verdict_entry(
        "c1",
        "overstated",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        overstatement=IMPACT,
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "overstated"
    assert claim["overstatement"]["readerImpact"] == IMPACT["readerImpact"]
    assert artifact["stats"]["impactMissing"] == 0


def test_main_should_downgrade_overstated_to_supported_when_bound_missing(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test the bound-downgrade rung of the evidence ladder.

    Given:
        Two passes voting `overstated` with verified supports evidence but no
        limits-role citation.
    When:
        main() reconciles the corpus.
    Then:
        It should downgrade to `supported` and record boundDowngrades entries.
    """
    # Arrange
    entry = verdict_entry(
        "c1", "overstated", [evidence(GOOD_LINES, GOOD_CODE)], overstatement=IMPACT
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "supported"
    assert artifact["stats"]["boundDowngrades"] == 2
    assert all(d["from"] == "overstated" for d in artifact["boundDowngrades"])


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


def test_main_should_route_to_adjudication_when_reader_impact_missing(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test that a missing harm test routes rather than moves the verdict.

    Given:
        Two passes voting `overstated` with verified two-sided evidence but an
        overstatement lacking readerImpact.
    When:
        main() reconciles the corpus.
    Then:
        It should keep the verdict, list the claim in impactMissing, and count
        it in stats.
    """
    # Arrange
    entry = verdict_entry(
        "c1",
        "overstated",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        overstatement={"asWritten": "x", "asSupported": "y"},
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    artifact, _ = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    (claim,) = artifact["claims"]
    assert claim["verdict"] == "overstated"
    assert artifact["impactMissing"] == [claim["claimId"]]
    assert artifact["stats"]["impactMissing"] == 1


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


def test_main_should_render_missing_impact_note_in_report(
    reconcile_verdicts, mirror, tmp_path, monkeypatch
):
    """Test report rendering of the unanswered harm test.

    Given:
        An overstated claim whose overstatement lacks readerImpact.
    When:
        main() writes the grounding report.
    Then:
        It should render the "not stated — routed to adjudication" note.
    """
    # Arrange
    entry = verdict_entry(
        "c1",
        "overstated",
        [evidence(GOOD_LINES, GOOD_CODE), evidence([8], LIMIT_CODE, role="limits")],
        overstatement={"asWritten": "x", "asSupported": "y"},
    )
    files = two_pass_corpus(tmp_path, entry, entry)

    # Act
    _, report = run_main(reconcile_verdicts, tmp_path, monkeypatch, mirror, files)

    # Assert
    assert "not stated — routed to adjudication" in report
