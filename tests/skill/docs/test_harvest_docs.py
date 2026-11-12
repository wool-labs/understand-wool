"""Tests for harvest_docs.py — stage 0 of the claim pipeline.

Stage 0 mints the `docUnitId` every later stage joins on, so one group pins
what that id depends on and what it does not. Another covers how a docstring
is parsed and split. A third covers the E0 field checks, whose value rests on
their suppression gates: a candidate list that is mostly false positives is
not reviewed. The first group covers which files the stage treats as Python,
where it must agree with the docstring stripper.
"""

from __future__ import annotations

import ast
import collections
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

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


# ── docUnitId — the identity contract ─────────────────────────────────────


def units_of(module, tmp_path: Path, source: str, rel: str = "pkg/mod.py"):
    """Harvest one source file; return its doc units."""
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    units, _, _ = module.harvest_file(path, tmp_path)
    return units


ORIGINAL = '''
    def spawn(count):
        """Spawn the requested number of workers.

        A count of zero means one worker per CPU.
        """
        return count
'''

# The same docstring re-wrapped and re-indented, moved down the file, around a
# body that also changed.
REFORMATTED = '''
    import os


    def spawn(count):
        """
        Spawn the requested
        number of workers.

        A count of zero means   one worker per CPU.
        """
        if count == 0:
            count = os.cpu_count()
        return count
'''


def test_harvest_file_should_keep_the_unit_id_when_the_docstring_is_reformatted(
    harvest_docs, tmp_path
):
    """Test that the id addresses content and not layout.

    Given:
        A documented function, and the same function with its docstring
        re-wrapped and re-indented, moved to another line, over a changed body.
    When:
        harvest_file harvests each version.
    Then:
        It should give the doc unit the same id in both.
    """
    # Act
    (before,) = units_of(harvest_docs, tmp_path / "a", ORIGINAL)
    (after,) = units_of(harvest_docs, tmp_path / "b", REFORMATTED)

    # Assert
    assert before["id"] == after["id"]
    assert before["sourceLine"] != after["sourceLine"]


@pytest.mark.parametrize(
    ("variant", "rel"),
    [
        (ORIGINAL.replace("one worker per CPU", "one worker per core"), "pkg/mod.py"),
        (ORIGINAL.replace("def spawn", "def start"), "pkg/mod.py"),
        (ORIGINAL, "pkg/other.py"),
    ],
    ids=["reworded", "other-symbol", "other-file"],
)
def test_harvest_file_should_change_the_unit_id_when_its_identity_changes(
    harvest_docs, tmp_path, variant, rel
):
    """Test each input the id is derived from.

    Given:
        A documented function, and a variant that rewords the docstring, or
        attaches the same docstring to another symbol or another file.
    When:
        harvest_file harvests each.
    Then:
        It should give the two doc units different ids.
    """
    # Act
    (before,) = units_of(harvest_docs, tmp_path / "a", ORIGINAL)
    (after,) = units_of(harvest_docs, tmp_path / "b", variant, rel)

    # Assert
    assert before["id"] != after["id"]


def test_harvest_file_should_give_distinct_ids_when_two_symbols_share_a_docstring(
    harvest_docs, tmp_path
):
    """Test that text alone does not identify a unit.

    Given:
        Two methods in one file with byte-identical docstrings.
    When:
        harvest_file harvests the file.
    Then:
        It should emit two doc units with different ids, each attached to its
        own method.
    """
    # Arrange
    source = '''
        class WorkerLike:
            def metadata(self):
                """Return the worker metadata."""

        class Worker:
            def metadata(self):
                """Return the worker metadata."""
    '''

    # Act
    units = units_of(harvest_docs, tmp_path, source)

    # Assert
    assert len({u["id"] for u in units}) == 2
    assert {u["attachedTo"] for u in units} == {
        "function:pkg/mod.py:WorkerLike.metadata",
        "function:pkg/mod.py:Worker.metadata",
    }


