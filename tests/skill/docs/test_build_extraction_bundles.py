"""Tests for build_extraction_bundles.py — the step-4 extraction input.

The division of doc units into bundles decides which doc units a pass sees,
and so which claims can reach quorum. It was done by hand on every archived
run, so these tests pin the rule that replaced the hand: subsystem membership
from scope-facts.json, whole files per bundle, and a label every later step
can name its files by.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.skill.docs.conftest import write_json


def unit(uid: str, source: str, line: int, **extra) -> dict:
    return {"id": f"docunit:{uid}", "sourceFile": source, "sourceLine": line,
            "text": "irrelevant", **extra}


def inputs(tmp_path: Path, units: list[dict], subsystems: dict[str, list[str]]):
    """Write a doc-units file and a scope-facts file; return their paths."""
    doc_units = write_json(tmp_path / "doc-units.json", {"docUnits": units})
    scope = write_json(tmp_path / "scope-facts.json", {"subsystems": [
        {"id": sid, "paths": paths} for sid, paths in subsystems.items()]})
    return doc_units, scope


def run_main(module, monkeypatch, doc_units, scope, out, extra=()):
    monkeypatch.setattr(sys, "argv", [
        "build_extraction_bundles.py", "--doc-units", str(doc_units),
        "--scope-facts", str(scope), "--out", str(out), *extra])
    return module.main()


def bundles(out: Path) -> dict[str, dict]:
    return {p.name: json.loads(p.read_text()) for p in sorted(out.glob("extract-*.json"))}


def test_main_should_write_one_bundle_per_subsystem_labelled_by_it(
    build_extraction_bundles, monkeypatch, tmp_path
):
    """Test subsystem membership and labels.

    Given:
        Doc units in three files, assigned to `layer:pool` and `layer:wire:grpc`.
    When:
        main() bundles them under the default limit.
    Then:
        It should write `extract-pool.json` and `extract-wire_grpc.json`, each
        holding its subsystem's doc units ordered by file and line, and exit 0.
    """
    # Arrange
    doc_units, scope = inputs(tmp_path, [
        unit("b", "pkg/pool.py", 9), unit("a", "pkg/pool.py", 2),
        unit("c", "pkg/grpc.py", 1), unit("d", "pkg/base.py", 4)],
        {"layer:pool": ["pkg/pool.py", "pkg/base.py"], "layer:wire:grpc": ["pkg/grpc.py"]})

    # Act
    code = run_main(build_extraction_bundles, monkeypatch, doc_units, scope, tmp_path / "out")
    written = bundles(tmp_path / "out")

    # Assert
    assert code == 0
    assert list(written) == ["extract-pool.json", "extract-wire_grpc.json"]
    pool = written["extract-pool.json"]
    assert (pool["subsystem"], pool["label"], pool["chunk"]) == ("layer:pool", "pool", "1/1")
    assert [u["id"] for u in pool["docUnits"]] == ["docunit:d", "docunit:a", "docunit:b"]
    assert written["extract-wire_grpc.json"]["label"] == "wire_grpc"


def test_main_should_divide_a_large_subsystem_by_whole_files(
    build_extraction_bundles, monkeypatch, tmp_path
):
    """Test the chunking of a subsystem over --max-units.

    Given:
        One subsystem of three files holding 2, 3, and 4 doc units, the last
        file a split docstring whose parts share a `context`, and
        --max-units 5.
    When:
        main() bundles them.
    Then:
        It should write `<sub>-1` with the first two files and `<sub>-2` with
        the third, never splitting a file.
    """
    # Arrange
    units = ([unit(f"a{i}", "pkg/a.py", i) for i in range(2)]
             + [unit(f"b{i}", "pkg/b.py", i) for i in range(3)]
             + [unit(f"c{i}", "pkg/c.py", 1, partIndex=i, partCount=4,
                     context=None if i == 0 else "Summary.") for i in range(4)])
    doc_units, scope = inputs(tmp_path, units, {"layer:pool": ["pkg/a.py", "pkg/b.py", "pkg/c.py"]})

    # Act
    code = run_main(build_extraction_bundles, monkeypatch, doc_units, scope,
                    tmp_path / "out", ("--max-units", "5"))
    written = bundles(tmp_path / "out")

    # Assert
    assert code == 0
    assert list(written) == ["extract-pool-1.json", "extract-pool-2.json"]
    first, second = written["extract-pool-1.json"], written["extract-pool-2.json"]
    assert (first["label"], first["chunk"], second["chunk"]) == ("pool-1", "1/2", "2/2")
    assert {u["sourceFile"] for u in first["docUnits"]} == {"pkg/a.py", "pkg/b.py"}
    assert [u["partIndex"] for u in second["docUnits"]] == [0, 1, 2, 3]


def test_main_should_write_identical_bundles_for_any_input_order(
    build_extraction_bundles, monkeypatch, tmp_path
):
    """Test that the division does not depend on the order of the inputs.

    Given:
        The same doc units and subsystems, listed in two different orders.
    When:
        main() bundles each into its own directory.
    Then:
        The two directories should hold byte-identical files.
    """
    # Arrange
    units = [unit(f"u{i}", f"pkg/m{i % 3}.py", i) for i in range(9)]
    subs = {"layer:a": ["pkg/m0.py", "pkg/m1.py"], "layer:b": ["pkg/m2.py"]}
    (tmp_path / "x").mkdir()
    (tmp_path / "y").mkdir()
    forward = inputs(tmp_path / "x", units, subs)
    backward = inputs(tmp_path / "y", units[::-1], dict(reversed(subs.items())))

    # Act
    run_main(build_extraction_bundles, monkeypatch, *forward, tmp_path / "out-x", ("--max-units", "4"))
    run_main(build_extraction_bundles, monkeypatch, *backward, tmp_path / "out-y", ("--max-units", "4"))

    # Assert
    x = {p.name: p.read_bytes() for p in (tmp_path / "out-x").iterdir()}
    y = {p.name: p.read_bytes() for p in (tmp_path / "out-y").iterdir()}
    assert x == y


def test_main_should_fail_and_name_a_file_in_no_subsystem(
    build_extraction_bundles, monkeypatch, tmp_path, capsys
):
    """Test a doc unit whose file scope-facts.json does not assign.

    Given:
        Doc units in `pkg/pool.py`, assigned, and `pkg/stray.py`, not.
    When:
        main() bundles them.
    Then:
        It should bundle the assigned doc unit, print an UNASSIGNED line naming
        `pkg/stray.py` and both causes with their remedies, and exit 1.
    """
    # Arrange
    doc_units, scope = inputs(tmp_path, [unit("a", "pkg/pool.py", 1), unit("b", "pkg/stray.py", 1)],
                              {"layer:pool": ["pkg/pool.py"]})

    # Act
    code = run_main(build_extraction_bundles, monkeypatch, doc_units, scope, tmp_path / "out")
    err = capsys.readouterr().err

    # Assert
    assert code == 1
    assert err.startswith("UNASSIGNED — 1 source file(s)")
    assert "pkg/stray.py" in err
    assert "run step 2 with --no-graph" in err
    assert "run it on this one" in err
    assert [u["id"] for u in bundles(tmp_path / "out")["extract-pool.json"]["docUnits"]] == ["docunit:a"]


@pytest.mark.parametrize("broken", ["doc-units", "scope-facts"])
@pytest.mark.parametrize("content", [None, "", "{not json", "[]", "{}", '{"docUnits": [1], "subsystems": [1]}'])
def test_main_should_fail_with_one_line_and_write_nothing_on_a_bad_input(
    build_extraction_bundles, monkeypatch, tmp_path, capsys, broken, content
):
    """Test a missing or malformed input file.

    Given:
        A valid pair of inputs with one replaced by a missing, empty,
        malformed, or wrongly shaped file.
    When:
        main() runs.
    Then:
        It should exit 1 with one BAD INPUT line and create no output.
    """
    # Arrange
    doc_units, scope = inputs(tmp_path, [unit("a", "pkg/pool.py", 1)], {"layer:pool": ["pkg/pool.py"]})
    target = doc_units if broken == "doc-units" else scope
    target.unlink()
    if content is not None:
        target.write_text(content)

    # Act
    code = run_main(build_extraction_bundles, monkeypatch, doc_units, scope, tmp_path / "out")
    err = capsys.readouterr().err

    # Assert
    assert code == 1
    assert err.startswith("BAD INPUT")
    assert err.count("\n") == 1
    assert not (tmp_path / "out").exists()


def test_main_should_remove_bundles_an_earlier_run_left(
    build_extraction_bundles, monkeypatch, tmp_path
):
    """Test a re-run into a directory holding bundles of another division.

    Given:
        An output directory holding `extract-pool-2.json` from an earlier run.
    When:
        main() writes one bundle for the subsystem into it.
    Then:
        Only `extract-pool.json` should remain.
    """
    # Arrange
    doc_units, scope = inputs(tmp_path, [unit("a", "pkg/pool.py", 1)], {"layer:pool": ["pkg/pool.py"]})
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "extract-pool-2.json").write_text("{}")

    # Act
    run_main(build_extraction_bundles, monkeypatch, doc_units, scope, tmp_path / "out")

    # Assert
    assert list(bundles(tmp_path / "out")) == ["extract-pool.json"]


@pytest.mark.parametrize("label", ["pool", "pool-2", "wire_grpc"])
def test_label_should_name_pass_files_the_reconciler_labels_alike(
    reconcile_claims, label
):
    """Test that a bundle label survives the pass-file name of step 4.

    Given:
        A bundle label, used as `claims-<label>-pass<N>.json`.
    When:
        reconcile_claims.label_from_names derives the label from five names.
    Then:
        It should return the bundle label, so step 5 writes `claims-<label>.json`.
    """
    # Arrange
    names = [Path(f"claims-{label}-pass{n}.json") for n in range(1, 6)]

    # Act
    derived = reconcile_claims.label_from_names(names)

    # Assert
    assert derived == label
