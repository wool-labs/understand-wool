"""Tests for harvest_docs.py — which files stage 0 treats as Python.

The harvester and the docstring stripper must agree on that. A file harvested
here and left out of the mirror sends claims to grounding with no code to be
checked against; a file skipped here in silence makes a run under the wrong
interpreter look complete.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

DOCUMENTED = b'def f():\n    """Return one."""\n    return 1\n'

UNREADABLE = {
    "syntax-error": b'def broken(:\n    """leak me"""\n',
    "not-utf8": b'def f():\n    """caf\xe9 prose"""\n    return 1\n',
    "null-byte": b'def f():\n    """prose"""\n    return 1\x00\n',
}
READABLE = {
    "utf8-bom": b'\xef\xbb\xbfdef f():\n    """caf\xc3\xa9 prose"""\n    return 1\n',
    "latin1-coding-line": (b'# -*- coding: latin-1 -*-\ndef f():\n'
                           b'    """caf\xe9 prose"""\n    return 1\n'),
}


def make_repo(tmp_path: Path, odd: bytes) -> Path:
    """A git repository holding one plain module and one unusual one."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "ok.py").write_bytes(DOCUMENTED)
    (root / "src" / "pkg" / "odd.py").write_bytes(odd)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def harvest(module, monkeypatch, root: Path, out: Path) -> set[str]:
    """Run harvest_docs.main(); return the source files that yielded units."""
    monkeypatch.setattr(sys, "argv", ["harvest_docs.py", str(root), "--src",
                                      "src", "--out", str(out)])
    assert module.main() in (0, None)
    units = json.loads((out / "doc-units.json").read_text())["docUnits"]
    return {u["sourceFile"] for u in units}


def mirror(module, monkeypatch, root: Path, dest: Path) -> set[str]:
    """Run strip_docstrings.main(); return the files it mirrored."""
    monkeypatch.setattr(sys, "argv", ["strip_docstrings.py", str(root / "src"),
                                      str(dest)])
    assert module.main() == 0
    return {"src/" + p.relative_to(dest).as_posix() for p in dest.rglob("*.py")}


@pytest.mark.parametrize("odd", UNREADABLE.values(), ids=UNREADABLE.keys())
def test_main_should_name_and_skip_a_file_it_cannot_parse(
    harvest_docs, monkeypatch, tmp_path, capsys, odd
):
    """Test that a skipped file is reported without being asked for.

    Given:
        A repository with one plain module and one that cannot be decoded or
        parsed as Python.
    When:
        main() harvests it without --explain.
    Then:
        It should harvest the plain module only and name the other on stderr.
    """
    # Arrange
    root = make_repo(tmp_path, odd)

    # Act
    harvested = harvest(harvest_docs, monkeypatch, root, tmp_path / "out")

    # Assert
    assert harvested == {"src/pkg/ok.py"}
    assert "SKIPPED (unparseable) src/pkg/odd.py" in capsys.readouterr().err


@pytest.mark.parametrize(
    "odd", [*UNREADABLE.values(), *READABLE.values()],
    ids=[*UNREADABLE.keys(), *READABLE.keys()],
)
def test_main_should_harvest_exactly_the_files_the_stripper_mirrors(
    harvest_docs, strip_docstrings, monkeypatch, tmp_path, odd
):
    """Test that stage 0 and the mirror agree on which files are Python.

    Given:
        A repository with one plain module and one that is broken, in an
        undeclared encoding, or in a declared one.
    When:
        harvest_docs.main() and strip_docstrings.main() each run over it.
    Then:
        The files that yield doc units should be the files in the mirror.
    """
    # Arrange
    root = make_repo(tmp_path, odd)

    # Act
    harvested = harvest(harvest_docs, monkeypatch, root, tmp_path / "out")
    mirrored = mirror(strip_docstrings, monkeypatch, root, tmp_path / "mirror")

    # Assert
    assert harvested == mirrored