@settings(max_examples=50, deadline=None)
@given(
    words=st.lists(st.text(alphabet="abcdefghij", min_size=1, max_size=8),
                   min_size=1, max_size=12),
    gaps=st.lists(st.sampled_from([" ", "  ", "\n", "\n    ", " \t "]),
                  min_size=12, max_size=12),
)
def test_normalize_should_ignore_whitespace_and_case_between_words(
    harvest_docs, words, gaps
):
    """Test the canonical form the id hashes.

    Given:
        Any sequence of words, joined once by single spaces and once by
        arbitrary runs of spaces, tabs, and newlines, in upper case.
    When:
        normalize canonicalises each spelling.
    Then:
        It should return the same string for both.
    """
    # Arrange
    plain = " ".join(words)
    spaced = "".join(w.upper() + g for w, g in zip(words, gaps))

    # Act & assert
    assert harvest_docs.normalize(spaced) == harvest_docs.normalize(plain)


# ── parse_fields ──────────────────────────────────────────────────────────


def test_parse_fields_should_join_continuation_lines_into_one_body(harvest_docs):
    """Test a field whose body wraps onto indented lines.

    Given:
        A `:param:` field followed by two more-indented continuation lines.
    When:
        parse_fields parses the docstring.
    Then:
        It should return one field whose body is the three lines joined by
        single spaces.
    """
    # Arrange
    doc = ("Summary.\n\n"
           ":param count: the number of workers\n"
           "    to spawn, where zero\n"
           "    means one per CPU\n")

    # Act
    fields, _ = harvest_docs.parse_fields(doc)

    # Assert
    (field,) = fields
    assert (field.kind, field.argument) == ("param", "count")
    assert field.body == "the number of workers to spawn, where zero means one per CPU"
    assert field.line_offset == 2


def test_parse_fields_should_keep_a_body_going_across_a_blank_line(harvest_docs):
    """Test a field body with a second paragraph.

    Given:
        A `:returns:` field whose indented body continues after a blank line.
    When:
        parse_fields parses the docstring.
    Then:
        It should keep both paragraphs in the body, separated by a blank line.
    """
    # Arrange
    doc = (":returns: the spawned workers\n"
           "\n"
           "    Empty when the pool is closed.\n")

    # Act
    fields, _ = harvest_docs.parse_fields(doc)

    # Assert
    (field,) = fields
    assert field.body == "the spawned workers\n\nEmpty when the pool is closed."


@pytest.mark.parametrize(
    ("following", "expected_fields"),
    [(":raises ValueError: if negative", 2), ("Unindented prose resumes here.", 1)],
    ids=["next-field", "dedented-prose"],
)
def test_parse_fields_should_end_a_body_at_the_next_line_of_equal_indent(
    harvest_docs, following, expected_fields
):
    """Test where a field body stops.

    Given:
        A `:param:` field with one continuation line, followed by another
        field or by prose at the field's own indent.
    When:
        parse_fields parses the docstring.
    Then:
        It should end the first body before that line.
    """
    # Arrange
    doc = f":param count: how many\n    to spawn\n{following}\n"

    # Act
    fields, _ = harvest_docs.parse_fields(doc)

    # Assert
    assert len(fields) == expected_fields
    assert fields[0].body == "how many to spawn"


@pytest.mark.parametrize(
    ("spelling", "kind"),
    [("arg", "param"), ("keyword", "param"), ("return", "returns"),
     ("raise", "raises"), ("except", "raises"), ("yield", "yields"),
     ("rtype", "rtype")],
)
def test_parse_fields_should_canonicalise_the_field_kind(harvest_docs, spelling, kind):
    """Test the alias table.

    Given:
        A field written with one of the spellings Sphinx accepts.
    When:
        parse_fields parses the docstring.
    Then:
        It should report the canonical kind.
    """
    # Act
    fields, _ = harvest_docs.parse_fields(f":{spelling} x: body text\n")

    # Assert
    assert [f.kind for f in fields] == [kind]


def test_parse_fields_should_separate_directives_from_fields(harvest_docs):
    """Test that a directive is not read as a field.

    Given:
        A docstring holding a `.. note::` directive and a `:param:` field.
    When:
        parse_fields parses the docstring.
    Then:
        It should return one directive and one field, each with its line.
    """
    # Arrange
    doc = "Summary.\n\n.. note:: read this\n\n:param x: a value\n"

    # Act
    fields, directives = harvest_docs.parse_fields(doc)

    # Assert
    assert directives == [{"name": "note", "argument": "read this", "lineOffset": 2}]
    assert [(f.kind, f.argument, f.line_offset) for f in fields] == [("param", "x", 4)]


# ── split_docstring ───────────────────────────────────────────────────────


