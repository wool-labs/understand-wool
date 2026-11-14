"""Tests for strip_docstrings.py — the anti-circularity mirror.

The mirror is the only source grounding agents read. Its single load-bearing
property is exact line-number preservation: `pool.py:212` in the mirror must be
`pool.py:212` in the real file, or every verified citation is off by the height
of the prose above it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
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
        kind = draw(st.sampled_from(["func", "func_doc", "class_doc", "comment",
                                     "inline_doc", "attr_doc", "trailing_code"]))
        if kind == "comment":
            pieces.append(f"# comment {i}")
        elif kind == "func":
            pieces.append(f"def f{i}():\n    return {i}")
        elif kind == "inline_doc":
            pieces.append(f'def g{i}(): "doc {i}"')
        elif kind == "attr_doc":
            pieces.append(f'A{i} = {i}; "doc {i}"; B{i} = {i}')
        elif kind == "trailing_code":
            pieces.append(f'class D{i}:\n    """doc {i}\n    detail"""; x = {i}')
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


def skeleton(source: str) -> str:
    """The module's AST with every bare string statement's text erased."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            node.value.value = ""
    return ast.dump(tree)


def statement_lines(source: str) -> list[tuple[str, int]]:
    """(kind, line) of every statement, in source order."""
    return sorted(((type(n).__name__, n.lineno)
                   for n in ast.walk(ast.parse(source)) if isinstance(n, ast.stmt)),
                  key=lambda pair: (pair[1], pair[0]))


@settings(max_examples=60, deadline=None)
@given(source=python_modules())
def test_strip_should_change_nothing_but_docstring_text(strip_docstrings, source):
    """Test that stripping leaves the program itself alone.

    Given:
        Any generated module, including docstrings that share a line with a
        def, with assignments on either side, or with code after the closing
        quotes of a multi-line string.
    When:
        strip() blanks its docstrings.
    Then:
        It should yield a module whose syntax tree equals the original's once
        docstring text is erased from both, with every statement on its
        original line and no docstring text left.
    """
    # Act
    stripped, _ = strip_docstrings.strip(source)

    # Assert
    assert skeleton(stripped) == skeleton(source)
    assert statement_lines(stripped) == statement_lines(source)
    assert "doc" not in stripped.replace("<docstring removed>", "")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('def f(): "doc"\n', 'def f(): """<docstring removed>"""\n'),
        ('x = 1; "doc"\n', 'x = 1; """<docstring removed>"""\n'),
        ('"doc"; y = 2\n', '"""<docstring removed>"""; y = 2\n'),
        ('é = 1; "döc"; w = "é"\n', 'é = 1; """<docstring removed>"""; w = "é"\n'),
        ('class C:\n    """doc\n    more"""; z = 3\n',
         'class C:\n    """<docstring removed>\n"""; z = 3\n'),
    ],
    ids=["after-def", "after-assignment", "before-assignment", "non-ascii",
         "code-after-multiline"],
)
def test_strip_should_keep_code_that_shares_a_line_with_a_docstring(
    strip_docstrings, source, expected
):
    """Test that only the string is replaced, never its whole line.

    Given:
        A docstring on the same physical line as executable code.
    When:
        strip() blanks the docstring.
    Then:
        It should replace the string alone and leave the code on its line.
    """
    # Act
    stripped, count = strip_docstrings.strip(source)

    # Assert
    assert stripped == expected
    assert count == 1


def test_strip_should_blank_the_right_lines_when_the_file_contains_a_form_feed(
    strip_docstrings,
):
    """Test line addressing on a character that is whitespace to Python.

    Given:
        A module with a form feed before a documented function.
    When:
        strip() blanks its docstrings.
    Then:
        It should blank the docstring's own lines and keep the def and the
        return where they were.
    """
    # Arrange
    source = 'A = 1\n\x0c\ndef g():\n    """doc\n    more"""\n    return 1\n'

    # Act
    stripped, _ = strip_docstrings.strip(source)

    # Assert
    assert stripped == (
        'A = 1\n\x0c\ndef g():\n    """<docstring removed>"""\n\n    return 1\n')


