#!/usr/bin/env python3
"""seed_scope_facts.py — derive subsystems and seed the vocabulary map.

Stage 0b of the documentation claim graph (see CLAIM-GRAPH-PLAN.md). Deterministic,
stdlib only, no LLM.

`scope-facts.json` feeds the Phase D same-referent gate, which is the single
highest-precision lever available: the most common false contradiction is two
claims about *different things* that share vocabulary. "The retry limit is 3" in
`discovery/` and in `worker/` are not in conflict, and the model should be told
the scope rather than made to infer it.

Nothing here is hand-authored. Two derivations:

  SUBSYSTEMS   from `/understand`'s `KnowledgeGraph.layers[]` when a graph exists
               (every file-level node belongs to exactly one layer, which is the
               partition this needs, with a generated responsibility line), else
               from directory structure as a fallback.

  VOCABULARY   from cross-subsystem `:param` collisions in `doc-units.json`. Any
               parameter name documented in two or more subsystems with differing
               bodies is a terminology- or designation-clash candidate. These are
               exhaustive over the corpus rather than limited to what a maintainer
               recalls.

Output is a *seed*: every derived entry carries `needsReview: true`. The manual
step is confirm-or-correct, not compose.

Usage:
    python3 seed_scope_facts.py <repo-root> --doc-units <doc-units.json>
        [--graph <knowledge-graph.json>] [--src-prefix wool/src/wool/] [--out DIR]

Output:
    <out>/scope-facts.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "0.1.0"

# A name must appear in at least this many subsystems to be a clash candidate.
MIN_SUBSYSTEMS = 2
# Jaccard similarity above which two differently-named params look like one concept.
# Jaccard, not overlap-over-min: a two-token body scores 1.0 against anything under
# a min denominator, which produced `namespace ~ s` and `advertise_host ~ loop`.
ALIAS_JACCARD = 0.6
# Both bodies must carry at least this many content tokens to be comparable at all.
ALIAS_MIN_TOKENS = 4

STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "in", "on", "and", "or", "this", "that",
    "is", "are", "be", "with", "from", "by", "as", "it", "its", "if", "when",
}


def resolve_ua_dir(root: Path) -> Path:
    legacy = root / ".understand-anything"
    return legacy if legacy.is_dir() else root / ".ua"


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in STOPWORDS and len(w) > 2}


def collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Subsystems
# ---------------------------------------------------------------------------


def subsystems_from_graph(graph_path: Path, src_prefix: str) -> Optional[dict[str, dict]]:
    """Layers from a `/understand` knowledge graph.

    The architecture-analyzer guarantees every file-level node belongs to exactly
    one layer, so this is a genuine partition rather than a heuristic grouping —
    and it carries a description, which the directory fallback cannot.
    """
    if not graph_path.exists():
        return None
    graph = json.loads(graph_path.read_text())
    layers = graph.get("layers") or []
    if not layers:
        return None

    node_path = {
        n["id"]: n.get("filePath")
        for n in graph.get("nodes", [])
        if n.get("filePath")
    }
    out: dict[str, dict] = {}
    for layer in layers:
        paths = sorted({node_path[nid] for nid in layer.get("nodeIds", []) if nid in node_path})
        if not paths:
            continue
        out[layer["id"]] = {
            "id": layer["id"],
            "name": layer.get("name", layer["id"]),
            "responsibility": layer.get("description", ""),
            "paths": paths,
            "source": "knowledge-graph.layers",
        }
    return out or None


def subsystems_from_directories(files: list[str], src_prefix: str) -> dict[str, dict]:
    """Fallback: group by the two path segments below the source prefix.

    For a codebase whose directory layout already *is* its subsystem layout this
    recovers most of the grouping — but with no responsibility line, and with no
    validation against import topology, so a misfiled module stays misfiled.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for path in files:
        rel = path[len(src_prefix):] if path.startswith(src_prefix) else path
        parts = rel.split("/")
        key = "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else "root")
        groups[key].append(path)
    return {
        key: {
            "id": key.replace("/", "-"),
            "name": key,
            "responsibility": "",
            "paths": sorted(paths),
            "source": "directory-fallback",
        }
        for key, paths in sorted(groups.items())
    }


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def seed_vocabulary(doc_units: list[dict], subsystem_of: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """Cross-subsystem `:param` collisions, plus parallel-name candidates."""
    uses: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for unit in doc_units:
        sub = subsystem_of.get(unit["sourceFile"])
        if not sub:
            continue
        for field in unit["fields"]:
            if field["kind"] == "param" and field["argument"]:
                name = field["argument"].lstrip("*")
                uses[name].append((sub, unit["sourceFile"], collapse(field["body"])))

    clashes: list[dict] = []
    for name in sorted(uses):
        entries = uses[name]
        subs = sorted({s for s, _, _ in entries})
        if len(subs) < MIN_SUBSYSTEMS:
            continue
        bodies = sorted({b.lower() for _, _, b in entries if b})
        if len(bodies) < 2:
            continue

        per_sub: dict[str, list[str]] = defaultdict(list)
        for sub, _, body in entries:
            if body:
                per_sub[sub].append(body)

        clashes.append({
            "term": name,
            "uses": len(entries),
            "subsystems": subs,
            # One concept per subsystem is the thing a reviewer confirms or corrects.
            "meanings": {s: sorted(set(b))[0] for s, b in sorted(per_sub.items())},
            "distinctBodies": len(bodies),
            "classification": None,   # designation-clash | same-concept | unambiguous
            "needsReview": True,
        })

    # Parallel-name candidates: different names with near-identical bodies.
    #
    # This was intended to find aliases (one concept, two names — `fn` vs `func`).
    # It does not. On wool it surfaces structurally *parallel* siblings instead:
    # `exc_tb`/`exc_type`/`exc_val`, `max_receive_message_length`/`max_send_...`,
    # `args`/`kwargs` — related parameters that share phrasing but are distinct
    # concepts. Meanwhile the one known true alias, `fn`/`func`, is missed because
    # both bodies fall under ALIAS_MIN_TOKENS.
    #
    # Kept because parallel names are worth a reviewer's glance, but labelled for
    # what it actually produces. Real alias detection needs the claim text, not
    # the field body, and belongs after Phase B.
    aliases: list[dict] = []
    names = sorted(uses)
    seen: set[tuple[str, str]] = set()
    for i, a in enumerate(names):
        toks_a = [tokens(b) for _, _, b in uses[a] if b]
        for b in names[i + 1:]:
            if (a, b) in seen:
                continue
            toks_b = [tokens(x) for _, _, x in uses[b] if x]
            best = 0.0
            for ta in toks_a:
                for tb in toks_b:
                    if len(ta) < ALIAS_MIN_TOKENS or len(tb) < ALIAS_MIN_TOKENS:
                        continue
                    union = len(ta | tb)
                    if not union:
                        continue
                    best = max(best, len(ta & tb) / union)
            if best >= ALIAS_JACCARD:
                seen.add((a, b))
                aliases.append({
                    "names": [a, b],
                    "jaccard": round(best, 3),
                    "canonical": None,
                    "needsReview": True,
                })
    aliases.sort(key=lambda x: (-x["jaccard"], x["names"]))
    return clashes, aliases


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--doc-units", type=Path, required=True)
    ap.add_argument("--graph", type=Path, default=None)
    ap.add_argument("--src-prefix", default="")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    root = args.root.resolve()
    out_dir = args.out or (resolve_ua_dir(root) / "docs")
    out_dir.mkdir(parents=True, exist_ok=True)

    doc_units = json.loads(args.doc_units.read_text())["docUnits"]
    files = sorted({u["sourceFile"] for u in doc_units})

    graph_path = args.graph or (resolve_ua_dir(root) / "knowledge-graph.json")
    subsystems = subsystems_from_graph(graph_path, args.src_prefix)
    if subsystems:
        source = "knowledge-graph.layers"
    else:
        subsystems = subsystems_from_directories(files, args.src_prefix)
        source = "directory-fallback"
        print(f"no usable layers at {graph_path} — falling back to directory structure",
              file=sys.stderr)

    subsystem_of: dict[str, str] = {}
    for key, sub in subsystems.items():
        for path in sub["paths"]:
            subsystem_of[path] = sub["id"]

    unassigned = [f for f in files if f not in subsystem_of]
    clashes, aliases = seed_vocabulary(doc_units, subsystem_of)

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "root": str(root),
        "subsystemSource": source,
        "stats": {
            "subsystems": len(subsystems),
            "filesAssigned": len(files) - len(unassigned),
            "filesUnassigned": len(unassigned),
            "vocabularyCandidates": len(clashes),
            "parallelNameCandidates": len(aliases),
            "reviewItems": len(clashes) + len(aliases) + sum(
                1 for s in subsystems.values() if not s["responsibility"]),
        },
        "unassignedFiles": unassigned,
        "subsystems": [subsystems[k] for k in sorted(subsystems)],
        "vocabulary": clashes,
        "parallelNames": aliases,
    }
    path = out_dir / "scope-facts.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    print(f"{len(subsystems)} subsystems ({source}), "
          f"{len(files) - len(unassigned)}/{len(files)} files assigned")
    if unassigned:
        print(f"  UNASSIGNED: {len(unassigned)} — the layer partition is incomplete")
    print(f"{len(clashes)} vocabulary candidates, {len(aliases)} parallel-name candidates")
    print(f"→ {path}  ({payload['stats']['reviewItems']} items need review)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