def paragraph(label: str, chars: int) -> str:
    """One prose paragraph of about `chars` characters, wrapped at 70."""
    words = (f"{label} " * (chars // (len(label) + 1))).strip()
    return "\n".join(textwrap.wrap(words, 70))


def test_split_docstring_should_return_one_range_when_at_the_threshold(harvest_docs):
    """Test the boundary of the split threshold.

    Given:
        A two-paragraph docstring of exactly SPLIT_THRESHOLD characters, and
        the same docstring one character longer.
    When:
        split_docstring splits each.
    Then:
        It should leave the first whole and split the second.
    """
    # Arrange
    base = paragraph("alpha", 880) + "\n\n" + paragraph("beta", 880)
    at = base + "x" * (harvest_docs.SPLIT_THRESHOLD - len(base))
    over = at + "x"

    # Act
    whole = harvest_docs.split_docstring(at)
    parts = harvest_docs.split_docstring(over)

    # Assert
    assert len(at) == harvest_docs.SPLIT_THRESHOLD
    assert whole == [(0, len(at.split("\n")))]
    assert len(parts) == 2


def test_split_docstring_should_not_cut_a_single_oversized_block(harvest_docs):
    """Test a docstring that has nowhere to be split.

    Given:
        One paragraph, with no blank line, longer than the threshold.
    When:
        split_docstring splits it.
    Then:
        It should return the whole docstring as one range.
    """
    # Arrange
    text = paragraph("gamma", 2 * harvest_docs.SPLIT_THRESHOLD)

    # Act
    parts = harvest_docs.split_docstring(text)

    # Assert
    assert parts == [(0, len(text.split("\n")))]


def test_split_docstring_should_fold_a_trailing_sliver_into_the_previous_part(
    harvest_docs,
):
    """Test the minimum part size.

    Given:
        Two paragraphs that fill one part each, followed by a paragraph
        shorter than MIN_PART_CHARS that would start a third.
    When:
        split_docstring splits the docstring.
    Then:
        It should return two parts, the second ending at the last line.
    """
    # Arrange
    text = "\n\n".join([paragraph("alpha", 1700), paragraph("beta", 1700),
                        paragraph("tail", 120)])

    # Act
    parts = harvest_docs.split_docstring(text)

    # Assert
    assert len(parts) == 2
    assert parts[-1][1] == len(text.split("\n"))


def test_split_docstring_should_keep_a_field_with_its_continuation_lines(
    harvest_docs,
):
    """Test that a field is an atomic block.

    Given:
        A long docstring whose part boundary would fall inside a field's
        continuation lines if fields could be cut.
    When:
        split_docstring splits the docstring.
    Then:
        It should start no part on a continuation line.
    """
    # Arrange
    field = ":param count: " + paragraph("delta", 900).replace("\n", "\n    ")
    text = "\n\n".join([paragraph("alpha", 1500), field, paragraph("beta", 1500)])
    lines = text.split("\n")

    # Act
    parts = harvest_docs.split_docstring(text)

    # Assert
    assert len(parts) > 1
    for start, _ in parts:
        assert not lines[start].startswith("    "), lines[start]


@settings(max_examples=40, deadline=None)
@given(sizes=st.lists(st.integers(min_value=40, max_value=1500),
                      min_size=1, max_size=9))
@example(sizes=[40, 40, 339, 1383, 300])
def test_split_docstring_should_partition_the_blocks_in_order(harvest_docs, sizes):
    """Test the packing invariants over generated docstrings.

    Given:
        Any docstring made of paragraphs of arbitrary sizes.
    When:
        split_docstring splits it.
    Then:
        The parts should be in order without overlap, every non-blank line
        should fall in exactly one part, and a part before the last whose
        non-blank lines exceed the threshold should hold a single paragraph.
    """
    # Arrange
    text = "\n\n".join(paragraph(f"w{i}", size) for i, size in enumerate(sizes))
    lines = text.split("\n")

    # Act
    parts = harvest_docs.split_docstring(text)

    # Assert
    covered = [i for start, end in parts for i in range(start, end)]
    assert covered == sorted(set(covered))
    assert {i for i, line in enumerate(lines) if line.strip()} <= set(covered)
    # The last part is exempt: it may carry a folded sliver. A part is sized
    # as the packer sizes it, by its non-blank lines and their newlines; blank
    # separator lines are not counted toward the threshold.
    for start, end in parts[:-1]:
        part = lines[start:end]
        packed = sum(len(line) + 1 for line in part if line.strip())
        if packed > harvest_docs.SPLIT_THRESHOLD:
            assert "" not in "\n".join(part).strip("\n").split("\n")


def test_harvest_file_should_link_the_parts_of_a_split_docstring(
    harvest_docs, tmp_path
):
    """Test what a split docstring's units carry.

    Given:
        A function whose docstring exceeds the threshold, with a field in its
        last paragraph.
    When:
        harvest_file harvests the file.
    Then:
        It should emit one unit per part, each with its own id, a shared
        parent id, its index and the part count, the summary line as context
        on later parts only, and the field numbered against the whole
        docstring.
    """
    # Arrange
    body = "\n\n".join([
        "Spawn the workers.", paragraph("alpha", 1700), paragraph("beta", 1700),
        ":param count: how many to spawn"])
    source = 'def spawn(count):\n    """' + body.replace("\n", "\n    ") + '\n    """\n'

    # Act
    units = units_of(harvest_docs, tmp_path, source)

    # Assert
    assert len(units) > 1
    assert len({u["id"] for u in units}) == len(units)
    assert len({u["parentDocUnitId"] for u in units}) == 1
    assert units[0]["parentDocUnitId"] not in {u["id"] for u in units}
    assert [u["partIndex"] for u in units] == list(range(len(units)))
    assert {u["partCount"] for u in units} == {len(units)}
    assert units[0]["context"] is None
    assert {u["context"] for u in units[1:]} == {"Spawn the workers."}
    assert [f["fieldIndex"] for u in units for f in u["fields"]] == [0]
    assert units[-1]["fields"][0]["argument"] == "count"


# ── returns_value / is_generator / direct_raises ──────────────────────────


def function(source: str) -> ast.AST:
    """The first function defined in `source`."""
    return ast.parse(textwrap.dedent(source)).body[0]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def outer():\n    def helper():\n        return 1\n    helper()\n", False),
        ("def outer():\n    key = lambda x: x + 1\n", False),
        ("def outer():\n    def helper():\n        return 1\n    return helper\n", True),
        ("def outer():\n    if True:\n        return 1\n", True),
        ("def outer():\n    return None\n", False),
        ("def outer():\n    return\n", False),
    ],
    ids=["nested-def-only", "lambda-only", "own-and-nested", "own-in-branch",
         "returns-none", "bare-return"],
)
def test_returns_value_should_judge_the_function_by_its_own_returns(
    harvest_docs, source, expected
):
    """Test that a nested function's return is not the outer function's.

    Given:
        A function whose only value-returning `return` sits in a nested def or
        lambda, or which returns a value itself, or returns nothing.
    When:
        returns_value inspects the function.
    Then:
        It should report whether the function's own body returns a value.
    """
    # Act & assert
    assert harvest_docs.returns_value(function(source)) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def outer():\n    def gen():\n        yield 1\n    return list(gen())\n", False),
        ("def outer():\n    async def gen():\n        yield 1\n    return gen\n", False),
        ("def outer():\n    return (lambda: (yield))\n", False),
        ("def outer():\n    for x in ():\n        yield x\n", True),
        ("def outer():\n    yield from ()\n", True),
        ("def outer():\n    return [x for x in ()]\n", False),
    ],
    ids=["nested-def-only", "nested-async-only", "lambda-only", "own-yield",
         "own-yield-from", "comprehension"],
)
def test_is_generator_should_judge_the_function_by_its_own_yields(
    harvest_docs, source, expected
):
    """Test that a nested generator does not make its enclosing function one.

    Given:
        A function whose only `yield` sits in a nested def or lambda, or which
        yields itself, or does not yield.
    When:
        is_generator inspects the function.
    Then:
        It should report whether the function's own body yields.
    """
    # Act & assert
    assert harvest_docs.is_generator(function(source)) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def outer():\n    def check():\n        raise ValueError('x')\n    check()\n",
         set()),
        ("def outer():\n    def check():\n        raise ValueError\n    raise KeyError\n",
         {"KeyError"}),
        ("def outer():\n    try:\n        pass\n    except OSError as exc:\n"
         "        raise errors.PoolError('x') from exc\n", {"PoolError"}),
        ("def outer(exc):\n    raise exc\n", set()),
        ("def outer():\n    try:\n        pass\n    except OSError:\n        raise\n", set()),
    ],
    ids=["nested-def-only", "own-and-nested", "own-in-handler", "re-raise-variable",
         "bare-raise"],
)
def test_direct_raises_should_collect_only_the_functions_own_raises(
    harvest_docs, source, expected
):
    """Test that a nested function's raise is not the outer function's.

    Given:
        A function whose only `raise` sits in a nested def, or which raises
        exception classes itself, or re-raises a variable.
    When:
        direct_raises inspects the function.
    Then:
        It should return the exception class names the function's own body
        raises.
    """
    # Act & assert
    assert harvest_docs.direct_raises(function(source)) == expected


