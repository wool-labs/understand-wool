#!/usr/bin/env python3
"""build_tiebreak_bundle.py — package only the claims two passes disagreed on.

Deterministic, stdlib only, no LLM.

Grounding runs two independent passes. Where they agree, the verdict stands;
where they differ, a third pass is dispatched on **just those claims**. This is
cheaper than a blanket third pass and targets effort where it is actually needed:
on wool's load-balancing subsystem 91.4% of verdicts were unanimous, so a full
third pass would have re-derived the same answer nine times out of ten.

The bundle deliberately carries **both prior verdicts and their reasoning**, and
the tie-break prompt asks the agent to adjudicate rather than start cold. That is
the opposite of the extraction passes, which are kept blind from each other — and
the difference is intentional. Extraction wants independent samples of the same
distribution. A tie-break wants the *best* answer, and the disagreement itself is
evidence: the observed splits are rarely about what the code does (both passes
usually find the same defect) but about how to label it — most often whether a
located boundary makes the claim `supported` with a scope note or `contradicted`.

Usage:
    python3 build_tiebreak_bundle.py --verdicts DIR --bundles DIR [--out DIR]

Output:
    <out>/tiebreak-<label>.json    one per source bundle that has disagreements
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--verdicts", type=Path, required=True)
    ap.add_argument("--bundles", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--also-claims", type=Path, default=None,
                    help="JSON list of claim IDs to adjudicate even where the "
                         "passes agreed — an escape hatch for claims a human "
                         "wants a third opinion on regardless of consensus")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    forced: set[str] = set()
    if args.also_claims:
        payload = json.loads(args.also_claims.read_text())
        forced = set(payload if isinstance(payload, list) else [])

    # claim -> its source bundle and metadata
    claim_meta: dict[str, dict] = {}
    claim_bundle: dict[str, str] = {}
    for path in sorted(args.bundles.glob("ground-*.json")):
        payload = json.loads(path.read_text())
        label = path.stem.replace("ground-", "")
        for unit in payload["units"]:
            for claim in unit["claims"]:
                claim_meta[claim["claimId"]] = {
                    **claim,
                    "sourceFile": unit["sourceFile"],
                    "qualname": unit["qualname"],
                    "groundingTarget": unit["groundingTarget"],
                    "candidateSites": unit.get("candidateSites", []),
                }
                claim_bundle[claim["claimId"]] = label

    votes: dict[str, list[dict]] = collections.defaultdict(list)
    for path in sorted(args.verdicts.glob("*.json")):
        payload = json.loads(path.read_text())
        for v in payload.get("verdicts", []):
            cid = v.get("claimId")
            if cid in claim_meta:
                votes[cid].append({
                    "pass": path.stem,
                    "verdict": v.get("verdict"),
                    "confidence": v.get("confidence"),
                    "reasoning": v.get("reasoning"),
                    "evidence": (v.get("evidence") or [])[:3],
                })

    disputed: dict[str, list[dict]] = collections.defaultdict(list)
    agreed = missing = forced_in = 0
    for cid, vs in votes.items():
        distinct = {v["verdict"] for v in vs}
        if len(vs) < 2:
            missing += 1
            continue
        if len(distinct) == 1:
            if cid not in forced:
                agreed += 1
                continue
            # Unanimous but structurally incomplete. The adjudicator sees two
            # identical priors and a shape marking it forced, which is the
            # question it is being asked to settle.
            forced_in += 1
        disputed[claim_bundle[cid]].append({
            **{k: claim_meta[cid][k] for k in
               ("claimId", "claimText", "claimType", "quantifier",
                "sourceFile", "qualname", "groundingTarget", "candidateSites")},
            "priorVerdicts": vs,
            "shape": (" vs ".join(sorted(distinct)) if len(distinct) > 1
                      else f"{next(iter(distinct))} (forced for review)"),
        })

    total_disputed = sum(len(v) for v in disputed.values())
    for label, claims in sorted(disputed.items()):
        shapes = collections.Counter(c["shape"] for c in claims)
        (args.out / f"tiebreak-{label}.json").write_text(json.dumps({
            "sourceBundle": label,
            "claimCount": len(claims),
            # Written, not just printed: the adjudication prompt reads these to
            # describe its own input instead of carrying run-specific counts
            # hard-coded in prose, which made the prompt single-use.
            "disagreementShapes": dict(shapes),
            "claims": sorted(claims, key=lambda c: c["claimId"]),
        }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    covered = agreed + total_disputed
    print(f"claims with 2+ votes: {covered}")
    if covered:
        print(f"  agreed:   {agreed}  ({agreed / covered:.1%})")
        print(f"  DISPUTED: {total_disputed}  ({total_disputed / covered:.1%})")
    if forced_in:
        print(f"  of those, {forced_in} were unanimous but forced in via --also-claims")
    if missing:
        print(f"  only one vote (bundle incomplete): {missing}")
    for label, claims in sorted(disputed.items()):
        pairs = collections.Counter(c["shape"] for c in claims)
        print(f"  → tiebreak-{label}.json  {len(claims)} claims  {dict(pairs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
