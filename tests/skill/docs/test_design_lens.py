"""Tests for design_lens.py — the second hop of the scope-note wire.

This step had no test module at all, and it is where the predecessor payload
survived while the step after it dropped the field for months. Both facts point
the same way: what matters here is not that the step works in isolation, but
that the field it carries is the one the next step reads.

The truncation is the specific hazard. `project()` keeps three evidence items;
without bound-role-first ordering the limiting citation — the entire content of
a scope note — is the item most likely to fall off the end.
"""

from __future__ import annotations

import json
import sys

import pytest

from tests.skill.docs.conftest import (
    CLAIM_ID,
    grounded_claim,
    make_claims_file,
    make_grounded_file,
    reconciled_claim,
    write_json,
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


DESIGN_TEXTS = [
    *(f"{name}Pool owns the workers it spawns." for name in ("Alpha", "Beta", "Gamma", "Delta")),
    *(f"{name}Pool always keeps one worker alive." for name in ("Alpha", "Beta", "Gamma", "Delta")),
    "AlphaPool keeps one worker alive because startup is slow.",
]


def run_bundled(module, monkeypatch, tmp_path, extra=()):
    """Run design_lens.main() over a corpus that yields symbol and rationale blocks."""
    claims = make_claims_file(tmp_path / "claims-pool.json", [
        reconciled_claim(cid=f"claim:d{i}", text=text, unit=f"docunit:d{i}")
        for i, text in enumerate(DESIGN_TEXTS)])
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["design_lens.py", str(claims), "--out", str(out), *extra])
    assert module.main() == 0
    blocks = json.loads((out / "design-blocks.json").read_text())["blocks"]
    return out, blocks


def named(block: dict) -> set[str]:
    return set(block["claimIds"]) | ({block["anchorClaimId"]} if "anchorClaimId" in block else set())


@pytest.mark.parametrize("cap", [1, 3, 4, 100])
def test_main_should_pack_the_blocks_into_bundles_under_a_claim_cap(
    design_lens, monkeypatch, tmp_path, cap
):
    """Test the step-13 bundles written with --claims-per-bundle.

    Given:
        Claims yielding four two-claim symbol blocks and one rationale block.
    When:
        main() runs with --claims-per-bundle at 1, 3, 4, or 100.
    Then:
        Every block should land in exactly one bundle, in design-blocks.json
        order; a bundle should exceed the cap only when it holds one block;
        and each bundle should hold the records of exactly the claims its
        blocks name, a rationale block's anchor included.
    """
    # Act
    out, blocks = run_bundled(design_lens, monkeypatch, tmp_path,
                              ("--claims-per-bundle", str(cap)))
    paths = sorted(out.glob("design-bundle-*.json"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    bundles = [json.loads(p.read_text()) for p in paths]

    # Assert
    assert len(blocks) == 5
    assert [bundle["bundle"] for bundle in bundles] == list(range(1, len(bundles) + 1))
    assert [b["blockId"] for bundle in bundles for b in bundle["blocks"]] == \
        [b["blockId"] for b in blocks]
    for bundle in bundles:
        held = set().union(*(named(b) for b in bundle["blocks"]))
        assert set(bundle["claims"]) == held
        assert len(held) <= cap or len(bundle["blocks"]) == 1
        for record in bundle["claims"].values():
            assert set(record) == set(design_lens.BUNDLE_FIELDS)
    if cap == 100:
        assert len(bundles) == 1


def test_main_should_remove_bundles_an_earlier_run_left(
    design_lens, monkeypatch, tmp_path
):
    """Test a re-run into a directory holding more bundles than it writes.

    Given:
        An output directory holding design-bundle-9.json from an earlier run.
    When:
        main() runs with --claims-per-bundle 100 into it.
    Then:
        It should write design-bundle-1.json alone.
    """
    # Arrange
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "design-bundle-9.json").write_text("{}")

    # Act
    out, _ = run_bundled(design_lens, monkeypatch, tmp_path, ("--claims-per-bundle", "100"))

    # Assert
    assert sorted(p.name for p in out.glob("design-bundle-*.json")) == ["design-bundle-1.json"]


def test_main_should_write_no_bundle_without_the_flag(design_lens, monkeypatch, tmp_path):
    """Test the default output.

    Given:
        Claims yielding design blocks.
    When:
        main() runs without --claims-per-bundle.
    Then:
        It should write design-blocks.json and no design-bundle file.
    """
    # Act
    out, blocks = run_bundled(design_lens, monkeypatch, tmp_path)

    # Assert
    assert blocks
    assert list(out.glob("design-bundle-*.json")) == []


def test_main_should_keep_the_subsystem_of_a_divided_subsystems_claims(
    reconcile_claims, design_lens, monkeypatch, tmp_path
):
    """Test the subsystem of claims extracted from one bundle of a divided subsystem.

    Given:
        Three pass files in the extraction prompt's output shape, named for
        the bundle label `pool-2` and carrying `subsystem: layer:pool`.
    When:
        reconcile_claims.main() reconciles them without --label and
        design_lens.main() tags the claim set it wrote.
    Then:
        The claim set should be `claims-pool-2.json`, and every design record
        should carry the subsystem `pool`, not the bundle label.
    """
    # Arrange
    claim = {"docUnitId": "docunit:u1", "claimText": "PoolA owns the workers.",
             "claimType": "factual", "quantifier": "particular",
             "sourceQuote": "owns the workers", "fieldIndex": None}
    passes = [write_json(tmp_path / f"claims-pool-2-pass{n}.json",
                         {"bundle": "pool-2", "subsystem": "layer:pool", "pass": n,
                          "claims": [claim]})
              for n in (1, 2, 3)]
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["reconcile_claims.py", *map(str, passes), "--out", str(out)])
    assert reconcile_claims.main() == 0

    # Act
    monkeypatch.setattr(sys, "argv", ["design_lens.py", str(out / "claims-pool-2.json"),
                                      "--out", str(out)])
    assert design_lens.main() == 0
    records = json.loads((out / "design-claims.json").read_text())["claims"]

    # Assert
    assert records
    assert {r["subsystem"] for r in records} == {"pool"}
