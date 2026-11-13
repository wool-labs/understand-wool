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

import pytest

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


def corpus(tmp_path: Path, *, source: str = SOURCE, rel: str = "pkg/pool.py",
           ghost: bool = False) -> dict:
    """Write a one-file source tree plus doc units and claims for it."""
    src = tmp_path / "src"
    (src / rel).parent.mkdir(parents=True)
    (src / rel).write_bytes(source if isinstance(source, bytes) else source.encode())

    def unit(uid: str, kind: str, qualname: str) -> dict:
        return {
            "id": f"docunit:{uid}",
            "sourceFile": rel,
            "sourceLine": 1,
            "docLineRange": [1, 1],
            "attachedKind": kind,
            "attachedTo": f"{kind}:{rel}:{qualname}",
            "text": "irrelevant",
        }

    units = write_json(tmp_path / "doc-units.json", {"docUnits": [
        unit("u-spawn", "function", "Pool.spawn"),
        unit("u-proto", "class", "WorkerLike"),
        unit("u-probe", "function", "Pool.probe"),
        unit("u-mod", "module", "pkg.pool"),
        *([unit("u-ghost", "function", "Pool.vanish")] if ghost else []),
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
        claim("c-mod", "u-mod"),
        *([claim("c-ghost", "u-ghost")] if ghost else []),
    ]})
    return {"src": src, "units": units, "claims": claims}


def run_main(module, monkeypatch, tmp_path, files, extra=(), code=0):
    out = tmp_path / "bundles"
    argv = ["build_grounding_bundles.py",
            "--claims", str(files["claims"]),
            "--doc-units", str(files["units"]),
            "--src-root", str(files["src"]),
            "--label", "t", "--out", str(out), *extra]
    monkeypatch.setattr(sys, "argv", argv)
    assert module.main() == code
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