# ── check_file — E0 candidates and their suppression gates ────────────────


def e0(module, tmp_path: Path, source: str):
    """Harvest and check one file; return (["check:direction", ...], stats)."""
    path = tmp_path / "pkg" / "mod.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    units, scopes, tree = module.harvest_file(path, tmp_path)
    stats: dict[str, int] = collections.defaultdict(int)
    found = module.check_file(path, tmp_path, units, scopes, tree, stats)
    return sorted(f"{c.check}:{c.direction}" for c in found), dict(stats)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('''
         def f(count):
             """Do it.

             :param total: not a parameter
             """
             count += 1
         ''', ["param:incorrect", "param:omitted"]),
        ('''
         def f(count, limit):
             """Do it.

             :param count: how many
             """
             print(count, limit)
         ''', ["param:omitted"]),
        ('''
         def f():
             """Do it.

             :returns: a value
             """
             print()
         ''', ["returns:incorrect"]),
        ('''
         def f():
             """Do it."""
             return 1
         ''', ["returns:omitted"]),
        ('''
         def f():
             """Do it.

             :yields: items
             """
             return 1
         ''', ["returns:omitted", "yields:incorrect"]),
        ('''
         def f():
             """Do it."""
             yield 1
         ''', ["yields:omitted"]),
        ('''
         def f():
             """Do it.

             :raises KeyError: when missing
             """
             done = True
         ''', ["raises:incorrect"]),
        ('''
         def f():
             """Do it."""
             raise KeyError("x")
         ''', ["raises:omitted"]),
        ('''
         def f(count):
             """Do it.

             :param count: how many
             :returns: the count
             :raises ValueError: if negative
             """
             if count < 0:
                 raise ValueError(count)
             return count
         ''', []),
    ],
    ids=["param-incorrect", "param-omitted", "returns-incorrect", "returns-omitted",
         "yields-incorrect", "yields-omitted", "raises-incorrect", "raises-omitted",
         "accurate"],
)
def test_check_file_should_flag_a_field_the_code_disagrees_with(
    harvest_docs, tmp_path, source, expected
):
    """Test each E0 check in both directions.

    Given:
        A public function whose docstring names something the code lacks, or
        omits something the code does, or matches the code exactly.
    When:
        check_file checks the file.
    Then:
        It should emit exactly the expected candidates.
    """
    # Act
    found, _ = e0(harvest_docs, tmp_path, source)

    # Assert
    assert found == expected


