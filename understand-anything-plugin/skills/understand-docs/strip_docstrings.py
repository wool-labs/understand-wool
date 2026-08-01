#!/usr/bin/env python3
"""strip_docstrings.py — mirror a package with every docstring blanked.

Phase E support for the documentation claim graph (see CLAIM-GRAPH-PLAN.md §2).
Deterministic, stdlib only, no LLM.

**This is the anti-circularity control, and it is the load-bearing part of
grounding.** A grounding agent asked "does the code support this claim?" will,
given the chance, quote the very docstring the claim was extracted from. That is
not evidence — it is the claim restated. On wool's `loadbalancer/base.py`, 71% of
the file is docstring, so an agent reading the real file is mostly reading prose.

The mirror removes the temptation structurally rather than by instruction:

    - every module/class/function docstring is replaced by blank lines
    - **the line count is preserved exactly**, so `base.py:212` in the mirror is
      `base.py:212` in the real file and cited evidence stays checkable
    - comments are kept: unlike docstrings they are rarely the extraction source
      and often carry the reasoning that explains a branch

Byte offsets are NOT preserved — only line numbers, which is what evidence needs.

Usage:
    python3 strip_docstrings.py <src-root> <dest-root> [--verify]

Output:
    a mirror tree of every .py file under src-root, docstrings blanked
"""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path


def strip(source: str) -> tuple[str, int]:
    """Blank every docstring, preserving line numbering. Returns (text, count)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0

    lines = source.splitlines(keepends=True)
    # Collect 1-indexed [start, end] spans of docstring expressions.
    #
    # Every bare string-expression statement counts, not just the canonical
    # docstring slot. PEP 258 attribute docstrings — a string literal following
    # an assignment — are invisible to `ast.get_docstring`, so an earlier version
    # left 10 of them intact in wool (`_STOP_RPC_MARGIN`, the discovery
    # predicates, the proxy defaults). A grounding agent noticed and reported it.
    # No evidence citation ever landed on one, and claims are only ever extracted
    # from module/class/function docstrings, so nothing was contaminated — but a
    # control that is 99% airtight is not a control, it is a habit.
    #
    # A bare string expression is always a no-op at runtime, so blanking any of
    # them is semantically safe.
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for stmt in body:
            if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)):
                spans.append((stmt.lineno, stmt.end_lineno or stmt.lineno))

    for start, end in spans:
        # Keep the indentation of the opening line so the file still reads as
        # structured code, and emit a marker rather than a silent gap — a gap
        # invites the agent to assume the symbol is undocumented, which is a
        # different and equally wrong inference.
        indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
        lines[start - 1] = " " * indent + '"""<docstring removed>"""\n'
        for i in range(start, end):
            lines[i] = "\n"

    return "".join(lines), len(spans)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src", type=Path)
    ap.add_argument("dest", type=Path)
    ap.add_argument("--verify", action="store_true",
                    help="assert line counts match and no triple-quoted prose survives")
    args = ap.parse_args()

    src, dest = args.src.resolve(), args.dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)

    files = sorted(p for p in src.rglob("*.py") if "__pycache__" not in p.parts)
    total_docs = 0
    mismatches: list[str] = []

    for path in files:
        rel = path.relative_to(src)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        original = path.read_text(encoding="utf-8")
        stripped, count = strip(original)
        out.write_text(stripped, encoding="utf-8")
        total_docs += count
        if args.verify and original.count("\n") != stripped.count("\n"):
            mismatches.append(f"{rel}: {original.count(chr(10))} -> {stripped.count(chr(10))}")

    print(f"{len(files)} files mirrored, {total_docs} docstrings blanked")
    print(f"→ {dest}")

    if args.verify:
        if mismatches:
            print("LINE COUNT MISMATCH — evidence line numbers would be wrong:",
                  file=sys.stderr)
            for m in mismatches:
                print("  " + m, file=sys.stderr)
            return 1
        # A surviving docstring means the control leaks.
        leaked = 0
        for path in sorted(dest.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                     ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node, clean=False)
                    if doc and doc != "<docstring removed>":
                        leaked += 1
        if leaked:
            print(f"LEAK: {leaked} docstrings survived stripping", file=sys.stderr)
            return 1
        print("verify: line counts preserved, 0 docstrings leaked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
