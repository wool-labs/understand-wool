#!/usr/bin/env python3
"""build_extraction_bundles.py — divide the doc units into step-4 extraction bundles.

Deterministic, stdlib only, no LLM.

Each bundle is the `{INPUT}` of `prompts/extract-claims.md`. The division is
made here rather than by the operator because it fixes which doc units a pass
sees, and therefore which claims can reach quorum: two operators dividing the
same corpus by hand produce claim sets that cannot be compared.

A doc unit belongs to the subsystem of `scope-facts.json` whose `paths` holds
its `sourceFile`. Within a subsystem, doc units are ordered by `sourceFile`,
`sourceLine`, and `id`, and whole files are packed into bundles of at most
`--max-units` doc units. A file is never split, so the parts of one long
docstring, which share a `context` line, stay in one bundle; a file holding
more than `--max-units` doc units is a bundle of its own.

The label of a bundle is the subsystem id with `layer:` removed and each `:`
made `_`, the transform `reconcile_claims.py` applies to a `subsystem` key.
A subsystem that needs more than one bundle labels them `<sub>-1` … `<sub>-k`.
Every later step names its files by this label: the extraction passes are
`claims-<label>-pass<N>.json`, and step 5 writes `claims-<label>.json`.

Usage:
    python3 build_extraction_bundles.py --doc-units doc-units.json \\
        --scope-facts scope-facts.json [--max-units 100] --out DIR

Output:
    <out>/extract-<label>.json    {subsystem, label, chunk, docUnits}

Every `extract-*.json` already in `--out` is deleted first, so a re-run that
writes fewer bundles leaves none from an earlier run.

Exit codes: 0; 1 when an input is missing or malformed (one `BAD INPUT` line,
before anything is written), or when a doc unit's file is in no subsystem (an
`UNASSIGNED` line listing each file, after the bundles are written; those doc
units are in no bundle).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"


def label_of(subsystem_id: str) -> str:
    """The reconciler's transform of a `subsystem` key."""
    return subsystem_id.replace("layer:", "").replace(":", "_")


def pack(units: list[dict[str, Any]], max_units: int) -> list[list[dict[str, Any]]]:
    """Whole files, in order, greedily packed into chunks of at most `max_units`."""
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        by_file[unit["sourceFile"]].append(unit)
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for path in sorted(by_file):
        group = by_file[path]
        if current and len(current) + len(group) > max_units:
            chunks.append(current)
            current = []
        current.extend(group)
    if current:
        chunks.append(current)
    return chunks


def load(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    """The doc units and the subsystems; raises on any malformed input."""
    doc_units = json.loads(args.doc_units.read_text())["docUnits"]
    if not isinstance(doc_units, list) or not all(
            isinstance(u, dict) and isinstance(u.get("id"), str)
            and isinstance(u.get("sourceFile"), str) for u in doc_units):
        raise ValueError("`docUnits` is not a list of doc units with `id` and "
                         "`sourceFile`")
    subsystems = json.loads(args.scope_facts.read_text())["subsystems"]
    if not isinstance(subsystems, list) or not all(
            isinstance(s, dict) and isinstance(s.get("id"), str)
            and isinstance(s.get("paths"), list) for s in subsystems):
        raise ValueError("`subsystems` is not a list of subsystems with `id` "
                         "and `paths`")
    return doc_units, subsystems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--doc-units", type=Path, required=True)
    ap.add_argument("--scope-facts", type=Path, required=True)
    ap.add_argument("--max-units", type=int, default=100)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    try:
        doc_units, subsystems = load(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"BAD INPUT — {type(exc).__name__}: {exc}. Pass the doc-units file "
              f"step 1 wrote and the scope-facts file step 2 wrote, and run "
              f"again.", file=sys.stderr)
        return 1

    owner = {path: s["id"] for s in subsystems for path in s["paths"]}
    by_subsystem: dict[str, list[dict]] = defaultdict(list)
    unassigned: set[str] = set()
    for unit in doc_units:
        sub = owner.get(unit["sourceFile"])
        if sub is None:
            unassigned.add(unit["sourceFile"])
        else:
            by_subsystem[sub].append(unit)

    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.glob("extract-*.json"):
        stale.unlink()

    total = 0
    for sub in sorted(by_subsystem):
        units = sorted(by_subsystem[sub],
                       key=lambda u: (u["sourceFile"], u.get("sourceLine", 0), u["id"]))
        chunks = pack(units, args.max_units)
        for i, chunk in enumerate(chunks, start=1):
            label = label_of(sub) + (f"-{i}" if len(chunks) > 1 else "")
            path = args.out / f"extract-{label}.json"
            path.write_text(json.dumps({
                "schemaVersion": SCHEMA_VERSION,
                "subsystem": sub,
                "label": label,
                "chunk": f"{i}/{len(chunks)}",
                "docUnits": chunk,
            }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
            total += len(chunk)
            files = len({u["sourceFile"] for u in chunk})
            print(f"  → {path.name}  {len(chunk)} doc units, {files} files")
    print(f"{total} of {len(doc_units)} doc units bundled")

    if unassigned:
        print(f"UNASSIGNED — {len(unassigned)} source file(s) hold doc units and "
              f"are in no subsystem of {args.scope_facts}, so their doc units are "
              f"in no bundle: {', '.join(sorted(unassigned))}. If step 2 printed "
              f"an UNASSIGNED line, its graph's layers leave these files out: run "
              f"step 2 with --no-graph. Otherwise step 2 read another doc-units "
              f"file: run it on this one.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