@pytest.mark.parametrize(
    ("source", "gate"),
    [
        ('''
         from typing import Protocol

         class Loader(Protocol):
             def load(self):
                 """Load it."""
                 return 1
         ''', "suppressed_protocol"),
        ('''
         from typing import Protocol

         class Loader(Protocol):
             pass

         class Cached(Loader):
             def load(self):
                 """Load it."""
                 return 1
         ''', "suppressed_protocol"),
        ('''
         def load():
             """Load it.

             :returns: the thing
             """
             ...
         ''', "suppressed_stub"),
        ('''
         def load():
             """Load it.

             :returns: the thing
             """
             raise NotImplementedError
         ''', "suppressed_stub"),
        ('''
         def helper():
             raise KeyError("x")

         def load():
             """Load it.

             :raises KeyError: when missing
             """
             helper()
         ''', "suppressed_transitive"),
        ('''
         def load():
             """Load it.

             :raises KeyError: when missing
             """
             elsewhere()
         ''', "suppressed_unresolved_callee"),
        ('''
         def _load():
             """Load it."""
             return 1
         ''', "suppressed_visibility_private"),
        ('''
         class Box:
             def __len__(self):
                 """Count it."""
                 return 1
         ''', "suppressed_visibility_dunder"),
    ],
    ids=["protocol", "protocol-subclass", "stub-ellipsis", "stub-not-implemented",
         "transitive-raise", "unresolved-callee", "private", "dunder"],
)
def test_check_file_should_suppress_a_candidate_at_each_gate(
    harvest_docs, tmp_path, source, gate
):
    """Test every suppression gate.

    Given:
        A documented function that a naive check would flag, in a position one
        suppression rule covers.
    When:
        check_file checks the file.
    Then:
        It should emit no candidate and count the suppression under that gate.
    """
    # Act
    found, stats = e0(harvest_docs, tmp_path, source)

    # Assert
    assert found == []
    assert stats == {gate: 1}


