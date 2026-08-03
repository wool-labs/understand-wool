"""Tests for build_grounding_bundles.py — grounding-target routing.

The classification (direct / implementor / unresolved) decides what a grounding
agent is told about a symbol. The costliest historical failure here was silent:
a swallowed SyntaxError turned 27 verifiable claims into `unresolved` and the
agents were told there was no code to check. BG-006 pins the loud version.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.skill.docs.conftest import write_json

SOURCE = '''\
from typing import Protocol


class WorkerLike(Protocol):
    """Contract for workers."""

    def start(self):
        """Start the worker."""
        ...


class Pool:
    """A pool."""

    def spawn(self, count):
        """Spawn workers."""
        if count == 0:
            count = 4
        return count

    def probe(self):
        """Probe health."""
        ...
'''


def corpus(tmp_path: Path, *, source: str = SOURCE) -> dict:
    """Write a one-file source tree plus doc units and claims for it."""
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "pool.py").write_text(source)

    def unit(uid: str, kind: str, qualname: str) -> dict:
        return {
            "id": f"docunit:{uid}",
            "sourceFile": "pkg/pool.py",
            "sourceLine": 1,
            "docLineRange": [1, 1],
            "attachedKind": kind,
            "attachedTo": f"{kind}:pkg/pool.py:{qualname}",
            "text": "irrelevant",
        }

    units = write_json(tmp_path / "doc-units.json", {"docUnits": [
        unit("u-spawn", "function", "Pool.spawn"),
        unit("u-proto", "class", "WorkerLike"),
        unit("u-probe", "function", "Pool.probe"),
        unit("u-ghost", "function", "Pool.vanish"),
    ]})

    def claim(cid: str, uid: str) -> dict:
        return {
            "id": f"claim:{cid}",
            "docUnitId": f"docunit:{uid}",
            "claimText": f"claim {cid}",
            "claimType": "factual",
            "quantifier": "particular",
            "sourceQuote": "quote",
            "passCount": 3,
        }

    claims = write_json(tmp_path / "claims.json", {"claims": [
        claim("c-spawn", "u-spawn"),
        claim("c-proto", "u-proto"),
        claim("c-probe", "u-probe"),
        claim("c-ghost", "u-ghost"),
    ]})
    return {"src": src, "units": units, "claims": claims}


def run_main(module, monkeypatch, tmp_path, files, extra=()):
    out = tmp_path / "bundles"
    argv = ["build_grounding_bundles.py",
            "--claims", str(files["claims"]),
            "--doc-units", str(files["units"]),
            "--src-root", str(files["src"]),
            "--label", "t", "--out", str(out), *extra]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == 0
    bundles = [json.loads(p.read_text()) for p in sorted(out.glob("ground-t-*.json"))]
    targets = {}
    for b in bundles:
        for u in b["units"]:
            for c in u["claims"]:
                targets[c["claimId"]] = u["groundingTarget"]
    return bundles, targets


def test_main_should_classify_direct_when_symbol_has_body(
    build_grounding_bundles, monkeypatch, tmp_path
):
    """Test routing of a documented symbol with an executable body.

    Given:
        A claim attached to Pool.spawn, whose body has real statements.
    When:
        main() builds the bundles.
    Then:
        It should classify the claim's unit as groundingTarget `direct`.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-spawn"] == "direct"


def test_main_should_classify_implementor_when_docstring_on_protocol(
    build_grounding_bundles, monkeypatch, tmp_path
):
    """Test routing of a Protocol-level docstring.

    Given:
        A claim attached to the WorkerLike Protocol class.
    When:
        main() builds the bundles.
    Then:
        It should classify the unit as `implementor` — behaviour lives in
        implementors, not at the declaration site.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-proto"] == "implementor"


def test_main_should_classify_implementor_when_body_is_stub(
    build_grounding_bundles, monkeypatch, tmp_path
):
    """Test routing of a stub-bodied method.

    Given:
        A claim attached to Pool.probe, whose body is a bare `...`.
    When:
        main() builds the bundles.
    Then:
        It should classify the unit as `implementor`.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-probe"] == "implementor"


def test_main_should_classify_unresolved_when_symbol_absent(
    build_grounding_bundles, monkeypatch, tmp_path
):
    """Test routing of a claim whose symbol does not exist.

    Given:
        A claim attached to Pool.vanish, which is not in the source.
    When:
        main() builds the bundles.
    Then:
        It should classify the unit as `unresolved`.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-ghost"] == "unresolved"


def test_main_should_filter_and_report_when_only_claims_supplied(
    build_grounding_bundles, monkeypatch, capsys, tmp_path
):
    """Test the blind-subset builder.

    Given:
        An --only-claims list naming one existing claim (records keyed `id`)
        and one unknown ID.
    When:
        main() builds the bundles.
    Then:
        It should bundle only the selected claim and print the missing ID.
    """
    # Arrange
    files = corpus(tmp_path)
    sel = write_json(tmp_path / "subset.json", ["claim:c-spawn", "claim:c-unknown"])

    # Act
    _, targets = run_main(
        build_grounding_bundles, monkeypatch, tmp_path, files,
        extra=("--only-claims", str(sel)),
    )
    out = capsys.readouterr().out

    # Assert
    assert set(targets) == {"claim:c-spawn"}
    assert "1 of 4 claims selected" in out
    assert "MISSING claim:c-unknown" in out


def test_main_should_warn_loudly_when_source_fails_to_parse(
    build_grounding_bundles, monkeypatch, capsys, tmp_path
):
    """Test the loud-parse regression — a silent SyntaxError misroutes claims.

    Given:
        A source file that does not parse.
    When:
        main() builds the bundles.
    Then:
        It should print a PARSE FAILED warning naming the file and the running
        interpreter version, and classify the claims `unresolved`.
    """
    # Arrange
    files = corpus(tmp_path, source="def broken(:\n    pass\n")

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)
    err = capsys.readouterr().err

    # Assert
    assert "PARSE FAILED" in err
    assert "pool.py" in err
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in err
    assert set(targets.values()) == {"unresolved"}


def test_main_should_emit_every_claim_once_when_bundles_split(
    build_grounding_bundles, monkeypatch, tmp_path
):
    """Test bundle splitting under --max-claims.

    Given:
        Four claims and a --max-claims of 2.
    When:
        main() builds the bundles.
    Then:
        It should produce multiple bundles that together carry every claim
        exactly once.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    bundles, targets = run_main(
        build_grounding_bundles, monkeypatch, tmp_path, files,
        extra=("--max-claims", "2"),
    )

    # Assert
    assert len(bundles) >= 2
    assert len(targets) == 4  # dict keyed by claimId — presence proves uniqueness
    counts = [c["claimId"] for b in bundles for u in b["units"] for c in u["claims"]]
    assert len(counts) == 4
