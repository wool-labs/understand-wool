"""Tests for design_lens.py — the second hop of the scope-note wire.

This stage had no test module at all, and it is where the predecessor payload
survived while the stage after it dropped the field for months. Both facts point
the same way: what matters here is not that the stage works in isolation, but
that the field it carries is the one the next stage reads.

The truncation is the specific hazard. `project()` keeps three evidence items;
without bound-role-first ordering the limiting citation — the entire content of
a scope note — is the item most likely to fall off the end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.skill.docs.conftest import (
    CLAIM_ID,
    grounded_claim,
    make_claims_file,
    make_grounded_file,
    reconciled_claim,
)

NARROWED = "spawns the requested quantity unless it is zero"
NOTE = {"asSupported": NARROWED,
        "bound": [{"file": "pkg/pool.py", "lines": [8], "role": "limits"}]}


def run_main(module, monkeypatch, tmp_path, grounded: list[dict]):
    """Run design_lens.main() over a one-claim corpus; return the design record."""
    claims = make_claims_file(tmp_path / "claims-pool.json", [reconciled_claim()])
    out = tmp_path / "out"
    argv = ["design_lens.py", str(claims), "--out", str(out)]
    if grounded is not None:
        argv += ["--grounded", str(make_grounded_file(tmp_path / "g.json", grounded))]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    payload = json.loads((out / "design-claims.json").read_text())
    return next(c for c in payload["claims"] if c["claimId"] == CLAIM_ID)


def test_main_should_carry_the_scope_note_into_the_design_record(
    design_lens, monkeypatch, tmp_path
):
    """Test that the scope note survives the design-lens hop.

    Given:
        A grounded claim carrying a scope note.
    When:
        main() writes design-claims.json.
    Then:
        The record's grounding should carry the note verbatim — this is the
        exact field the emitter reads, and the hop where its predecessor was
        silently dropped.
    """
    # Arrange
    grounded = [grounded_claim(scope_note=NOTE)]

    # Act
    record = run_main(design_lens, monkeypatch, tmp_path, grounded)

    # Assert
    assert record["grounding"]["scopeNote"] == NOTE


def test_main_should_omit_the_scope_note_when_the_claim_has_none(
    design_lens, monkeypatch, tmp_path
):
    """Test the negative control at this hop.

    Given:
        A grounded claim with a verdict and evidence but no scope note.
    When:
        main() writes design-claims.json.
    Then:
        It should omit the key entirely rather than carry a falsy value, which
        a downstream truthiness check would read as a note.
    """
    # Act
    record = run_main(design_lens, monkeypatch, tmp_path, [grounded_claim()])

    # Assert
    assert "scopeNote" not in record["grounding"]
    assert record["grounding"]["verdict"] == "supported"


def test_main_should_keep_the_limiting_citation_within_the_truncation(
    design_lens, monkeypatch, tmp_path
):
    """Test that the bound cannot be truncated away.

    Given:
        A grounded claim with four supporting citations and one limiting
        citation listed last, against a projection that keeps only three.
    When:
        main() projects the evidence.
    Then:
        The limiting citation should be retained and ranked first, with its role
        intact — without the role no consumer can tell a bound from a support.
    """
    # Arrange
    ev = [{"file": "pkg/pool.py", "lines": [i, i], "role": "supports"}
          for i in range(1, 5)]
    ev.append({"file": "pkg/pool.py", "lines": [8], "role": "limits"})

    # Act
    record = run_main(design_lens, monkeypatch, tmp_path,
                      [grounded_claim(ev=ev, scope_note=NOTE)])

    # Assert
    projected = record["grounding"]["evidence"]
    assert len(projected) == 3
    assert projected[0]["role"] == "limits"
    assert projected[0]["lines"] == [8]


def test_main_should_leave_grounding_null_when_no_grounded_file_covers_the_claim(
    design_lens, monkeypatch, tmp_path
):
    """Test the join between claim sets and grounding artifacts.

    Given:
        A grounded artifact whose claim ID matches nothing in the claim set.
    When:
        main() writes design-claims.json.
    Then:
        The record's grounding should be null rather than a partial object — a
        mismatched join must read as absent, not as an empty verdict.
    """
    # Act
    record = run_main(design_lens, monkeypatch, tmp_path,
                      [grounded_claim(cid="claim:other", scope_note=NOTE)])

    # Assert
    assert record["grounding"] is None