def test_strip_should_raise_when_the_source_does_not_parse(strip_docstrings):
    """Test that an unparseable source is never handed back as stripped.

    Given:
        Source text that is not valid Python and contains a docstring.
    When:
        strip() is called on it.
    Then:
        It should raise SyntaxError.
    """
    # Act & assert
    with pytest.raises(SyntaxError):
        strip_docstrings.strip('def broken(:\n    """leak me"""\n')


# ── main() — files that cannot be stripped ────────────────────────────────

BROKEN = 'def broken(:\n    """leak me"""\n'


def add_file(src: Path, rel: str, data: str | bytes) -> None:
    path = src / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


@pytest.mark.parametrize(
    "data",
    [BROKEN, '"""caf\xe9 leak me"""\n'.encode("latin-1"),
     b'"""leak me"""\nx = 1\x00\n'],
    ids=["syntax-error", "not-utf8", "null-byte"],
)
def test_main_should_leave_a_file_out_of_the_mirror_when_it_cannot_be_parsed(
    strip_docstrings, monkeypatch, tmp_path, capsys, data
):
    """Test that an unstrippable file is excluded, not copied through.

    Given:
        A source tree with one parseable file and one that cannot be read as
        UTF-8 Python, each containing a docstring.
    When:
        main() mirrors the tree without --verify.
    Then:
        It should mirror the parseable file, write nothing for the other, name
        that file on stderr, and count only the mirrored file.
    """
    # Arrange
    src = make_src(tmp_path)
    add_file(src, "pkg/bad.py", data)
    dest = tmp_path / "dest"

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, dest)

    # Assert
    captured = capsys.readouterr()
    assert code == 0
    assert (dest / "pkg" / "pool.py").exists()
    assert not (dest / "pkg" / "bad.py").exists()
    assert "UNPARSEABLE" in captured.err
    assert "pkg/bad.py" in captured.err
    assert "1 files mirrored" in captured.out


@pytest.mark.parametrize(
    "data",
    [b'\xef\xbb\xbfdef f():\n    """caf\xc3\xa9 prose"""\n    return 1\n',
     b'# -*- coding: latin-1 -*-\ndef f():\n    """caf\xe9 prose"""\n    return "\xe9"\n'],
    ids=["utf8-bom", "latin1-coding-line"],
)
def test_main_should_mirror_a_file_python_can_decode(
    strip_docstrings, monkeypatch, tmp_path, capsys, data
):
    """Test that valid Python in a declared encoding is not called unparseable.

    Given:
        A source file that begins with a byte-order mark, or declares Latin-1
        with a PEP 263 coding line, and contains a docstring.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 0, mirror the file with its docstring removed, and
        report nothing unparseable.
    """
    # Arrange
    src = tmp_path / "src"
    add_file(src, "pkg/enc.py", data)
    dest = tmp_path / "dest"

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, dest, extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    mirrored = (dest / "pkg" / "enc.py").read_text(encoding="utf-8")
    assert code == 0
    assert "prose" not in mirrored
    assert "def f():" in mirrored
    assert "UNPARSEABLE" not in captured.err


@pytest.mark.parametrize("root", ["missing", "empty"])
def test_main_should_fail_and_keep_the_destination_when_there_is_no_source(
    strip_docstrings, monkeypatch, tmp_path, capsys, root
):
    """Test that a wrong source root cannot produce a passing empty mirror.

    Given:
        A source root that does not exist, or exists and holds no Python file,
        and a destination holding an earlier mirror.
    When:
        main() runs with --verify.
    Then:
        It should exit 1 with a NO SOURCE line, print no success line, and
        leave the earlier mirror in place.
    """
    # Arrange
    src = tmp_path / "nowhere"
    if root == "empty":
        src.mkdir()
    dest = tmp_path / "dest"
    add_file(dest, "pkg/earlier.py", "x = 1\n")

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, dest, extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "NO SOURCE" in captured.err
    assert "verify:" not in captured.out
    assert (dest / "pkg" / "earlier.py").exists()


