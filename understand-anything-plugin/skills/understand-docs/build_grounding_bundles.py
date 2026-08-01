#!/usr/bin/env python3
"""build_grounding_bundles.py — package reconciled claims for Phase E grounding.

Deterministic, stdlib only, no LLM.

Groups a subsystem's surviving claims by doc unit, attaches the code location
each claim is *about*, and classifies that location by whether it can be grounded
in place. That last part is the reason this script exists rather than the claims
being handed to an agent directly.

**A claim attached to a `Protocol` cannot be verified where it is written.** The
docstring on `LoadBalancerLike.delegate` describes a contract; the method body is
`...`. Measured on wool's load-balancing subsystem, 76% of surviving claims sit on
Protocol declarations — 46% on class-level contract docstrings, 30% on stub
bodies. An agent told to "check the claim against the code" would find no code,
and the honest verdict for three-quarters of the subsystem would be
`unverifiable` — which is true of the symbol and false of the codebase, since the
behavior does exist, in the implementors.

So each doc unit is tagged with a `groundingTarget`:

    direct       the symbol has an executable body; verify there
    implementor  the symbol is a Protocol/stub; the agent must locate
                 implementors and consumers and verify the contract against them

`implementor` units carry `candidateSites` — a grep-derived, deliberately
over-inclusive list of files that name the symbol. It is a starting point for
navigation, not a resolved answer: Python Protocols are structural, so nothing
guarantees an implementor mentions the Protocol at all. The agent is expected to
search beyond the list, and to say so when it finds nothing.

Usage:
    python3 build_grounding_bundles.py --claims claims-<label>.json \\
        --doc-units doc-units.json --src-root <real source root> \\
        [--max-claims 60] [--out DIR]

Output:
    <out>/ground-<label>-<n>.json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"


def symbol_index(path: Path) -> dict[str, tuple[ast.AST, ast.ClassDef | None]]:
    """qualname -> (node, owning class)."""
    out: dict[str, tuple[ast.AST, ast.ClassDef | None]] = {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"  UNREADABLE {path}: {exc}", file=sys.stderr)
        return out
    except SyntaxError as exc:
        # Loud, because the downstream symptom is indistinguishable from a
        # symbol that genuinely does not exist: every claim on this file
        # silently becomes `unresolved`, and the grounding agent is told there
        # is no code to check. Running this on Python 3.9 against a corpus using
        # 3.10+ syntax turned 27 `direct` claims into `unresolved` with no error
        # anywhere — the interpreter, not the corpus, was the defect.
        print(f"  PARSE FAILED {path}:{exc.lineno} — {exc.msg}. Every claim on "
              f"this file will be reported `unresolved`. Running Python "
              f"{sys.version_info.major}.{sys.version_info.minor}; this pipeline "
              f"needs 3.10+.", file=sys.stderr)
        return out

    def walk(node: ast.AST, prefix: str = "", owner: ast.ClassDef | None = None) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                out[name] = (child, owner)
                walk(child, name + ".",
                     child if isinstance(child, ast.ClassDef) else owner)

    walk(tree)
    return out


def is_protocol(node: ast.ClassDef | None) -> bool:
    if node is None:
        return False
    return any(
        (isinstance(b, ast.Name) and b.id == "Protocol")
        or (isinstance(b, ast.Attribute) and b.attr == "Protocol")
        for b in node.bases
    )


def is_stub(node: ast.AST) -> bool:
    if isinstance(node, ast.ClassDef):
        return False
    body = [
        s for s in getattr(node, "body", [])
        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                and isinstance(s.value.value, str))
    ]
    if not body:
        return True
    return all(
        isinstance(s, ast.Pass)
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
            and s.value.value is Ellipsis)
        or (isinstance(s, ast.Raise) and "NotImplementedError" in ast.dump(s))
        for s in body
    )


def candidate_sites(root: Path, symbol: str, declaring_file: str) -> list[str]:
    """Files naming this symbol, declaring file first.

    The declaring file is **included, not excluded**. An earlier version dropped
    it on the reasoning that it only points back at the declaration — which is
    wrong for exactly the case this function exists to serve. wool declares
    `LoadBalancerContextLike` (a Protocol) and its concrete implementor
    `LoadBalancerContext` in the same `base.py`, so excluding the declaring file
    hid the one implementation an agent needed. Caught by a grounding agent that
    found it anyway by searching method names, as the prompt instructs.

    Method names are searched as `def name` / `.name(` rather than bare, because a
    bare grep for a common method name collides across unrelated concepts — the
    wool run listed six `runtime/context/*` modules for `dispatch`, matching
    "runtime context" against "load-balancer context". A grounding agent read all
    six before discarding them.
    """
    if "." in symbol:                     # a method: match definitions and calls
        pattern = rf"(def[[:space:]]+{re.escape(symbol.split('.')[-1])}\b|\.{re.escape(symbol.split('.')[-1])}\()"
    else:                                 # a class: the bare name is precise enough
        pattern = rf"\b{re.escape(symbol)}\b"
    try:
        raw = subprocess.run(
            ["grep", "-rlE", "--include=*.py", pattern, str(root)],
            capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        return []
    hits = set()
    for line in raw.strip().split("\n"):
        if line:
            hits.add(str(Path(line).resolve().relative_to(root.resolve())))
    # Declaring file first: a same-file implementor is the most common case.
    ordered = ([declaring_file] if declaring_file in hits else [])
    return ordered + sorted(hits - {declaring_file})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--claims", type=Path, required=True)
    ap.add_argument("--doc-units", type=Path, required=True)
    ap.add_argument("--src-root", type=Path, required=True,
                    help="real source root, for AST + grep (never shown to the agent)")
    ap.add_argument("--path-prefix", default="",
                    help="strip this from docUnit sourceFile to get a src-root-relative path")
    ap.add_argument("--max-claims", type=int, default=60)
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--only-claims", type=Path, default=None,
                    help="JSON list of claim IDs (or an object with `claimIds`) "
                         "to restrict the bundles to. Produces ordinary blind "
                         "grounding bundles over a subset — unlike a tie-break "
                         "bundle, no prior verdict is shown. Used to re-ground a "
                         "boundary-sensitive subset after a taxonomy change.")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    payload = json.loads(args.claims.read_text())
    claims = payload["claims"]
    if args.only_claims:
        sel = json.loads(args.only_claims.read_text())
        wanted = set(sel if isinstance(sel, list) else sel.get("claimIds") or [])
        before = len(claims)
        # Reconciled claim sets key on `id`; bundles and verdict files on
        # `claimId`. Same content hash either way.
        cid = lambda c: c.get("id") or c.get("claimId")
        claims = [c for c in claims if cid(c) in wanted]
        found = {cid(c) for c in claims}
        # Silence here would mean re-grounding a subset that quietly lost rows
        # and comparing it to the full prior run as if it were paired.
        print(f"--only-claims: {len(claims)} of {before} claims selected; "
              f"{len(wanted - found)} requested IDs not present in this claim set")
        for cid in sorted(wanted - found):
            print(f"  MISSING {cid}")
    units = {u["id"]: u for u in json.loads(args.doc_units.read_text())["docUnits"]}
    label = args.label or args.claims.stem.replace("claims-", "")

    by_unit: dict[str, list[dict]] = defaultdict(list)
    for claim in claims:
        by_unit[claim["docUnitId"]].append(claim)

    index_cache: dict[str, dict] = {}
    records: list[dict[str, Any]] = []

    for unit_id, unit_claims in sorted(by_unit.items()):
        unit = units.get(unit_id)
        if unit is None:
            continue
        src_file = unit["sourceFile"]
        rel = src_file[len(args.path_prefix):] if src_file.startswith(args.path_prefix) else src_file
        kind, _, qualname = unit["attachedTo"].split(":", 2)

        if rel not in index_cache:
            index_cache[rel] = symbol_index(args.src_root / rel)
        node, owner = index_cache[rel].get(qualname, (None, None))

        if node is None:
            target, reason = "unresolved", "symbol not found in AST"
            sites: list[str] = []
        elif isinstance(node, ast.ClassDef) and is_protocol(node):
            target, reason = "implementor", "class-level docstring on a Protocol"
            sites = candidate_sites(args.src_root, qualname, rel)
        elif is_stub(node):
            target = "implementor"
            reason = ("stub body on a Protocol method" if is_protocol(owner)
                      else "stub body (no executable statements)")
            sites = candidate_sites(args.src_root, qualname, rel)
        else:
            target, reason = "direct", "symbol has an executable body"
            # Callers matter even when the body is present: a grounding agent
            # checking "eviction is the proxy's responsibility" needs the call
            # sites, not just the method. Empty lists here sent one agent
            # searching for context it should have been handed.
            sites = candidate_sites(args.src_root, qualname, rel)

        records.append({
            "docUnitId": unit_id,
            "sourceFile": rel,
            "attachedTo": unit["attachedTo"],
            "attachedKind": unit["attachedKind"],
            "qualname": qualname,
            "declLine": getattr(node, "lineno", unit.get("sourceLine")),
            "docLineRange": unit.get("docLineRange"),
            "groundingTarget": target,
            "groundingReason": reason,
            "candidateSites": sites,
            "claims": [
                {
                    "claimId": c["id"],
                    "claimText": c["claimText"],
                    "claimType": c["claimType"],
                    "quantifier": c["quantifier"],
                    "passCount": c["passCount"],
                    "sourceQuote": c.get("sourceQuote"),
                }
                for c in sorted(unit_claims, key=lambda x: x["id"])
            ],
        })

    # Bundle whole doc units — never split one across bundles, since claims from
    # one docstring share the code the agent has to read.
    bundles: list[list[dict]] = []
    current: list[dict] = []
    count = 0
    for record in records:
        n = len(record["claims"])
        if current and count + n > args.max_claims:
            bundles.append(current)
            current, count = [], 0
        current.append(record)
        count += n
    if current:
        bundles.append(current)

    written = []
    for i, bundle in enumerate(bundles, start=1):
        n_claims = sum(len(r["claims"]) for r in bundle)
        targets: dict[str, int] = defaultdict(int)
        for r in bundle:
            targets[r["groundingTarget"]] += len(r["claims"])
        path = args.out / f"ground-{label}-{i}.json"
        path.write_text(json.dumps({
            "schemaVersion": SCHEMA_VERSION,
            "subsystem": payload.get("stats", {}).get("subsystem"),
            "bundle": i,
            "bundleCount": len(bundles),
            "docUnits": len(bundle),
            "claimCount": n_claims,
            "claimsByTarget": dict(sorted(targets.items())),
            "units": bundle,
        }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        written.append((path, len(bundle), n_claims, dict(targets)))

    total_targets: dict[str, int] = defaultdict(int)
    for r in records:
        total_targets[r["groundingTarget"]] += len(r["claims"])
    total = sum(total_targets.values())
    print(f"{label}: {len(records)} doc units, {total} claims → {len(bundles)} bundles")
    for key in sorted(total_targets):
        n = total_targets[key]
        print(f"  {key:<12} {n:>4} claims ({n / total:.0%})")
    for path, units_n, claims_n, targets in written:
        print(f"  → {path.name}  {units_n} units, {claims_n} claims  {targets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
