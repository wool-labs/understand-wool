#!/usr/bin/env python3
"""strip_docstrings.py — mirror a package with every docstring blanked.

Step 3 of the `understand-docs` pipeline; the agents of steps 7, 10, and 13
read the mirror, and steps 8 and 11 check citations against it. Deterministic,
stdlib only, no LLM.

**This is the anti-circularity control, and it is the load-bearing part of
grounding.** A grounding agent asked "does the code support this claim?" will,
given the chance, quote the very docstring the claim was extracted from. That is
not evidence — it is the claim restated. On wool's `loadbalancer/base.py`,
docstrings are 72% of the file's characters and 60% of its lines, so an agent
reading the real file is mostly reading prose.

The mirror removes the temptation structurally rather than by instruction:

    - every bare string statement (module, class, function, and PEP 258
      attribute docstrings) is replaced by a `<docstring removed>` marker
    - **the line count is preserved exactly**, so `base.py:212` in the mirror is
      `base.py:212` in the real file and cited evidence stays checkable
    - code that shares a line with a docstring stays on that line
    - comments are kept: unlike docstrings they are rarely the extraction source
      and often carry the reasoning that explains a branch

Byte offsets are NOT preserved — only line numbers, which is what evidence needs.

A file is read the way Python reads it: a UTF-8 byte-order mark and a PEP 263
coding line are honoured. A file that still cannot be decoded or parsed cannot
be stripped, so it is **left out of the mirror** and named on stderr. Copying it
through unchanged would put its docstrings in front of the grounding agents.

The destination is deleted and rebuilt on every run, so it must hold nothing
but the mirror. Each mirror carries a `.understand-docs-mirror` marker file at
its root, and the run refuses to delete a destination that is neither empty nor
marked, or is a file: it exits 1 with a `NOT A MIRROR` line and touches nothing.
It refuses the same way when the source root is the destination or lies inside
it, since deleting the destination would delete the source. Each refusal names
its cause and the action that fits it.

A source root that does not exist, or holds no `.py` file, is an error: the run
exits 1 without touching the destination.

`--verify` re-reads the finished mirror and checks five things:

    - no source file was left out as unparseable
    - every mirrored file has its source's line count
    - every mirrored file still parses
    - no bare string statement other than the marker survives
    - every class and function in the source is in the mirror, on the same line

All five are reported before it exits, and it exits 1 if any failed.

Usage:
    python3 strip_docstrings.py <src-root> <dest-root> [--verify]

Output:
    a mirror tree of every parseable .py file under src-root, docstrings blanked
"""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
import tokenize
from pathlib import Path


MARKER = "<docstring removed>"
# Written at the root of every mirror. A destination without it, and with
# anything in it, is not a mirror this script built, and is not deleted.
MIRROR_MARK = ".understand-docs-mirror"


def refusal(src: Path, dest: Path) -> tuple[str, str] | None:
    """Why `dest` must not be deleted and what to do instead, or None."""
    if src.is_relative_to(dest):
        return (f"the source root {src} is {dest} or lies inside it, so deleting "
                f"the destination would delete the source",
                "The arguments are swapped or mistyped: run again with "
                "<src-root> and <dest-root> as SKILL.md step 3 defines them. "
                "Never empty or remove this directory; it holds the source.")
    if dest.exists() and not dest.is_dir():
        return (f"{dest} is a file",
                "Stop and tell the user; do not delete this file. Run again with "
                "a destination the user names.")
    if dest.is_dir() and any(dest.iterdir()) and not (dest / MIRROR_MARK).is_file():
        return (f"{dest} holds files and no {MIRROR_MARK} marker, so this script "
                f"cannot tell it from a directory it did not build",
                "Stop and tell the user; do not delete this directory. A mirror "
                "built before the marker existed is refused the same way, so it "
                "may be an old mirror; whether to remove it is the user's "
                "decision. Run again with a destination the user names, or once "
                "the user has emptied or removed this one.")
    return None


def not_a_mirror(reason: str, action: str) -> int:
    """Print the refusal to delete the destination; the exit code."""
    print(f"NOT A MIRROR — {reason}. The destination is deleted and rebuilt on "
          f"every run, so nothing was mirrored and nothing was deleted. {action}",
          file=sys.stderr)
    return 1


