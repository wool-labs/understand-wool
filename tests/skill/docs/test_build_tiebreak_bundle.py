"""Tests for build_tiebreak_bundle.py — packaging disagreements for adjudication.

Two contracts matter: only genuinely disputed claims are sent (adjudication is
the expensive step), and `disagreementShapes` is written into the bundle so the
adjudication prompt can describe its own input instead of carrying hard-coded
run counts — the defect that made the first tie-break prompt single-use.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.skill.docs.conftest import (
    make_bundle,
    make_claim,
    make_pass,
    verdict_entry,
    write_json,
)


def corpus(tmp_path: Path, votes: dict[str, list[str]]) -> dict:
    """One bundle holding every claim in `votes`; one pass file per vote slot.

    `votes` maps claim id -> list of verdicts, one per pass. Missing entries in
    later passes are simply absent from that pass file.
    """
    bundles = tmp_path / "bundles"
    make_bundle(bundles, "b1",
                [make_claim(cid, f"claim {cid}") for cid in votes])
    n_passes = max(len(v) for v in votes.values())
    passes = tmp_path / "verdicts"
    for i in range(n_passes):
        entries = [verdict_entry(cid, v[i]) for cid, v in votes.items() if i < len(v)]
        make_pass(passes, f"b1-pass{i + 1}", entries)
    return {"bundles": bundles, "verdicts": passes}


def run_main(module, monkeypatch, tmp_path, dirs, extra=()):
    out = tmp_path / "tb"
    argv = ["build_tiebreak_bundle.py",
            "--verdicts", str(dirs["verdicts"]),
            "--bundles", str(dirs["bundles"]),
            "--out", str(out), *extra]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    files = sorted(out.glob("tiebreak-*.json"))
    return [json.loads(p.read_text()) for p in files]


def test_main_should_package_only_disputed_claims(
    build_tiebreak_bundle, monkeypatch, tmp_path
):
    """Test dispute detection.

    Given:
        One claim the two passes disagree on and one they agree on.
    When:
        main() builds the tie-break bundle.
    Then:
        It should package only the disputed claim, with its shape recorded.
    """
    # Arrange
    dirs = corpus(tmp_path, {
        "c1": ["supported", "contradicted"],
        "c2": ["supported", "supported"],
    })

    # Act
    (bundle,) = run_main(build_tiebreak_bundle, monkeypatch, tmp_path, dirs)

    # Assert
    (claim,) = bundle["claims"]
    assert claim["claimId"] == "claim:c1"
    assert claim["shape"] == "contradicted vs supported"
    assert claim["priorVerdicts"][0]["verdict"] in ("supported", "contradicted")


def test_main_should_write_disagreement_shapes_into_bundle(
    build_tiebreak_bundle, monkeypatch, tmp_path
):
    """Test the shape summary the adjudication prompt reads.

    Given:
        Two disputed claims with different disagreement shapes.
    When:
        main() builds the tie-break bundle.
    Then:
        It should write a disagreementShapes counter into the bundle file.
    """
    # Arrange
    dirs = corpus(tmp_path, {
        "c1": ["supported", "contradicted"],
        "c2": ["unverifiable", "supported"],
    })

    # Act
    (bundle,) = run_main(build_tiebreak_bundle, monkeypatch, tmp_path, dirs)

    # Assert
    assert bundle["disagreementShapes"] == {
        "contradicted vs supported": 1,
        "supported vs unverifiable": 1,
    }
    assert bundle["claimCount"] == 2


def test_main_should_force_unanimous_claim_when_listed_in_also_claims(
    build_tiebreak_bundle, monkeypatch, tmp_path
):
    """Test the escape hatch for a claim the passes agreed on.

    Given:
        A claim both passes unanimously call supported, whose ID is passed via
        --also-claims.
    When:
        main() builds the tie-break bundle.
    Then:
        It should include the claim, marked as forced rather than disputed, so
        the adjudicator knows what it is being asked.
    """
    # Arrange
    dirs = corpus(tmp_path, {"c1": ["supported", "supported"]})
    sel = write_json(tmp_path / "also.json", ["claim:c1"])

    # Act
    (bundle,) = run_main(
        build_tiebreak_bundle, monkeypatch, tmp_path, dirs,
        extra=("--also-claims", str(sel)),
    )

    # Assert
    (claim,) = bundle["claims"]
    assert claim["shape"] == "supported (forced for review)"


def test_main_should_ignore_a_non_list_also_claims_payload(
    build_tiebreak_bundle, monkeypatch, tmp_path
):
    """Test that an unexpected --also-claims shape forces nothing.

    Given:
        --also-claims pointing at a JSON object rather than a list of IDs.
    When:
        main() builds the tie-break bundle over claims the passes agreed on.
    Then:
        It should force nothing in, rather than guessing at a key.
    """
    # Arrange
    dirs = corpus(tmp_path, {"c1": ["supported", "supported"]})
    sel = write_json(tmp_path / "grounded.json", {"claims": ["claim:c1"]})

    # Act
    bundles = run_main(
        build_tiebreak_bundle, monkeypatch, tmp_path, dirs,
        extra=("--also-claims", str(sel)),
    )

    # Assert
    assert bundles == []


def test_main_should_exclude_claim_when_it_has_a_single_vote(
    build_tiebreak_bundle, monkeypatch, tmp_path, capsys
):
    """Test the coverage guard on incomplete bundles.

    Given:
        A claim voted on by only one pass.
    When:
        main() builds the tie-break bundle.
    Then:
        It should produce no tie-break file and report the incomplete count.
    """
    # Arrange
    dirs = corpus(tmp_path, {"c1": ["supported"]})

    # Act
    bundles = run_main(build_tiebreak_bundle, monkeypatch, tmp_path, dirs)
    out = capsys.readouterr().out

    # Assert
    assert bundles == []
    assert "only one vote" in out