def test_main_should_classify_unresolved_and_exit_zero_when_doc_unit_is_a_module(
    build_grounding_bundles, monkeypatch, capsys, tmp_path
):
    """Test the one ordinary source of `unresolved` claims.

    Given:
        A claim on the module docstring of a readable source file.
    When:
        main() builds the bundles.
    Then:
        It should classify the unit `unresolved` and exit 0 with no
        UNRESOLVED SYMBOL line.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-mod"] == "unresolved"
    assert "UNRESOLVED SYMBOL" not in capsys.readouterr().err


def test_main_should_fail_and_list_the_unit_when_a_function_is_unresolved(
    build_grounding_bundles, monkeypatch, capsys, tmp_path
):
    """Test a class or function doc unit with no definition in the source.

    Given:
        A claim attached to Pool.vanish, which is not in the source, as when
        the source changed after harvest.
    When:
        main() builds the bundles.
    Then:
        It should classify the unit `unresolved`, print an UNRESOLVED SYMBOL
        line listing the file and qualname, and exit 1.
    """
    # Arrange
    files = corpus(tmp_path, ghost=True)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files, code=1)
    err = capsys.readouterr().err

    # Assert
    assert targets["claim:c-ghost"] == "unresolved"
    assert "UNRESOLVED SYMBOL — 1 class or function doc unit(s)" in err
    assert "pkg/pool.py  Pool.vanish" in err


@pytest.mark.parametrize("source", [
    b"\xef\xbb\xbf" + SOURCE.encode(),
    b"# -*- coding: latin-1 -*-\n# caf\xe9\n" + SOURCE.encode(),
], ids=["bom", "coding-line"])
def test_main_should_read_source_as_the_harvester_does(
    build_grounding_bundles, monkeypatch, capsys, tmp_path, source
):
    """Test source files that step 1 reads through `tokenize.open`.

    Given:
        A source file with a UTF-8 byte-order mark, or a latin-1 coding line
        and a non-UTF-8 byte.
    When:
        main() builds the bundles.
    Then:
        It should classify Pool.spawn `direct` and exit 0 with no
        UNREADABLE line.
    """
    # Arrange
    files = corpus(tmp_path, source=source)

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-spawn"] == "direct"
    assert "UNREADABLE" not in capsys.readouterr().err


def test_main_should_resolve_a_symbol_in_a_file_whose_name_holds_a_colon(
    build_grounding_bundles, monkeypatch, tmp_path
):
    """Test parsing of `attachedTo` when the path contains a colon.

    Given:
        The corpus written to `pkg/odd:name.py`.
    When:
        main() builds the bundles.
    Then:
        It should classify Pool.spawn `direct` and exit 0.
    """
    # Arrange
    files = corpus(tmp_path, rel="pkg/odd:name.py")

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    assert targets["claim:c-spawn"] == "direct"


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


def test_main_should_fail_loudly_when_source_fails_to_parse(
    build_grounding_bundles, monkeypatch, capsys, tmp_path
):
    """Test the loud-parse regression — a silent SyntaxError misroutes claims.

    Given:
        A source file that does not parse.
    When:
        main() builds the bundles.
    Then:
        It should print a PARSE FAILED warning naming the file and the running
        interpreter version, classify the claims `unresolved`, print an
        UNREADABLE SOURCE line and no UNRESOLVED SYMBOL line, and exit 1.
    """
    # Arrange
    files = corpus(tmp_path, source="def broken(:\n    pass\n")

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files, code=1)
    err = capsys.readouterr().err

    # Assert
    assert "PARSE FAILED" in err
    assert "pool.py" in err
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in err
    assert "UNREADABLE SOURCE — 1 source file(s)" in err
    assert "UNRESOLVED SYMBOL" not in err
    assert set(targets.values()) == {"unresolved"}


def test_main_should_fail_when_the_source_root_is_wrong(
    build_grounding_bundles, monkeypatch, capsys, tmp_path
):
    """Test a --src-root under which no doc unit's source file exists.

    Given:
        A claims file and doc units for pkg/pool.py, and a --src-root that
        does not exist.
    When:
        main() builds the bundles.
    Then:
        It should print UNREADABLE for the file and an UNREADABLE SOURCE line
        naming the root, and exit 1.
    """
    # Arrange
    files = {**corpus(tmp_path), "src": tmp_path / "nonexistent"}

    # Act
    _, targets = run_main(build_grounding_bundles, monkeypatch, tmp_path, files, code=1)
    err = capsys.readouterr().err

    # Assert
    assert "UNREADABLE " in err
    assert f"UNREADABLE SOURCE — 1 source file(s) named by the doc units could not be read or parsed under {tmp_path / 'nonexistent'}" in err
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


# ── orphaned claims ───────────────────────────────────────────────────────


def test_main_should_record_no_orphans_when_every_claim_has_its_doc_unit(
    build_grounding_bundles, monkeypatch, tmp_path, capsys
):
    """Test the orphan record on a correctly paired run.

    Given:
        A claims file and the doc-units file it was extracted from.
    When:
        main() bundles the claims.
    Then:
        It should exit 0, print a zero orphan count, and write an orphan file
        that lists none.
    """
    # Arrange
    files = corpus(tmp_path)

    # Act
    run_main(build_grounding_bundles, monkeypatch, tmp_path, files)

    # Assert
    record = json.loads((tmp_path / "bundles" / "orphaned-t.json").read_text())
    assert (record["orphanedClaims"], record["orphaned"]) == (0, [])
    assert "orphaned        0 claims" in capsys.readouterr().out


def test_main_should_report_and_record_claims_whose_doc_unit_is_absent(
    build_grounding_bundles, monkeypatch, tmp_path, capsys
):
    """Test that a claim with no doc unit is never dropped in silence.

    Given:
        A claims file in which two of six claims name doc units the doc-units
        file does not hold.
    When:
        main() bundles the claims.
    Then:
        It should bundle the other four, exit 1, name the mismatch on stderr,
        print the orphan count, and write both orphaned claims to the orphan
        file with their ids, doc units, and text.
    """
    # Arrange
    files = corpus(tmp_path)
    payload = json.loads(files["claims"].read_text())
    for cid, uid in (("c-lost-2", "u-other"), ("c-lost-1", "u-gone")):
        payload["claims"].append({
            "id": f"claim:{cid}", "docUnitId": f"docunit:{uid}",
            "claimText": f"claim {cid}", "claimType": "factual",
            "quantifier": "particular", "sourceQuote": "quote", "passCount": 3,
        })
    files["claims"].write_text(json.dumps(payload))
    out_dir = tmp_path / "bundles"
    monkeypatch.setattr(sys, "argv", [
        "build_grounding_bundles.py", "--claims", str(files["claims"]),
        "--doc-units", str(files["units"]), "--src-root", str(files["src"]),
        "--label", "t", "--out", str(out_dir)])

    # Act
    code = build_grounding_bundles.main()

    # Assert
    captured = capsys.readouterr()
    bundled = [c["claimId"]
               for path in sorted(out_dir.glob("ground-t-*.json"))
               for unit in json.loads(path.read_text())["units"]
               for c in unit["claims"]]
    record = json.loads((out_dir / "orphaned-t.json").read_text())
    assert code == 1
    assert len(bundled) == 4
    assert "ORPHANED — 2 claim(s) name 2 doc unit(s)" in captured.err
    assert "orphaned        2 claims" in captured.out
    assert (record["orphanedClaims"], record["orphanedDocUnits"]) == (2, 2)
    assert record["orphaned"] == [
        {"claimId": "claim:c-lost-1", "docUnitId": "docunit:u-gone",
         "claimText": "claim c-lost-1"},
        {"claimId": "claim:c-lost-2", "docUnitId": "docunit:u-other",
         "claimText": "claim c-lost-2"},
    ]