def docstring_statements(tree: ast.AST) -> list[ast.Expr]:
    """Every bare string-expression statement in `tree`.

    Every one counts, not just the canonical docstring slot. PEP 258 attribute
    docstrings — a string literal following an assignment — are invisible to
    `ast.get_docstring`, so an earlier version left 10 of them intact in wool
    (`_STOP_RPC_MARGIN`, the discovery predicates, the proxy defaults). A
    grounding agent noticed and reported it. One evidence citation of the 9,485
    in the archive did land on one: it quoted the attribute docstring under
    `DEFAULT_QUORUM` in `proxy.py`, verified against that mirror, and is
    rejected against the current one. The claim kept its verdict on three other
    citations, so nothing was contaminated — but a control that blanks 429 of
    439 strings is not a control, it is a habit.
    """
    found: list[ast.Expr] = []
    for node in ast.walk(tree):
        # Every statement list a node owns: `body`, and the `orelse`,
        # `finalbody`, and handler bodies a bare string can equally sit in.
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for stmt in block:
                if (isinstance(stmt, ast.Expr)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    found.append(stmt)
    return found


def symbols(tree: ast.AST) -> set[tuple[str, int]]:
    """(qualname, line) of every class and function in `tree`, at any depth."""
    out: set[tuple[str, int]] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef,
                                  ast.AsyncFunctionDef)):
                out.add((prefix + child.name, child.lineno))
                walk(child, prefix + child.name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def read_source(path: Path) -> str:
    """Read a Python file as the interpreter would decode it.

    `tokenize.open` honours a byte-order mark and a PEP 263 coding line. Reading
    as plain UTF-8 called a valid Latin-1 module undecodable and a module with a
    BOM unparseable, and both were then left out of the mirror for no reason.
    """
    with tokenize.open(path) as handle:
        return handle.read()


def strip(source: str) -> tuple[str, int]:
    """Blank every docstring, preserving line numbering. Returns (text, count).

    Raises `SyntaxError` (or `ValueError`, for source `ast` refuses outright)
    when `source` is not parseable. The caller decides what to do with such a
    file; returning it unchanged would hand back its docstrings as if stripped.
    """
    tree = ast.parse(source)

    # Split on "\n" alone, which is how `ast` numbers lines. `str.splitlines`
    # also breaks on form feeds and other separators Python treats as plain
    # whitespace, and one such character shifts every later span by a line.
    lines = source.split("\n")
    statements = docstring_statements(tree)

    # Bottom-up, so an edit never moves the columns of a statement still to be
    # processed on the same line.
    for stmt in sorted(statements, key=lambda s: (s.lineno, s.col_offset),
                       reverse=True):
        start, end = stmt.lineno - 1, (stmt.end_lineno or stmt.lineno) - 1
        # Replace the string token and nothing else. A docstring can share its
        # line with code — `def f(): "doc"`, or `"doc"; x = 1` — and replacing
        # the whole physical line deleted that code: the mirror stayed valid
        # Python with the right line count while `f` no longer existed in it.
        # `ast` columns are UTF-8 byte offsets, hence the encode/decode.
        head = lines[start].encode("utf-8")[:stmt.col_offset].decode("utf-8")
        tail = lines[end].encode("utf-8")[stmt.end_col_offset:].decode("utf-8")
        if start == end:
            lines[start] = f'{head}"""{MARKER}"""{tail}'
            continue
        for i in range(start + 1, end):
            lines[i] = ""
        if tail.strip():
            # Code follows the closing quotes on the last line. Keep it there,
            # behind a string that still spans the original lines, so the code
            # neither moves nor is left as a line that cannot start a statement.
            lines[start] = f'{head}"""{MARKER}'
            lines[end] = f'"""{tail}'
        else:
            # Emit a marker rather than a silent gap — a gap invites the agent
            # to assume the symbol is undocumented, which is a different and
            # equally wrong inference.
            lines[start] = f'{head}"""{MARKER}"""'
            lines[end] = ""

    return "\n".join(lines), len(statements)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dest", type=Path)
    ap.add_argument("--verify", action="store_true",
                    help="check the mirror: nothing left out, line counts kept, "
                         "no docstring surviving, no class or function lost")
    args = ap.parse_args()

    src, dest = args.src.resolve(), args.dest.resolve()
    # Nothing under the destination is source. The project's data directory
    # sits inside the analyzed tree, so a mirror from an earlier run can be
    # under `src`; listing it would mirror the mirror, and the listed files are
    # gone once the destination is cleared below.
    # `is_file()`, because a directory may be named `*.py` and reading it
    # raised an uncaught `IsADirectoryError`.
    found = [p for p in src.rglob("*.py")
             if p.is_file() and "__pycache__" not in p.parts]
    files = sorted(p for p in found if not p.is_relative_to(dest))
    # Before the destination is cleared, so a mistyped source root does not
    # cost an existing mirror. An empty mirror would otherwise pass every check
    # below, and every grader would then find no code at all.
    no_source = (f"NO SOURCE — {'no .py file under' if src.is_dir() else 'no such directory:'} "
                 f"{src}. Nothing was mirrored and {dest} was left as it was.")
    if not found:
        print(no_source, file=sys.stderr)
        return 1
    # Tested before an empty `files` is reported: a destination that holds the
    # source, or an unmarked one inside it that holds every source file, would
    # otherwise be reported as a source root with no source.
    refused = refusal(src, dest)
    if refused:
        return not_a_mirror(*refused)
    if not files:
        print(no_source, file=sys.stderr)
        return 1
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / MIRROR_MARK).write_text(
        "Built by understand-docs strip_docstrings.py, which deletes and "
        "rebuilds this directory on every run.\n", encoding="utf-8")

    total_docs = 0
    mirrored: list[Path] = []
    unparseable: list[str] = []

    for path in files:
        rel = path.relative_to(src)
        try:
            stripped, count = strip(read_source(path))
        except (SyntaxError, ValueError) as exc:
            # `ValueError` covers bytes that do not decode and source `ast`
            # rejects before parsing. Either way there is no tree to strip by,
            # and the only safe mirror of the file is no file: a verbatim copy
            # carries every docstring in it.
            where = f":{exc.lineno}" if getattr(exc, "lineno", None) else ""
            unparseable.append(f"{rel}{where} — {exc.__class__.__name__}: "
                               f"{getattr(exc, 'msg', None) or exc}")
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(stripped, encoding="utf-8")
        mirrored.append(rel)
        total_docs += count

    print(f"{len(mirrored)} files mirrored, {total_docs} docstrings blanked")
    print(f"→ {dest}")
    if unparseable:
        print(f"UNPARSEABLE — {len(unparseable)} file(s) left out of the mirror. "
              f"Running Python {sys.version_info.major}."
              f"{sys.version_info.minor}. If a file below compiles under the "
              f"Python the corpus targets, this interpreter is the defect: "
              f"re-run the pipeline from step 1 under that Python, because "
              f"step 1 skipped the same file. Otherwise the file is broken and "
              f"stays out:", file=sys.stderr)
        for line in unparseable:
            print("  " + line, file=sys.stderr)

    if not args.verify:
        return 0

    # Every check runs and reports before the exit code is decided, so one
    # failure never hides another.
    mismatches: list[str] = []
    broken: list[str] = []
    leaked: list[str] = []
    lost: list[str] = []
    kept = 0
    for rel in mirrored:
        original = read_source(src / rel)
        stripped = (dest / rel).read_text(encoding="utf-8")
        if original.count("\n") != stripped.count("\n"):
            mismatches.append(
                f"{rel}: {original.count(chr(10))} -> {stripped.count(chr(10))}")
        try:
            tree = ast.parse(stripped)
        except (SyntaxError, ValueError) as exc:
            broken.append(f"{rel}:{getattr(exc, 'lineno', '?')} — "
                          f"{getattr(exc, 'msg', None) or exc}")
            continue
        # A surviving docstring means the control leaks.
        for stmt in docstring_statements(tree):
            if stmt.value.value.strip() != MARKER:
                leaked.append(f"{rel}:{stmt.lineno}")
        # A lost symbol means the mirror hides code that exists: an agent
        # looking for it finds nothing and answers `unverifiable`.
        before = symbols(ast.parse(original))
        after = symbols(tree)
        kept += len(before & after)
        lost.extend(f"{rel}:{line} {name}"
                    for name, line in sorted(before - after,
                                             key=lambda s: (s[1], s[0])))

    failures = [
        ("UNPARSEABLE — source files left out of the mirror", unparseable),
        ("LINE COUNT MISMATCH — evidence line numbers would be wrong", mismatches),
        ("MIRROR DOES NOT PARSE — stripping broke these files", broken),
        ("LEAK — docstrings survived stripping", leaked),
        ("SYMBOL LOST — in the source, absent from the mirror at that line", lost),
    ]
    failed = [(title, items) for title, items in failures if items]
    for title, items in failed:
        # The unparseable files were itemised above; say only the count again.
        if items is unparseable:
            print(f"verify FAILED: {title} ({len(items)}, listed above)",
                  file=sys.stderr)
            continue
        print(f"verify FAILED: {title} ({len(items)}):", file=sys.stderr)
        for item in items:
            print("  " + item, file=sys.stderr)
    if failed:
        return 1
    print(f"verify: line counts preserved, 0 docstrings leaked, "
          f"{kept} symbols kept in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
