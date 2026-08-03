#!/usr/bin/env python3
"""audit_graph_structure.py — measure how much of the real call graph is present.

Deterministic, stdlib only, no LLM.

A claim graph sits on top of a structural graph, and inherits its blind spots. On
wool this was not hypothetical: an agent asked "how do nested routines work?"
answered from the graph alone and named its single biggest gap as *"which process
enters `routine_scope`?"*. The graph held exactly one `calls` edge into
`routine_scope` — from `wrapper.py:_stream`, the client side. The real caller,
`session.py:437` on the worker, was missing, though `session.py` had 13 nodes.
The graph did not merely omit the answer; it pointed the wrong way.

So this audit compares the graph's `calls` edges against call sites recovered
from the AST, and reports:

    covered     a real call site with a matching graph edge
    MISSING     a real call site with no graph edge — the failure mode above
    unresolved  a bare name that is neither defined locally nor imported, so it
                is not a project call at all. Reported separately: a limit of
                this audit, not a graph defect.

Coverage is deliberately **name-based**: a call site counts as covered when the
graph holds an edge from that caller to *some* symbol of that name. Binding the
callee here and demanding an exact (source, target) match would measure whether
the pipeline agrees with this script's guess, and the two then drift apart
whenever either resolver improves. Where this script *can* bind unambiguously and
disagrees with the graph, that is reported as `divergent` — never folded into
coverage.

The denominator includes call sites whose name is ambiguous across several
project symbols. They are real calls the graph either represents or does not;
excluding them because *this script* cannot bind them would flatter the number.

Resolution is deliberately conservative. A bare `foo()` binds only if `foo` is
imported from a project module or defined in the same file. An attribute call
`x.foo()` binds only when exactly one project symbol is named `foo` — otherwise
it is `unresolved` rather than guessed, since a wrong "missing edge" report is
worse than an admitted unknown.

Coverage here is a floor, not a verdict: the graph is a curated summary and was
never meant to hold every edge. What matters is whether the *load-bearing* ones
survive, which is why `--symbol` reports one symbol's callers exactly.

Usage:
    python3 audit_graph_structure.py --graph knowledge-graph.json \\
        --src-root <source root> [--path-prefix wool/src/] [--symbol NAME]
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
from pathlib import Path
from typing import Any


def qualname_index(tree: ast.AST) -> dict[int, str]:
    """line number of a def -> its dotted qualname."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str = "") -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                out[child.lineno] = name
                walk(child, name + ".")

    walk(tree)
    return out


