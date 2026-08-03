"""Tests for strip_docstrings.py — the anti-circularity mirror.

The mirror is the only source grounding agents read. Its single load-bearing
property is exact line-number preservation: `pool.py:212` in the mirror must be
`pool.py:212` in the real file, or every verified citation is off by the height
of the prose above it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

DOCUMENTED = '''\
"""Module docstring.

Two lines long.
"""
import asyncio


class Pool:
    """Class docstring."""

    def spawn(self, count):
        """Spawn workers.

        Multi-line detail.
        """
        # keep the zero-means-cpu-count behaviour
        if count == 0:
            count = 4
        return count


LEASE = 30
"""PEP 258 attribute docstring — invisible to ast.get_docstring."""
'''


def run_main(module, monkeypatch, src: Path, dest: Path, extra=()):
    argv = ["strip_docstrings.py", str(src), str(dest), *map(str, extra)]
    monkeypatch.setattr(sys, "argv", argv)
    return module.main()


def make_src(tmp_path: Path, text: str = DOCUMENTED) -> Path:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "pool.py").write_text(text)
    return src


def test_main_should_preserve_line_count(strip_docstrings, monkeypatch, tmp_path):
    """Test line-count preservation across stripping.

    Given:
        A module with docstrings at module, class, function, and attribute level.
    When:
        main() mirrors the tree.
    Then:
        It should produce a mirror file with the identical number of lines.
    """
    # Arrange
    src = make_src(tmp_path)

    # Act
    assert run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest") == 0

    # Assert
    mirrored = (tmp_path / "dest" / "pkg" / "pool.py").read_text()
    assert mirrored.count("\n") == DOCUMENTED.count("\n")


def test_main_should_keep_non_docstring_lines_at_their_numbers(
    strip_docstrings, monkeypatch, tmp_path
):
    """Test citation addressability of the mirror.

    Given:
        The same documented module.
    When:
        main() mirrors the tree.
    Then:
        It should keep every non-docstring line byte-identical at its original
        line number.
    """
    # Arrange
    src = make_src(tmp_path)
    original = DOCUMENTED.splitlines()

    # Act
    run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest")

    # Assert
    mirrored = (tmp_path / "dest" / "pkg" / "pool.py").read_text().splitlines()
    for lineno in (5, 8, 11, 16, 17, 18, 19, 22):  # import, class, def, comment, code, LEASE
        assert mirrored[lineno - 1] == original[lineno - 1], f"line {lineno}"


def test_main_should_remove_docstring_text_and_leave_marker(
    strip_docstrings, monkeypatch, tmp_path
):
    """Test the anti-circularity property itself.

    Given:
        The same documented module.
    When:
        main() mirrors the tree.
    Then:
        It should remove every docstring's prose and mark each site with the
        removed-docstring marker.
    """
    # Arrange
    src = make_src(tmp_path)

    # Act
    run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest")

    # Assert
    mirrored = (tmp_path / "dest" / "pkg" / "pool.py").read_text()
    for prose in ("Module docstring", "Class docstring", "Spawn workers",
                  "Multi-line detail", "PEP 258 attribute docstring"):
        assert prose not in mirrored
    assert mirrored.count('"""<docstring removed>"""') == 4


def test_main_should_preserve_comments(strip_docstrings, monkeypatch, tmp_path):
    """Test that comments survive stripping.

    Given:
        A module containing a `#` comment inside a function.
    When:
        main() mirrors the tree.
    Then:
        It should keep the comment — comments are navigation, not extraction
        source.
    """
    # Arrange
    src = make_src(tmp_path)

    # Act
    run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest")

    # Assert
    mirrored = (tmp_path / "dest" / "pkg" / "pool.py").read_text()
    assert "# keep the zero-means-cpu-count behaviour" in mirrored


def test_main_should_pass_verify_on_own_output(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test the built-in self-check.

    Given:
        The same documented module.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 0 and report zero leaked docstrings.
    """
    # Act
    code = run_main(
        strip_docstrings, monkeypatch, make_src(tmp_path), tmp_path / "dest",
        extra=("--verify",),
    )

    # Assert
    assert code == 0
    assert "0 docstrings leaked" in capsys.readouterr().out


@st.composite
def python_modules(draw) -> str:
    """Generate small syntactically valid modules mixing docstrings and code."""
    pieces = []
    if draw(st.booleans()):
        pieces.append('"""module doc\nsecond line\n"""')
    n = draw(st.integers(min_value=1, max_value=4))
    for i in range(n):
        kind = draw(st.sampled_from(["func", "func_doc", "class_doc", "comment"]))
        if kind == "comment":
            pieces.append(f"# comment {i}")
        elif kind == "func":
            pieces.append(f"def f{i}():\n    return {i}")
        elif kind == "func_doc":
            pieces.append(f'def f{i}():\n    """doc {i}\n    detail\n    """\n    return {i}')
        else:
            pieces.append(
                f'class C{i}:\n    """doc {i}"""\n\n    def m(self):\n        return {i}'
            )
    return "\n\n".join(pieces) + "\n"


@settings(max_examples=40, deadline=None)
@given(source=python_modules())
def test_strip_should_preserve_every_non_docstring_line(strip_docstrings, source):
    """Test the preservation property over generated modules.

    Given:
        Any generated module mixing documented and undocumented defs, classes,
        and comments.
    When:
        strip() blanks its docstrings.
    Then:
        It should preserve the line count and leave every line that is not part
        of a docstring byte-identical at its line number.
    """
    import ast

    # Act
    stripped, count = strip_docstrings.strip(source)

    # Assert — line count exactly preserved
    assert stripped.count("\n") == source.count("\n")

    # Assert — lines outside docstring spans are untouched
    spans = []
    for node in ast.walk(ast.parse(source)):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for stmt in body:
                if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    spans.append((stmt.lineno, stmt.end_lineno))
    in_span = {ln for s, e in spans for ln in range(s, e + 1)}
    original_lines = source.splitlines()
    stripped_lines = stripped.splitlines()
    for i, (a, b) in enumerate(zip(original_lines, stripped_lines), start=1):
        if i not in in_span:
            assert a == b, f"line {i} changed"
    assert count == len(spans)
