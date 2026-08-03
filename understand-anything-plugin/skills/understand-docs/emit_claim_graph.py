#!/usr/bin/env python3
"""emit_claim_graph.py — merge the documentation claim graph into knowledge-graph.json.

Deterministic, stdlib only, no LLM.

Renders claims and design principles as nodes the existing dashboard already
knows how to draw. No new UI: `claim` is an established `NodeType`, `concept`
carries the principles, and the Knowledge edge category (`documents`,
`exemplifies`, `builds_on`, `contradicts`, `related`) carries the relations.

**The join is free.** A doc unit's `attachedTo` is `kind:file:qualname`, which is
byte-identical to the knowledge graph's node id format
(`class:wool/src/.../base.py:LoadBalancerLike`). Measured on wool, 77% of claims
hit an existing node exactly. The remainder are functions the curated graph does
not carry as their own nodes, so linkage falls back:

    exact symbol  ->  containing class  ->  containing file

The fallback is recorded per edge in `linkage`, because "this claim documents
`LoadBalancerLike`" and "this claim documents the file that contains it" are
different strengths of statement and a reader should be able to tell them apart.

**Edge type mapping.** The design taxonomy is finer than `EdgeType`, so the
precise relation is preserved in each edge's `description` and the mapping is
lossy by intent rather than by accident:

    justifies      -> builds_on      refines    -> builds_on
    enables        -> builds_on      realizes   -> exemplifies
    contradicts    -> contradicts    owns       -> related
    delegates-to   -> related        tensions-with -> related

Extending `EdgeType` with first-class `justifies`/`tensions_with` members would
be truer, and is a clean follow-up: it touches `types.ts`, the schema validator,
and the dashboard legend.

Grounding verdicts arrive via `design-claims.json` (produced by `design_lens.py`
with its own `--grounded` flag), not through a flag here.

Usage:
    python3 emit_claim_graph.py --graph knowledge-graph.json \\
        --design-claims design-claims.json --doc-units doc-units.json \\
        [--edges design-edges-*.json] --out merged-knowledge-graph.json
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

EDGE_MAP = {
    "justifies": "builds_on",
    "refines": "builds_on",
    "enables": "builds_on",
    "realizes": "exemplifies",
    "exemplifies": "exemplifies",
    "contradicts": "contradicts",
    "owns": "related",
    "delegates-to": "related",
    "tensions-with": "related",
}

# Uppercase for findings, lowercase for status — a reader scanning tags should be
# able to see at a glance which claims need action. `OVERSTATED` is deliberately
# not prefixed `DRIFT-`: tags are matched exactly everywhere in the dashboard, so
# a shared prefix would imply a substring filter groups them when nothing does.
VERDICT_TAG = {"supported": "verified",
               "overstated": "OVERSTATED",
               "contradicted": "DRIFT",
               "unverifiable": "unverified"}


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-")


def short(text: str, limit: int = 72) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--graph", type=Path, required=True)
    ap.add_argument("--design-claims", type=Path, required=True)
    ap.add_argument("--edges", nargs="*", type=Path, default=[])
    ap.add_argument("--doc-units", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--min-confidence", default="low",
                    choices=["low", "medium", "high"],
                    help="lowest principle confidence to emit")
    args = ap.parse_args()

    graph = json.loads(args.graph.read_text())
    nodes: list[dict[str, Any]] = list(graph["nodes"])
    edges: list[dict[str, Any]] = list(graph["edges"])
    node_ids = {n["id"] for n in nodes}
    by_file: dict[str, str] = {n["filePath"]: n["id"] for n in nodes
                               if n["type"] == "file" and n.get("filePath")}

    units = {u["id"]: u for u in json.loads(args.doc_units.read_text())["docUnits"]}
    claims = {c["claimId"]: c
              for c in json.loads(args.design_claims.read_text())["claims"]}

    def resolve(unit: dict) -> tuple[str | None, str]:
        """Best existing node for a doc unit: exact -> class -> file."""
        attached = unit["attachedTo"]
        if attached in node_ids:
            return attached, "exact"
        kind, path, qual = attached.split(":", 2)
        if "." in qual:                       # method -> its class
            owner = f"class:{path}:{qual.rsplit('.', 1)[0]}"
            if owner in node_ids:
                return owner, "containing-class"
        return by_file.get(path), "containing-file"

    # ---- collapse claims that say the same thing ----
    #
    # A claim id is hash(docUnitId + text), so one sentence appearing in two
    # docstrings is two claims. That is correct for validation — each occurrence
    # is separately true or false of its own symbol — and wrong here, where it
    # renders "Eviction is WorkerProxy's responsibility" three times as three
    # nodes. Occurrences are merged into one node that records every place the
    # sentence appears.
    def canon_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().rstrip(".").casefold()

    text_group: dict[str, list[str]] = collections.defaultdict(list)
    for cid, claim in claims.items():
        if claim["docUnitId"] in units:
            text_group[canon_text(claim["claimText"])].append(cid)
    # Lowest id wins, so the canonical choice is stable across runs.
    canonical: dict[str, str] = {}
    for group in text_group.values():
        keep = min(group)
        for cid in group:
            canonical[cid] = keep
    collapsed = len(canonical) - len(set(canonical.values()))

    # ---- claim nodes ----
    linkage_stats: collections.Counter = collections.Counter()
    # An unmapped verdict silently ships its raw string as a tag. Count them so
    # the next taxonomy change fails loudly rather than leaking.
    unmapped_verdicts: collections.Counter = collections.Counter()
    emitted_claims = 0
    for cid, claim in sorted(claims.items()):
        unit = units.get(claim["docUnitId"])
        if unit is None:
            continue
        if canonical.get(cid) != cid:
            continue
        target, how = resolve(unit)
        grounding = claim.get("grounding") or {}
        verdict = grounding.get("verdict")
        tags = sorted(set(claim["roles"]))
        tags.append(claim["subsystem"])
        if verdict:
            if verdict not in VERDICT_TAG:
                unmapped_verdicts[verdict] += 1
            tags.append(VERDICT_TAG.get(verdict, verdict))

        nodes.append({
            "id": cid,
            "type": "claim",
            "name": short(claim["claimText"]),
            "filePath": unit["sourceFile"],
            "lineRange": unit.get("docLineRange"),
            "summary": claim["claimText"],
            "tags": tags,
            # `complexity` is a CLOSED enum (simple|moderate|complex) in core's
            # schema, so it cannot carry a fourth level. Treat it as the binary
            # "is this a finding" channel it already is in practice: both
            # `contradicted` and `overstated` are findings and should draw the
            # eye equally. Which *kind* of finding is carried in tags.
            "complexity": ("complex" if verdict in ("contradicted", "overstated")
                           else "moderate" if claim["passCount"] >= 4
                           else "simple"),
            # First-class fields rather than more overloading of `complexity`.
            # `GraphNodeSchema` is `.passthrough()`, so these survive validation
            # and save with zero TypeScript changes. (`GraphEdgeSchema` is not
            # passthrough — which is why verdict qualifiers must live on the
            # node, not the edge.)
            **({"groundingVerdict": verdict} if verdict else {}),
            **({"groundingAgreement": grounding.get("agreement")}
               if grounding.get("agreement") else {}),
            "knowledgeMeta": {
                "category": claim["roles"][0],
                "content": claim["claimText"] + (
                    f"\n\nAlso stated in {len(text_group[canon_text(claim['claimText'])]) - 1} "
                    f"other docstring(s)."
                    if len(text_group[canon_text(claim["claimText"])]) > 1 else ""),
            },
        })
        emitted_claims += 1
        linkage_stats[how] += 1

        if target:
            edges.append({
                "source": cid,
                "target": target,
                "type": "documents",
                "direction": "forward",
                "description": (f"documentation claim ({claim['passCount']}/5 "
                                f"extraction agreement"
                                + (f", grounded {verdict}" if verdict else "")
                                + f"; linkage: {how})"),
                "weight": round(claim["passCount"] / 5, 2),
            })

    # ---- principles and design edges ----
    order = {"low": 0, "medium": 1, "high": 2}
    floor = order[args.min_confidence]
    principles: dict[str, dict] = {}
    design_edges = 0
    dropped_edges: collections.Counter = collections.Counter()

    for path in args.edges:
        payload = json.loads(path.read_text())
        for p in payload.get("principles", []):
            if order.get(p.get("confidence", "low"), 0) < floor:
                continue
            realized = sorted({canonical[c] for c in p.get("realizedBy", []) if c in canonical})
            if len(realized) < 2:
                dropped_edges["principle with <2 resolvable claims"] += 1
                continue
            pid = "concept:principle:" + slug(p["statement"])
            # Two agents can independently find the same principle; merge rather
            # than emit duplicates, keeping the union of evidence.
            if pid in principles:
                principles[pid]["realized"].update(realized)
                principles[pid]["justified"].update(
                    canonical[c] for c in p.get("justifiedBy", []) if c in canonical)
                principles[pid]["scope"].update(p.get("scope", []))
                principles[pid]["agents"] += 1
            else:
                principles[pid] = {
                    "statement": p["statement"],
                    "realized": set(realized),
                    "justified": {canonical[c] for c in p.get("justifiedBy", []) if c in canonical},
                    "tension": p.get("tension"),
                    "scope": set(p.get("scope", [])),
                    "confidence": p.get("confidence", "low"),
                    "agents": 1,
                }

        for e in payload.get("edges", []):
            src, dst = canonical.get(e.get("from")), canonical.get(e.get("to"))
            if src is None or dst is None:
                dropped_edges["edge referencing unknown claim"] += 1
                continue
            mapped = EDGE_MAP.get(e.get("type"))
            if not mapped:
                dropped_edges[f"unmapped edge type: {e.get('type')}"] += 1
                continue
            edges.append({
                "source": src,
                "target": dst,
                "type": mapped,
                "direction": "bidirectional" if e["type"] == "tensions-with" else "forward",
                "description": f"{e['type']}: {e.get('rationale', '')}".strip(),
                "weight": 0.8,
            })
            design_edges += 1

    for pid, p in sorted(principles.items()):
        nodes.append({
            "id": pid,
            "type": "concept",
            "name": p["statement"],
            "summary": (p["statement"]
                        + (f"  Trade-off: {p['tension']}" if p.get("tension") else "")),
            "tags": ["design-principle", p["confidence"], *sorted(p["scope"])]
                    + (["has-tension"] if p.get("tension") else []),
            "complexity": "complex" if len(p["realized"]) >= 4 else "moderate",
            "knowledgeMeta": {
                "category": "design-principle",
                "content": p["statement"] + (f"\n\nTrade-off: {p['tension']}"
                                             if p.get("tension") else ""),
            },
        })
        for cid in sorted(p["realized"]):
            edges.append({"source": cid, "target": pid, "type": "exemplifies",
                          "direction": "forward",
                          "description": "realizes this design principle",
                          "weight": 0.9})
        for cid in sorted(p["justified"]):
            edges.append({"source": cid, "target": pid, "type": "builds_on",
                          "direction": "forward",
                          "description": "states the rationale for this principle",
                          "weight": 1.0})

    # ---- layer assignment ----
    #
    # Without this the dashboard renders an empty layer: it draws a layer from
    # its `nodeIds`, and a node absent from every layer is absent from every
    # layer view.
    #
    # Assign each claim to the layer containing the FILE it documents. Matching
    # `layer:{claim.subsystem}` by name looks natural and is brittle — layer ids
    # are regenerated by the architecture pass on every run and are not stable.
    # A re-analysis of wool renamed `layer:load-balancing` to `layer:loadbalancer`
    # and `layer:worker-execution` to `layer:worker`, silently dropping 841 of
    # 1009 claims out of every layer view, with no error: an unmatched name is
    # indistinguishable from a claim that legitimately has no layer.
    #
    # File containment needs no agreement about names, and degrades sensibly —
    # a file the architecture pass never placed simply yields no layer.
    layers = graph.get("layers") or []
    by_layer_id = {L["id"]: L for L in layers}
    node_file = {n["id"]: n.get("filePath") for n in nodes}
    layer_of_file: dict[str, str] = {}
    for layer in layers:
        for nid in layer.get("nodeIds", []):
            path = node_file.get(nid)
            if path and path not in layer_of_file:
                layer_of_file[path] = layer["id"]

    assigned = 0
    unplaced: collections.Counter = collections.Counter()
    emitted_ids = {n["id"] for n in nodes}
    for cid, claim in sorted(claims.items()):
        if cid not in emitted_ids:
            continue
        unit = units.get(claim["docUnitId"])
        layer_id = layer_of_file.get(unit["sourceFile"]) if unit else None
        # Legacy name match as a fallback, so a graph whose ids do line up works.
        layer = (by_layer_id.get(layer_id) if layer_id
                 else by_layer_id.get(f"layer:{claim['subsystem']}"))
        if layer is None:
            unplaced[claim["subsystem"]] += 1
            continue
        layer.setdefault("nodeIds", []).append(cid)
        assigned += 1

    # A principle spans subsystems by construction; it is listed in each layer
    # it is visible in, so it shows up wherever the reader happens to be looking.
    for pid, p in sorted(principles.items()):
        for scope in sorted(p["scope"]) or []:
            layer = by_layer_id.get(f"layer:{scope}")
            if layer is not None:
                layer.setdefault("nodeIds", []).append(pid)

    for L in layers:
        L["nodeIds"] = sorted(set(L.get("nodeIds", [])))

    # Dedup: six agents working on overlapping blocks independently emit the
    # same edge, which the dashboard renders as six identical rows in one
    # sidebar. Agreement is signal, so it is kept as `agreedBy` rather than
    # discarded — but it is one edge.
    merged: dict[tuple[str, str, str], dict] = {}
    for e in edges:
        key = (e["source"], e["target"], e["type"])
        if key in merged:
            prior = merged[key]
            prior["weight"] = max(prior.get("weight", 0), e.get("weight", 0))
            prior["agreedBy"] = prior.get("agreedBy", 1) + 1
            if len(e.get("description") or "") > len(prior.get("description") or ""):
                prior["description"] = e["description"]
        else:
            merged[key] = dict(e)
    duplicate_edges = len(edges) - len(merged)
    edges = list(merged.values())

    # Drop any edge pointing at a node that does not exist, so the graph the
    # dashboard loads is internally consistent.
    all_ids = {n["id"] for n in nodes}
    before = len(edges)
    edges = [e for e in edges if e["source"] in all_ids and e["target"] in all_ids]
    dangling = before - len(edges)

    graph["nodes"], graph["edges"] = nodes, edges
    args.out.write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n")

    print(f"claims emitted:     {emitted_claims} ({assigned} placed in a layer)")
    if unplaced:
        print(f"  WARNING claims with no layer: {dict(unplaced)}")
    print(f"  linkage: {dict(linkage_stats)}")
    print(f"principles emitted: {len(principles)}")
    print(f"design edges:       {design_edges}")
    if unmapped_verdicts:
        print(f"  WARNING unmapped verdicts (raw string used as tag): "
              f"{dict(unmapped_verdicts)}")
    print(f"duplicate claims collapsed: {collapsed}")
    print(f"duplicate edges merged:     {duplicate_edges}")
    if dropped_edges:
        print(f"  dropped: {dict(dropped_edges)}")
    if dangling:
        print(f"  dangling edges removed: {dangling}")
    print(f"graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