def enclosing(line: int, defs: list[tuple[int, int, str]]) -> str | None:
    """Innermost def whose body contains this line."""
    best = None
    for start, end, name in defs:
        if start <= line <= end and (best is None or start > best[0]):
            best = (start, name)
    return best[1] if best else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--src-root", type=Path, required=True)
    ap.add_argument("--path-prefix", default="")
    ap.add_argument("--symbol", default=None,
                    help="report every caller of this symbol, and whether the graph has it")
    args = ap.parse_args()

    graph = json.loads(args.graph.read_text())
    node_ids = {n["id"] for n in graph["nodes"]}
    graph_pairs: set[tuple[str, str]] = {
        (e["source"], e["target"]) for e in graph["edges"] if e["type"] == "calls"
    }
    # caller node id -> the simple names it calls. Name-keyed so coverage does
    # not depend on this script resolving to the same target the pipeline chose.
    graph_calls: dict[str, set[str]] = collections.defaultdict(set)
    for source, target in graph_pairs:
        graph_calls[source].add(target.rsplit(":", 1)[-1].split(".")[-1])

    # project symbol name -> node ids that define it (graph side)
    defined: dict[str, list[str]] = collections.defaultdict(list)
    for nid in node_ids:
        if nid.startswith(("function:", "class:")):
            defined[nid.rsplit(":", 1)[-1].split(".")[-1]].append(nid)

    # Every symbol name defined anywhere in the SOURCE.
    #
    # The denominator must not depend on the graph being measured. Deriving it
    # from graph nodes lets a richer graph inflate its own denominator — the
    # before/after comparison then divides by different numbers and the
    # improvement is unquantifiable. This set is a property of the code alone.
    source_symbols: set[str] = set()
    for path in sorted(p for p in args.src_root.rglob("*.py")
                       if "__pycache__" not in p.parts):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                source_symbols.add(node.name)

    files = sorted(p for p in args.src_root.rglob("*.py")
                   if "__pycache__" not in p.parts)

    covered = missing = unresolved = closure_calls = divergent = 0
    no_caller_node = 0
    divergent_rows: list[tuple[str, str, str, int, str]] = []
    missing_rows: list[tuple[str, str, str, int]] = []
    symbol_rows: list[tuple[str, str, int, bool]] = []

    for path in files:
        rel = args.path_prefix + str(path.relative_to(args.src_root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue

        defs: list[tuple[int, int, str]] = []
        qi = qualname_index(tree)

        def collect(node: ast.AST, prefix: str = "") -> None:
            for child in getattr(node, "body", []):
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = f"{prefix}{child.name}"
                    defs.append((child.lineno, child.end_lineno or child.lineno, name))
                    collect(child, name + ".")

        collect(tree)
        local_names = {n.split(".")[-1] for _, _, n in defs}
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                name, bare = func.id, True
            elif isinstance(func, ast.Attribute):
                name, bare = func.attr, False
            else:
                continue

            if name not in source_symbols:
                continue
            caller_qual = enclosing(node.lineno, defs)
            if caller_qual is None:
                continue
            # Attribute the call to the nearest ENCLOSING graph node, walking
            # outward through nested closures.
            #
            # This is not a nicety. wool calls `routine_scope` from
            # `DispatchSession._schedule_worker._start._run` — a closure two
            # levels deep. The graph models methods, not closures, so an exact
            # match finds nothing and the call silently vanishes from the audit,
            # which is precisely how the real caller went unnoticed. Walking
            # outward attributes it to `_schedule_worker`, which does exist.
            src_id = None
            parts = caller_qual.split(".")
            depth_climbed = 0
            while parts and src_id is None:
                cand_qual = ".".join(parts)
                for kind in ("function", "class"):
                    cand = f"{kind}:{rel}:{cand_qual}"
                    if cand in node_ids:
                        src_id = cand
                        break
                if src_id is None:
                    parts.pop()
                    depth_climbed += 1
            if src_id is None:
                # The caller has no node at all, so the graph cannot represent
                # this call. That is a miss, not something to exclude.
                missing += 1
                no_caller_node += 1
                missing_rows.append((caller_qual, name, rel, node.lineno))
                continue
            if depth_climbed:
                closure_calls += 1

            targets = defined[name]
            # A bare name must be locally defined or imported to be a project
            # call at all; this is about the *denominator*, not about binding.
            if bare and not (name in local_names or name in imported):
                unresolved += 1
                continue

            # COVERAGE IS NAME-BASED, NOT IDENTITY-BASED.
            #
            # An earlier version bound the callee with the audit's own rules and
            # then asked whether that exact (source, target) pair existed. That
            # measures "does the pipeline agree with this script's guess?", so
            # the two silently diverge the moment either resolver improves: when
            # the pipeline learned to see past Protocol stubs, those call sites
            # dropped out of the audit entirely and the reported coverage became
            # an undercount of the pipeline's real behaviour.
            #
            # Coverage now asks only: does the graph carry an edge from this
            # caller to *something with this name*? That is rule-independent, so
            # improving either resolver cannot corrupt the measurement.
            edge_targets = graph_calls.get(src_id, set())
            hit = name in edge_targets
            if hit:
                covered += 1
            else:
                missing += 1
                missing_rows.append((caller_qual, name, rel, node.lineno))

            # Independence is preserved separately: where this script *can*
            # bind unambiguously, disagreeing with the graph is worth knowing —
            # but it is reported as divergence, never folded into coverage.
            if len(targets) == 1 and targets[0] != src_id:
                tgt = targets[0]
                if hit and tgt.rsplit(":", 1)[-1].split(".")[-1] == name:
                    if (src_id, tgt) not in graph_pairs:
                        divergent += 1
                        divergent_rows.append((caller_qual, name, rel, node.lineno, tgt))

            if args.symbol and name == args.symbol:
                symbol_rows.append((f"{rel}:{caller_qual}", name, node.lineno, hit))

    total = covered + missing
    print(f"graph `calls` edges: {len(graph_pairs)}")
    print(f"resolvable call sites found in AST: {total}")
    if total:
        print(f"  covered by a graph edge: {covered}  ({covered / total:.0%})")
        print(f"  MISSING from the graph:  {missing}  ({missing / total:.0%})")
    print(f"  unresolved (not counted):  {unresolved}")
    if no_caller_node:
        print(f"  (of the missing, {no_caller_node} have no node for the caller at all)")
    print(f"  (of the resolvable, {closure_calls} originate inside nested closures\n    and were attributed to the enclosing graph node)")
    if divergent:
        print(f"  divergent targets (graph bound a different symbol than this "
              f"script would): {divergent}")

    if missing_rows:
        by_target = collections.Counter(name for _, name, _, _ in missing_rows)
        print("\nmost-frequently-missed call targets:")
        for name, n in by_target.most_common(12):
            print(f"  {n:>3}  -> {name}")

    if args.symbol:
        print(f"\n=== callers of `{args.symbol}` ===")
        if not symbol_rows:
            print("  none found in the AST")
        for caller, _name, line, hit in sorted(symbol_rows):
            mark = "in graph" if hit else "MISSING"
            print(f"  [{mark:<8}] {caller}  (line {line})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
