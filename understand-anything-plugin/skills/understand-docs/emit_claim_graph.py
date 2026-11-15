#!/usr/bin/env python3
"""emit_claim_graph.py — merge the documentation claim graph into knowledge-graph.json.

Step 14 of the `understand-docs` pipeline. Deterministic, stdlib only, no LLM.

Renders claims and design principles as nodes the existing dashboard already
knows how to draw. No new UI: `claim` is an established `NodeType`, `concept`
carries the principles, and the Knowledge edge category (`documents`,
`exemplifies`, `builds_on`, `contradicts`, `related`) carries the relations.

**The join is free.** A doc unit's `attachedTo` is `kind:file:qualname`, which is
byte-identical to the knowledge graph's node id format
(`class:wool/src/.../base.py:LoadBalancerLike`). How many claims hit an existing
node exactly depends on how much of the code the graph models: on wool, 61% on
the first load-balancing graph and 99% (1,770 of 1,784) on the graph of the
whole package. The remainder are functions the graph does not carry as their own
nodes, so linkage falls back:

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

**The trust surface.** Two properties hold for every claim node, and a consumer
may rely on both without reading anything else:

- `groundingVerdict` is always present. A claim no grader examined reads
  `ungrounded`, which is a different statement from `unverifiable` ("examined,
  and no executable code bears on it"). A node that merges occurrences whose
  verdicts differ reads `mixed` and lists each one in `groundingOccurrences`.
- `name` is the full claim text, never an abbreviation, so two distinct claim
  nodes never share a name. Shortening for display is the renderer's job.

Both hold on a re-run as well. Feeding a previous output back in as `--graph`
replaces the claim layer that run wrote rather than appending a second copy.

Usage:
    python3 emit_claim_graph.py --graph knowledge-graph.json \\
        --design-claims design-claims.json --doc-units doc-units.json \\
        [--edges design-edges-*.json] --out merged-knowledge-graph.json

Each item an edges file holds that cannot be drawn is counted on a `dropped:`
line and named on a line below it with its file and the claim ids at fault.
An edges file that is not one the step-13 prompt describes is skipped whole,
with a `BAD EDGES <file>: <reason>` line on stderr.

Exit codes: 0; 1 when any edges file was skipped. The graph is written either
way, without the skipped files' edges and principles.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
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
# able to see at a glance which claims need action.
#
# There is no tag for a scope note. A scoped claim is `verified` like any other
# supported claim, because it is true; the boundary is a qualification on it, not
# a defect to triage. Tagging it would put it in the same visual bucket as
# `DRIFT`, which is the mistake the withdrawn `overstated` verdict made.
VERDICT_TAG = {"supported": "verified",
               "contradicted": "DRIFT",
               "unverifiable": "unverified"}

# Emitter-level statuses. Neither is a grader verdict and neither belongs in the
# grading taxonomy: a grader never casts them, the emitter derives them.
#
# `UNGROUNDED` marks a claim no grader examined. Leaving the field off instead
# made "never checked" render identically to nothing at all, and a consumer
# could not tell it from `unverifiable` — opposite statements about how much
# work has been done.
#
# `MIXED` marks a node that merges occurrences of one sentence whose verdicts
# differ. Each occurrence is graded against its own symbol, so the sentence can
# hold of one and not of another; reporting any single member's verdict for the
# node would attribute it to the rest.
UNGROUNDED = "ungrounded"
MIXED = "mixed"
# Kept apart from `VERDICT_TAG`, whose keys are exactly the grader vocabulary
# and are asserted equal to `reconcile_verdicts.VERDICTS`.
STATUS_TAG = {UNGROUNDED: "ungrounded", MIXED: "mixed"}

# Stamped on every node this script writes, so a later run can find and replace
# them. `claim` is a shared NodeType — other producers emit claim nodes too —
# so the type alone does not say whose a node is.
EMITTER = "understand-docs"
PRINCIPLE_PREFIX = "concept:principle:"
# The shape of an id `reconcile_claims.claim_id` mints: `claim:` plus a SHA-256
# hex digest. Other producers name claims by slug (`claim:<article>:<slug>`),
# so the shape identifies this pipeline's claim nodes even on a graph written
# before the stamp existed.
PIPELINE_CLAIM_ID = re.compile(r"claim:[0-9a-f]{64}")


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-")


CONFIDENCE = {"low": 0, "medium": 1, "high": 2}


def design_payload_fault(payload: Any) -> str | None:
    """Why `payload` is not a design-edges file the step-13 prompt describes, or None."""
    if not isinstance(payload, dict):
        return "the file is not a JSON object with `edges` and `principles`"
    for key in ("edges", "principles"):
        if not isinstance(payload.get(key, []), list):
            return f"`{key}` is not a list"
    for p in payload.get("principles", []):
        if not isinstance(p, dict) or not isinstance(p.get("statement"), str):
            return "a principle has no `statement`"
        if not isinstance(p.get("realizedBy", []), list):
            return "a principle's `realizedBy` is not a list"
        if p.get("confidence", "low") not in CONFIDENCE:
            return (f"a principle's `confidence` is {p.get('confidence')!r}, "
                    f"not low, medium, or high")
    if not all(isinstance(e, dict) for e in payload.get("edges", [])):
        return "an edge is not a JSON object"
    return None


def read_design_edges(name: str, payload: dict, canonical: dict[str, str], floor: int,
                      principles: dict[str, dict], dropped: collections.Counter,
                      items: list[str]) -> list[dict[str, Any]]:
    """The graph edges one design-edges file yields; principles merge into `principles`.

    Everything the emitter reads from an agent's design-edges file is read
    here, so `check_prompts.py` can run this on the step-13 prompt's own
    example and require that every key it reads is one the prompt describes.
    A dropped item is counted in `dropped` and described in `items`.
    """
    out: list[dict[str, Any]] = []
    for p in payload.get("principles", []):
        if CONFIDENCE[p.get("confidence", "low")] < floor:
            continue
        named = p.get("realizedBy", [])
        if len(set(named)) < 2:
            # The prompt requires two or more; this is the agent's error, not
            # a consequence of merging.
            dropped["principle naming <2 claims"] += 1
            items.append(f"{name}: principle {p['statement'][:60]!r} names "
                         f"{len(set(named))} distinct claim(s)")
            continue
        realized = sorted({canonical[c] for c in named if c in canonical})
        if len(realized) < 2:
            dropped["principle with <2 resolvable claims"] += 1
            unknown = sorted(c for c in named if c not in canonical)
            items.append(f"{name}: principle {p['statement'][:60]!r} resolves "
                         f"to {len(realized)} claim(s); not held: {unknown or 'none'}")
            continue
        pid = PRINCIPLE_PREFIX + slug(p["statement"])
        # Two agents can independently find the same principle; merge rather
        # than emit duplicates, keeping the union of evidence.
        justified = {canonical[c] for c in p.get("justifiedBy", []) if c in canonical}
        if pid in principles:
            principles[pid]["realized"].update(realized)
            principles[pid]["justified"].update(justified)
            principles[pid]["scope"].update(p.get("scope", []))
            principles[pid]["agents"] += 1
        else:
            principles[pid] = {
                "statement": p["statement"],
                "realized": set(realized),
                "justified": justified,
                "tension": p.get("tension"),
                "scope": set(p.get("scope", [])),
                "confidence": p.get("confidence", "low"),
                "agents": 1,
            }

    for e in payload.get("edges", []):
        src, dst = canonical.get(e.get("from")), canonical.get(e.get("to"))
        if src is None or dst is None:
            dropped["edge referencing unknown claim"] += 1
            unknown = [c for c in (e.get("from"), e.get("to")) if c not in canonical]
            items.append(f"{name}: {e.get('type')} edge; not held: {unknown}")
            continue
        mapped = EDGE_MAP.get(e.get("type"))
        if not mapped:
            dropped[f"unmapped edge type: {e.get('type')}"] += 1
            items.append(f"{name}: unmapped edge type {e.get('type')!r} "
                         f"from {e.get('from')} to {e.get('to')}")
            continue
        out.append({
            "source": src,
            "target": dst,
            "type": mapped,
            "direction": "bidirectional" if e["type"] == "tensions-with" else "forward",
            "description": f"{e['type']}: {e.get('rationale', '')}".strip(),
            "weight": 0.8,
        })
    return out


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
    units = {u["id"]: u for u in json.loads(args.doc_units.read_text())["docUnits"]}
    claims = {c["claimId"]: c
              for c in json.loads(args.design_claims.read_text())["claims"]}

    # ---- remove what an earlier run of this script wrote ----
    #
    # The natural workflow emits twice: once ungrounded, again after grounding,
    # with the first output as the second run's `--graph`. Appending to it would
    # leave a stale `ungrounded` node beside the fresh `supported` one under the
    # same id and name, which is the ambiguity this step exists to prevent.
    #
    # The stamp identifies this script's nodes. The id tests cover a graph
    # written before the stamp existed: every id this run will mint, every
    # claim node whose id has the shape only this pipeline mints, and the
    # principle namespace, which nothing else writes into. Matching the shape
    # matters because a claim id hashes its text: a reworded docstring leaves
    # the old node under an id no current claim has, with a truncated name and
    # no verdict, and nothing else would remove it.
    stale = {n["id"] for n in graph["nodes"]
             if n.get("emittedBy") == EMITTER
             or n["id"] in claims
             or (n.get("type") == "claim"
                 and PIPELINE_CLAIM_ID.fullmatch(n["id"]))
             or n["id"].startswith(PRINCIPLE_PREFIX)}
    nodes: list[dict[str, Any]] = [n for n in graph["nodes"]
                                   if n["id"] not in stale]
    edges: list[dict[str, Any]] = [e for e in graph["edges"]
                                   if e["source"] not in stale
                                   and e["target"] not in stale]
    for layer in graph.get("layers") or []:
        layer["nodeIds"] = [i for i in layer.get("nodeIds", []) if i not in stale]
    node_ids = {n["id"] for n in nodes}
    by_file: dict[str, str] = {n["filePath"]: n["id"] for n in nodes
                               if n["type"] == "file" and n.get("filePath")}

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
    # nodes. Occurrences are merged into one node, and `groundingOccurrences`
    # on that node keeps each occurrence's own verdict so the merge loses none.
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
    verdict_stats: collections.Counter = collections.Counter()
    # Per occurrence, before merging. This is the coverage measure: a `mixed`
    # node hides how many of its occurrences were graded, and an ungrounded
    # occurrence inside one is in no node-level count.
    occurrence_stats: collections.Counter = collections.Counter()
    emitted_claims = 0
    for cid, claim in sorted(claims.items()):
        unit = units.get(claim["docUnitId"])
        if unit is None:
            continue
        if canonical.get(cid) != cid:
            continue
        target, how = resolve(unit)
        # Every occurrence of this sentence, canonical first, each with the
        # verdict its own symbol earned and the docstring it was read from.
        members = sorted(text_group[canon_text(claim["claimText"])],
                         key=lambda m: (m != cid, m))
        occurrences = []
        # target node -> [(linkage, verdict)], one entry per occurrence on it
        linked: dict[str, list[tuple[str, str]]] = {}
        for member in members:
            g = claims[member].get("grounding") or {}
            member_unit = units[claims[member]["docUnitId"]]
            member_verdict = g.get("verdict") or UNGROUNDED
            member_target, member_how = resolve(member_unit)
            if member_target:
                linked.setdefault(member_target, []).append(
                    (member_how, member_verdict))
            occurrence_stats[member_verdict] += 1
            occurrences.append({
                "claimId": member,
                "attachedTo": member_unit["attachedTo"],
                "filePath": member_unit["sourceFile"],
                **({"lineRange": member_unit["docLineRange"]}
                   if member_unit.get("docLineRange") else {}),
                "groundingVerdict": member_verdict,
                **({"groundingAgreement": g["agreement"]}
                   if g.get("agreement") else {}),
                **({"scopeNote": g["scopeNote"]} if g.get("scopeNote") else {}),
            })
        cast = {o["groundingVerdict"] for o in occurrences}
        split = len(cast) > 1
        verdict = MIXED if split else occurrences[0]["groundingVerdict"]

        # A qualifier belongs to one occurrence's grading. The node states it
        # only when every occurrence carries the same value; otherwise it is
        # read per occurrence, so one symbol's bound or agreement is never
        # reported for another.
        def shared(key: str) -> Any:
            first = occurrences[0].get(key)
            if split or any(o.get(key) != first for o in occurrences):
                return None
            return first

        agreement = shared("groundingAgreement")
        note = shared("scopeNote")
        drift = "contradicted" in cast
        tags = sorted(set(claim["roles"]))
        tags.append(claim["subsystem"])
        for v in sorted(cast - STATUS_TAG.keys()):
            if v not in VERDICT_TAG:
                unmapped_verdicts[v] += 1
        tags.append(STATUS_TAG.get(verdict) or VERDICT_TAG.get(verdict, verdict))
        if split and drift:
            # A contradicted occurrence is a finding wherever it hides.
            tags.append(VERDICT_TAG["contradicted"])
        verdict_stats[verdict] += 1

        nodes.append({
            "id": cid,
            "type": "claim",
            # The full text, not an abbreviation. `name` is what a consumer
            # queries by, so truncating it made distinct claims that share a
            # long prefix indistinguishable. Distinct nodes cannot collide
            # here: the collapse above already merged every pair of claims
            # whose text agrees.
            "name": claim["claimText"],
            "filePath": unit["sourceFile"],
            "lineRange": unit.get("docLineRange"),
            "summary": claim["claimText"],
            "tags": tags,
            # `complexity` is a CLOSED enum (simple|moderate|complex) in core's
            # schema. Treat it as the binary "is this a finding" channel it
            # already is in practice: only `contradicted` is a defect a
            # maintainer must act on, including when it is one occurrence of a
            # split node. A scoped claim is true and stays at its ordinary level.
            "complexity": ("complex" if drift
                           else "moderate" if claim["passCount"] >= 4
                           else "simple"),
            # First-class fields rather than more overloading of `complexity`.
            # `GraphNodeSchema` is `.passthrough()`, so these survive validation
            # and save with zero TypeScript changes. (`GraphEdgeSchema` is not
            # passthrough — which is why verdict qualifiers must live on the
            # node, not the edge.)
            # Always present: see `UNGROUNDED` and `MIXED` above.
            "groundingVerdict": verdict,
            **({"groundingOccurrences": occurrences}
               if len(occurrences) > 1 else {}),
            **({"groundingAgreement": agreement} if agreement else {}),
            # Where the claim stops holding, with the citation that proves it.
            # This is the retrievable half of the withdrawn `overstated` verdict
            # — and it was already being computed by design_lens and dropped
            # here, so the graph asserted claims true without their boundaries.
            **({"scopeNote": note} if note else {}),
            "emittedBy": EMITTER,
            "knowledgeMeta": {
                "category": claim["roles"][0],
                "content": claim["claimText"] + (
                    f"\n\nAlso stated in {len(text_group[canon_text(claim['claimText'])]) - 1} "
                    f"other docstring(s)."
                    if len(text_group[canon_text(claim["claimText"])]) > 1 else "")
                # `.get` rather than `[...]`: design-claims files are inputs,
                # not necessarily this pipeline's own output, and a malformed
                # note should not abort the whole graph build with a KeyError.
                + (f"\n\nHolds as far as: {note['asSupported']}"
                   if isinstance(note, dict) and note.get("asSupported") else ""),
            },
        })
        emitted_claims += 1
        linkage_stats[how] += 1

        # One `documents` edge per symbol the sentence is stated on, each
        # described with the verdict earned on THAT symbol. A single edge to
        # the canonical symbol left every other symbol with no claim attached,
        # including the one whose occurrence was contradicted.
        for linked_target, on_target in linked.items():
            verdicts_here = {v for _, v in on_target}
            here = MIXED if len(verdicts_here) > 1 else next(iter(verdicts_here))
            edges.append({
                "source": cid,
                "target": linked_target,
                "type": "documents",
                "direction": "forward",
                "description": (f"documentation claim ({claim['passCount']}/5 "
                                f"extraction agreement"
                                + (f", {here}" if here in (UNGROUNDED, MIXED)
                                   else f", grounded {here}")
                                + f"; linkage: {on_target[0][0]})"),
                "weight": round(claim["passCount"] / 5, 2),
            })

    # ---- principles and design edges ----
    floor = CONFIDENCE[args.min_confidence]
    principles: dict[str, dict] = {}
    design_edges = 0
    dropped_edges: collections.Counter = collections.Counter()
    # One line per dropped item, naming its file and the ids at fault. The
    # counts alone left an operator unable to find the agent whose output was
    # dropped, or to tell an agent's error from a claim set the run never held.
    dropped_items: list[str] = []

    # Files that are not design-edges files the prompt describes. Each is named
    # and skipped, so one malformed agent output neither stops the emit nor
    # hides which agent to re-run.
    bad_edges: list[str] = []
    for path in args.edges:
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            reason = design_payload_fault(payload)
        if reason:
            bad_edges.append(f"BAD EDGES {path.name}: {reason}")
            continue
        added = read_design_edges(path.name, payload, canonical, floor, principles,
                                  dropped_edges, dropped_items)
        edges.extend(added)
        design_edges += len(added)

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
            "emittedBy": EMITTER,
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
        for scope in sorted(p["scope"]):
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

    if stale:
        print(f"replaced {len(stale)} node(s) written by an earlier emit")
    print(f"claims emitted:     {emitted_claims} ({assigned} placed in a layer)")
    if unplaced:
        print(f"  WARNING claims with no layer: {dict(unplaced)}")
    print(f"  linkage: {dict(linkage_stats)}")
    print(f"  grounding: {dict(sorted(verdict_stats.items()))}")
    print(f"  occurrences: {dict(sorted(occurrence_stats.items()))}")
    print(f"principles emitted: {len(principles)}")
    print(f"design edges:       {design_edges}")
    if unmapped_verdicts:
        print(f"  WARNING unmapped verdicts (raw string used as tag): "
              f"{dict(unmapped_verdicts)}")
    print(f"duplicate claims collapsed: {collapsed}")
    print(f"duplicate edges merged:     {duplicate_edges}")
    if dropped_edges:
        print(f"  dropped: {dict(dropped_edges)}")
        for item in dropped_items:
            print(f"    {item}")
    if dangling:
        print(f"  dangling edges removed: {dangling}")
    print(f"graph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
    print(f"→ {args.out}")
    for line in bad_edges:
        print(line, file=sys.stderr)
    return 1 if bad_edges else 0


if __name__ == "__main__":
    raise SystemExit(main())