def test_main_should_rebuild_the_mirror_when_the_destination_is_inside_the_source(
    strip_docstrings, monkeypatch, tmp_path
):
    """Test a second run onto a mirror that lives under the source root.

    Given:
        A source tree already mirrored into a directory inside itself.
    When:
        main() mirrors the tree into that directory again with --verify.
    Then:
        It should exit 0, keep the mirror, and not mirror the earlier mirror.
    """
    # Arrange
    src = make_src(tmp_path)
    dest = src / ".ua" / "src-stripped"
    assert run_main(strip_docstrings, monkeypatch, src, dest) == 0

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, dest, extra=("--verify",))

    # Assert
    assert code == 0
    assert (dest / "pkg" / "pool.py").exists()
    assert not (dest / ".ua").exists()


def test_main_should_skip_a_directory_whose_name_ends_in_py(
    strip_docstrings, monkeypatch, tmp_path
):
    """Test a source tree holding a directory named like a module.

    Given:
        A source tree with a directory `pkg/thing.py/` beside `pkg/pool.py`.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 0 and mirror pool.py without treating the directory as
        a source file.
    """
    # Arrange
    src = make_src(tmp_path)
    (src / "pkg" / "thing.py").mkdir()
    dest = tmp_path / "mirror"

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, dest, extra=("--verify",))

    # Assert
    assert code == 0
    assert (dest / "pkg" / "pool.py").is_file()
    assert not (dest / "pkg" / "thing.py").exists()


def test_main_should_fail_verify_when_a_file_was_left_out(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test that --verify does not pass over an incomplete mirror.

    Given:
        A source tree with one parseable file and one with a syntax error.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 1, report the unparseable file as the failure, still
        check the rest of the mirror, and not print the success line.
    """
    # Arrange
    src = make_src(tmp_path)
    add_file(src, "pkg/bad.py", BROKEN)

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "verify FAILED: UNPARSEABLE" in captured.err
    assert "pkg/bad.py:1" in captured.err
    assert "LEAK" not in captured.err
    assert "verify:" not in captured.out


def test_main_should_remove_a_stale_mirror_file_when_its_source_stops_parsing(
    strip_docstrings, monkeypatch, tmp_path
):
    """Test that an earlier mirror of a now-broken file does not linger.

    Given:
        A mirror built while a file parsed, and that file since broken.
    When:
        main() mirrors the tree again into the same destination.
    Then:
        It should leave no copy of the file in the mirror.
    """
    # Arrange
    src = make_src(tmp_path)
    add_file(src, "pkg/later.py", 'def ok():\n    """doc"""\n')
    dest = tmp_path / "dest"
    run_main(strip_docstrings, monkeypatch, src, dest)
    add_file(src, "pkg/later.py", BROKEN)

    # Act
    run_main(strip_docstrings, monkeypatch, src, dest)

    # Assert
    assert not (dest / "pkg" / "later.py").exists()


# ── main() --verify — failure paths ───────────────────────────────────────
#
# A correct strip() never trips these checks, so each test swaps in a strip()
# with one specific defect and asserts that --verify names it.


def whole_line_strip(source: str) -> tuple[str, int]:
    """The defect --verify's symbol check exists for: overwrite the line."""
    lines = source.split("\n")
    count = 0
    for stmt in ast.walk(ast.parse(source)):
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)):
            lines[stmt.lineno - 1] = '"""<docstring removed>"""'
            count += 1
    return "\n".join(lines), count