def test_check_file_should_skip_parameter_checks_when_the_function_is_overloaded(
    harvest_docs, tmp_path
):
    """Test the overload gate, which suppresses one family of checks.

    Given:
        An implementation preceded by `@overload` declarations of the same
        name, documenting a parameter only the overloads have, and returning a
        value without a `:returns:` field.
    When:
        check_file checks the file.
    Then:
        It should emit no parameter candidate, count the overload suppression,
        and still flag the undocumented return.
    """
    # Arrange
    source = '''
        from typing import overload

        @overload
        def load(path): ...
        @overload
        def load(path, mode): ...
        def load(*args):
            """Load it.

            :param path: where from
            """
            return args
    '''

    # Act
    found, stats = e0(harvest_docs, tmp_path, source)

    # Assert
    assert found == ["returns:omitted"]
    assert stats == {"suppressed_overload": 1}


def test_check_file_should_not_charge_a_nested_helpers_behaviour_to_its_owner(
    harvest_docs, tmp_path
):
    """Test the omission checks against a nested helper.

    Given:
        A public function that returns nothing, yields nothing, and raises
        nothing itself, containing a nested helper that returns, a nested
        generator, and a nested function that raises.
    When:
        check_file checks the file.
    Then:
        It should emit no candidate.
    """
    # Arrange
    source = '''
        def run(items):
            """Run the items."""
            def key(item):
                return item.rank
            def each():
                yield from items
            def fail():
                raise RuntimeError("x")
            items.sort(key=key)
    '''

    # Act
    found, _ = e0(harvest_docs, tmp_path, source)

    # Assert
    assert found == []


def test_check_file_should_flag_a_false_returns_field_beside_a_nested_generator(
    harvest_docs, tmp_path
):
    """Test the incorrectness check a nested generator used to switch off.

    Given:
        A function documenting `:returns:` that returns no value and contains
        a nested generator.
    When:
        check_file checks the file.
    Then:
        It should flag the `:returns:` field as incorrect.
    """
    # Arrange
    source = '''
        def run(items):
            """Run the items.

            :returns: the results
            """
            def each():
                yield from items
            list(each())
    '''

    # Act
    found, _ = e0(harvest_docs, tmp_path, source)

    # Assert
    assert found == ["returns:incorrect"]


def test_check_file_should_check_a_split_docstring_once_with_all_its_fields(
    harvest_docs, tmp_path
):
    """Test E0 on a function whose docstring was split into parts.

    Given:
        A function with a docstring over the split threshold whose only
        `:param:` field lands in the last part.
    When:
        check_file checks the file.
    Then:
        It should emit no candidate, and in particular not report the
        parameter as undocumented from a part that lacks the field.
    """
    # Arrange
    body = "\n\n".join([
        "Spawn the workers.", paragraph("alpha", 1700), paragraph("beta", 1700),
        ":param count: how many to spawn"])
    source = ('def spawn(count):\n    """' + body.replace("\n", "\n    ")
              + '\n    """\n    print(count)\n')

    # Act
    found, _ = e0(harvest_docs, tmp_path, source)

    # Assert
    assert found == []


# ── main() — output ───────────────────────────────────────────────────────


def test_main_should_write_identical_output_when_run_twice(
    harvest_docs, monkeypatch, tmp_path
):
    """Test the determinism the module promises.

    Given:
        A repository with two documented modules.
    When:
        main() harvests it into two output directories.
    Then:
        It should write byte-identical doc-units.json and e0-candidates.json.
    """
    # Arrange
    root = make_repo(tmp_path, b'def g():\n    """Return two."""\n    return 2\n')

    # Act
    harvest(harvest_docs, monkeypatch, root, tmp_path / "one")
    harvest(harvest_docs, monkeypatch, root, tmp_path / "two")

    # Assert
    for name in ("doc-units.json", "e0-candidates.json"):
        assert (tmp_path / "one" / name).read_bytes() == \
            (tmp_path / "two" / name).read_bytes()
