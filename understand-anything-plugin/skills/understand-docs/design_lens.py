#!/usr/bin/env python3
"""design_lens.py — tag claims by design role and block them for relation extraction.

Phase C of the documentation claim graph, retargeted for *understanding* rather
than validation (see CLAIM-GRAPH-PLAN.md §1.9, §2). Deterministic, stdlib only,
no LLM, no embeddings.

The validation pipeline asks "is this claim true?". A design graph asks "why is
the system shaped this way?", and that question needs different structure:

**Design roles.** A claim's grammatical shape predicts its explanatory function.
Measured over 960 factual claims from three wool subsystems, ~10% carry
design-philosophy language and the rest are mechanical detail. The roles below
separate them:

    rationale       why a choice was made — "coupling X with Y forces every
                    balancer to reimplement retry boilerplate"
    responsibility  who owns what — "eviction is WorkerProxy's responsibility"
    invariant       a guarantee — "a uid always resolves to the latest record"
    lifecycle       ownership over time — "a retired pool's cursor is reclaimed
                    with the context"
    omission        deliberate non-action — "a refresh mid-cycle needs no
                    handling here"
    mechanism       everything else; the bulk, and the least explanatory

Roles are not exclusive: "channel lifetime belongs to the pooling layer rather
than to the holder of a handle" is responsibility *and* lifecycle, and that
overlap is signal, not noise.

**Blocking.** All-pairs over 1,033 claims is ~530k comparisons and cannot be sent
to a model. Claims are instead blocked by **shared code symbol**, which for this
corpus is a strong and free signal: claims that mention `WorkerProxy` are about
`WorkerProxy`. Symbols are extracted as CamelCase identifiers, dotted paths, and
snake_case names — the three shapes wool's docstrings actually use.

Two block kinds are emitted:

    symbol      claims sharing a code symbol, within and across subsystems.
                Cross-subsystem symbol blocks are where a recurring principle
                becomes visible.
    rationale   every rationale/omission claim, paired with the mechanism claims
                from its own doc unit and symbol. These are the `justifies` edge
                candidates, and they are cheap because rationale is rare.

Usage:
    python3 design_lens.py CLAIMS.json [CLAIMS.json ...] [--grounded G.json ...]
        [--min-block 2] [--max-block 40] [--out DIR]

Output:
    <out>/design-claims.json    every claim, tagged with roles and symbols
    <out>/design-blocks.json    blocks for the relation-extraction agents
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"

# Ordered: the first match that fires is the claim's primary role.
ROLE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("rationale", re.compile(
        r"\b(because|forces|would (?:never|otherwise|leak|trade)|enables|ensures|"
        r"avoids|prevents|so that|makes .* brittle|is the safe default|"
        r"trades?|suited to|at the cost of)\b", re.I)),
    ("responsibility", re.compile(
        r"\b(owns?|responsib\w+|belongs to|delegat\w+|is not responsible|"
        r"the .*'s (?:only|sole) )\b", re.I)),
    ("omission", re.compile(
        r"\b(needs no|no handling|deliberately|intentionally|rather than|"
        r"instead of|does not (?:close|handle|touch|read|mutate)|never (?:closes|handles))\b",
        re.I)),
    ("lifecycle", re.compile(
        r"\b(lifetime|reclaimed|retired|leaks?|teardown|pinned|shutdown|"
        r"disposed|survives|for the life of)\b", re.I)),
    ("invariant", re.compile(
        r"\b(never|always|guarantee\w*|invariant|must not|cannot|"
        r"is fixed for|remains? stable)\b", re.I)),
]

# CamelCase (WorkerProxy), dotted (ctx.remove_worker), snake_case (remove_worker).
SYMBOL_RES = [
    re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b"),
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*\.[a-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b"),
]

# Words that look like symbols but carry no referential weight.
SYMBOL_STOP = {
    "async_generator", "context_manager", "type_check", "read_only", "per_attempt",
    "round_robin", "load_balancer", "worker_pool", "no_op", "op_out",
}


def roles(text: str, claim_type: str) -> list[str]:
    found = [name for name, pattern in ROLE_PATTERNS if pattern.search(text)]
    if claim_type == "aspirational" and "rationale" not in found:
        found.insert(0, "rationale")
    return found or ["mechanism"]


def symbols(text: str) -> list[str]:
    out: set[str] = set()
    for pattern in SYMBOL_RES:
        for match in pattern.findall(text):
            token = match.strip(".")
            if token.lower() in SYMBOL_STOP or len(token) < 4:
                continue
            out.add(token)
    # A dotted path implies its owner: `ctx.remove_worker` also indexes as
    # `remove_worker`, so a claim naming the method and one naming the call site
    # land in the same block.
    for token in list(out):
        if "." in token:
            out.add(token.split(".")[-1])
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("claims", nargs="+", type=Path)
    ap.add_argument("--grounded", nargs="*", type=Path, default=[],
                    help="grounded-*.json files, to attach verdicts and evidence")
    ap.add_argument("--min-block", type=int, default=2)
    ap.add_argument("--max-block", type=int, default=40)
    ap.add_argument("--out", type=Path, default=Path("."))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    # Grounding, where it exists. Absence is normal: only one subsystem is ground-
    # ed so far, and a design graph should not wait on full-corpus verification.
    # `role` must survive. A scope note is defined by its *limiting* citation —
    # the one showing where the documented behaviour stops — and dropping the
    # role leaves a claim asserting a boundary with no visible reason. Bound-role
    # items are also sorted first, so the truncation below cannot discard the
    # citation that carries the finding.
    BOUND_ROLES = {"limits", "contradicts"}

    def project(evidence: list[dict]) -> list[dict]:
        ranked = sorted(
            evidence or [],
            key=lambda e: (e.get("role") or "").strip().lower() not in BOUND_ROLES,
        )
        return [
            {"file": e.get("file", "").split("src-stripped/")[-1],
             "lines": e.get("lines"),
             **({"role": e["role"]} if e.get("role") else {})}
            for e in ranked[:3]
        ]

    verdicts: dict[str, dict] = {}
    for path in args.grounded:
        for claim in json.loads(path.read_text())["claims"]:
            verdicts[claim["claimId"]] = {
                "verdict": claim["verdict"],
                "agreement": claim["agreement"],
                "evidence": project(claim.get("evidence")),
                **({"scopeNote": claim["scopeNote"]}
                   if claim.get("scopeNote") else {}),
            }

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in args.claims:
        payload = json.loads(path.read_text())
        subsystem = (payload.get("stats", {}).get("subsystem")
                     or path.stem.replace("claims-", "").replace("-all", ""))
        for claim in payload["claims"]:
            if claim["id"] in seen:
                continue
            seen.add(claim["id"])
            records.append({
                "claimId": claim["id"],
                "claimText": claim["claimText"],
                "claimType": claim["claimType"],
                "quantifier": claim["quantifier"],
                "passCount": claim["passCount"],
                "docUnitId": claim["docUnitId"],
                "subsystem": subsystem.replace("layer:", ""),
                "roles": roles(claim["claimText"], claim["claimType"]),
                "symbols": symbols(claim["claimText"]),
                "grounding": verdicts.get(claim["id"]),
            })

    by_id = {r["claimId"]: r for r in records}

    # ---- blocks ----
    by_symbol: dict[str, list[str]] = collections.defaultdict(list)
    for r in records:
        for sym in r["symbols"]:
            by_symbol[sym].append(r["claimId"])

    blocks: list[dict[str, Any]] = []

    for sym, ids in sorted(by_symbol.items()):
        if not (args.min_block <= len(ids) <= args.max_block):
            continue
        subs = {by_id[i]["subsystem"] for i in ids}
        role_mix = collections.Counter(
            role for i in ids for role in by_id[i]["roles"])
        # A block worth an agent has some explanatory content; an all-mechanism
        # block yields "X does Y, and also X does Z", which is not a design edge.
        if role_mix.keys() <= {"mechanism"}:
            continue
        blocks.append({
            "blockId": f"symbol:{sym}",
            "kind": "symbol",
            "symbol": sym,
            "subsystems": sorted(subs),
            "crossSubsystem": len(subs) > 1,
            "claimIds": sorted(ids),
            "roleMix": dict(role_mix),
        })

    # Rationale blocks: each rationale/omission claim with the claims most likely
    # to be what it explains — the `justifies` candidates.
    #
    # Neighbours are RANKED, not merely collected. Plain symbol overlap is far too
    # loose here: `WorkerProxy` appears in 130+ claims, so every rationale claim
    # mentioning it pulled in the same undifferentiated 40, and two unrelated
    # rationales produced near-identical blocks. Ranking uses inverse symbol
    # frequency, so a shared rare symbol (`_delegate_dispatch`) counts for much
    # more than a shared ubiquitous one (`WorkerProxy`).
    for r in records:
        if not ({"rationale", "omission"} & set(r["roles"])):
            continue
        anchor_syms = set(r["symbols"])
        scored: list[tuple[float, str]] = []
        for o in records:
            if o["claimId"] == r["claimId"]:
                continue
            shared = anchor_syms & set(o["symbols"])
            same_unit = o["docUnitId"] == r["docUnitId"]
            if not shared and not same_unit:
                continue
            score = 3.0 if same_unit else 0.0
            # Inverse frequency: a symbol in 2 claims is far more discriminative
            # than one in 130.
            score += sum(1.0 / len(by_symbol[s]) for s in shared) * 10
            # An explanatory neighbour is a better edge candidate than one more
            # mechanical fact.
            if set(o["roles"]) - {"mechanism"}:
                score += 1.0
            scored.append((score, o["claimId"]))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[0], x[1]))
        picked = [cid for _, cid in scored[:args.max_block]]
        subs = {r["subsystem"]} | {by_id[i]["subsystem"] for i in picked}
        blocks.append({
            "blockId": f"rationale:{r['claimId'][6:18]}",
            "kind": "rationale",
            "anchorClaimId": r["claimId"],
            "anchorText": r["claimText"],
            "subsystems": sorted(subs),
            "crossSubsystem": len(subs) > 1,
            "claimIds": picked,
            # Computed over the claims actually in the block, not the pre-cut set.
            "roleMix": dict(collections.Counter(
                role for i in picked for role in by_id[i]["roles"])),
        })

    blocks.sort(key=lambda b: (-len(b["claimIds"]), b["blockId"]))

    (args.out / "design-claims.json").write_text(json.dumps({
        "schemaVersion": SCHEMA_VERSION,
        "stats": {
            "claims": len(records),
            "subsystems": sorted({r["subsystem"] for r in records}),
            "byRole": dict(collections.Counter(
                role for r in records for role in r["roles"]).most_common()),
            "grounded": sum(1 for r in records if r["grounding"]),
            "withSymbols": sum(1 for r in records if r["symbols"]),
        },
        "claims": records,
    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    (args.out / "design-blocks.json").write_text(json.dumps({
        "schemaVersion": SCHEMA_VERSION,
        "stats": {
            "blocks": len(blocks),
            "byKind": dict(collections.Counter(b["kind"] for b in blocks)),
            "crossSubsystem": sum(1 for b in blocks if b["crossSubsystem"]),
            "pairsIfAllPairs": len(records) * (len(records) - 1) // 2,
            "claimSlotsInBlocks": sum(len(b["claimIds"]) for b in blocks),
        },
        "blocks": blocks,
    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    role_counts = collections.Counter(role for r in records for role in r["roles"])
    print(f"{len(records)} claims from {len({r['subsystem'] for r in records})} subsystems")
    for role, n in role_counts.most_common():
        print(f"  {n:>5}  {role}")
    print(f"\n{len(blocks)} blocks "
          f"({sum(1 for b in blocks if b['crossSubsystem'])} cross-subsystem) "
          f"vs {len(records) * (len(records) - 1) // 2:,} if all-pairs")
    print(f"→ {args.out}/design-claims.json")
    print(f"→ {args.out}/design-blocks.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