def test_main_should_fail_verify_when_a_symbol_is_missing_from_the_mirror(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test the symbol-survival check.

    Given:
        A source file whose function shares a line with its docstring, and a
        strip() that overwrites that whole line.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 1 and name the lost function with its file and line.
    """
    # Arrange
    src = make_src(tmp_path, 'A = 1\ndef f(): "doc"\n')
    monkeypatch.setattr(strip_docstrings, "strip", whole_line_strip)

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "verify FAILED: SYMBOL LOST" in captured.err
    assert "pkg/pool.py:2 f" in captured.err


def test_main_should_fail_verify_when_a_symbol_moves_to_another_line(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test that a symbol counts as kept only on its original line.

    Given:
        A strip() that swaps a function with the blank line above it, so the
        line count holds and the function sits one line early.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 1 and report the function as lost at its source line.
    """
    # Arrange
    src = make_src(tmp_path, 'A = 1\n\ndef f(): return 1\n')
    monkeypatch.setattr(strip_docstrings, "strip",
                        lambda source: ('A = 1\ndef f(): return 1\n\n', 0))

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "pkg/pool.py:3 f" in captured.err
    assert "LINE COUNT MISMATCH" not in captured.err


@pytest.mark.parametrize(
    "source",
    ['def f():\n    """real prose"""\n', 'LEASE = 30\n"""attribute prose"""\n',
     'try:\n    pass\nfinally:\n    "prose in a finally block"\n'],
    ids=["function", "attribute", "finally-block"],
)
def test_main_should_fail_verify_when_a_docstring_survives(
    strip_docstrings, monkeypatch, tmp_path, capsys, source
):
    """Test the leak check on every kind of bare string statement.

    Given:
        A strip() that returns its input unchanged.
    When:
        main() mirrors a documented file with --verify.
    Then:
        It should exit 1 and report the surviving docstring by file and line.
    """
    # Arrange
    src = make_src(tmp_path, source)
    monkeypatch.setattr(strip_docstrings, "strip", lambda text: (text, 0))

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "verify FAILED: LEAK" in captured.err
    assert "pkg/pool.py:" in captured.err


def test_main_should_fail_verify_when_the_line_count_changes(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test the line-count check.

    Given:
        A strip() that drops the docstring's line instead of blanking it.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 1 and report both line counts for the file.
    """
    # Arrange
    src = make_src(tmp_path, 'def f():\n    """doc"""\n    return 1\n')
    monkeypatch.setattr(strip_docstrings, "strip",
                        lambda text: ("def f():\n    return 1\n", 1))

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "verify FAILED: LINE COUNT MISMATCH" in captured.err
    assert "pkg/pool.py: 3 -> 2" in captured.err


def test_main_should_fail_verify_when_the_mirror_does_not_parse(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test that --verify survives a mirror file it cannot parse.

    Given:
        A strip() that turns the first of two valid files into invalid Python
        and leaves a docstring in the second.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 1, report the unparseable mirror file without raising,
        and still report the leak in the file after it.
    """
    # Arrange
    src = make_src(tmp_path, "def f():\n    return 1\n")
    add_file(src, "pkg/zeta.py", 'def z():\n    """real prose"""\n')
    monkeypatch.setattr(
        strip_docstrings, "strip",
        lambda text: (text.replace("def f():", "def f(:"), 0))

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    captured = capsys.readouterr()
    assert code == 1
    assert "verify FAILED: MIRROR DOES NOT PARSE" in captured.err
    assert "pkg/pool.py:1" in captured.err
    assert "verify FAILED: LEAK" in captured.err
    assert "pkg/zeta.py:2" in captured.err


def test_main_should_report_the_symbols_it_kept_when_verify_passes(
    strip_docstrings, monkeypatch, tmp_path, capsys
):
    """Test that a passing --verify states what the symbol check covered.

    Given:
        A file holding a class, a method, a nested function, a function inside
        an `if` block, and a function sharing a line with its docstring.
    When:
        main() mirrors the tree with --verify.
    Then:
        It should exit 0 and report all five symbols kept in place.
    """
    # Arrange
    source = (
        'class C:\n'
        '    def m(self):\n'
        '        def inner(): "doc"\n'
        '        return inner\n'
        'if True:\n'
        '    def guarded():\n'
        '        """doc"""\n'
        'def f(): "doc"\n'
    )
    src = make_src(tmp_path, source)

    # Act
    code = run_main(strip_docstrings, monkeypatch, src, tmp_path / "dest",
                    extra=("--verify",))

    # Assert
    assert code == 0
    assert "5 symbols kept in place" in capsys.readouterr().out
