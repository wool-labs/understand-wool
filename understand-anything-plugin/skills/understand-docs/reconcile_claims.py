#!/usr/bin/env python3
"""reconcile_claims.py — merge N independent extraction passes into a claim set.

Phase B tier-1 + tier-2 of the documentation claim graph (see CLAIM-GRAPH-PLAN.md
§1.8). Deterministic, stdlib only, no LLM and no embeddings.

Given N passes of the identical extraction task over one bundle, produce the set
of claims that survive quorum, plus the agreement statistics that tell you
whether the extraction prompt is good enough to proceed.

Two tiers:

  TIER 1  exact match on `contentHash(docUnitId + normalize(claimText))`.
          Free. Normalization strips reST/markdown markup, articles, and
          possessives — markup alone was worth 18 points of agreement on wool.

  TIER 2  single-linkage lexical clustering within one doc unit, over
          `difflib` similarity, **gated by sourceQuote overlap**.

The quote gate is the load-bearing half. Lexical similarity systematically
mis-merges the two halves of a disjunction — precisely the claims the extraction
prompt instructs agents to split, since the halves share nearly the whole
sentence stem. Measured on wool: bad merges scored 0.871/0.903 on claim text,
good merges 0.929/0.890 — claim similarity alone cannot separate them. But good
merges quote the *identical* span (1.000) and bad merges quote different spans
(0.636, 0.556). Requiring quote overlap blocks the failure for ~2 points of
agreement.

Quorum counts **distinct pass indices** in a cluster, never a sum. Summing lets
one pass clear quorum by itself when it emits two near-duplicate wordings of the
same assertion from one docstring — which is more likely for a hallucinated
claim than a real one.

Usage:
    python3 reconcile_claims.py PASS.json [PASS.json ...] \
        [--quorum 3] [--threshold 0.85] [--quote-gate 0.7] [--out DIR]

Output:
    <out>/claims-<bundle>.json           surviving claims, with variants
    <out>/claims-rejected-<bundle>.json  sub-quorum claims, never silently dropped
"""

from __future__ import annotations

import argparse
import collections
import difflib
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"

MARKUP_RE = [
    (re.compile(r"``([^`]+)``"), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\*\*?([^*]+)\*\*?"), r"\1"),
    (re.compile(r":[a-z]+:"), " "),
    (re.compile(r"[~`*_]"), ""),
]
ARTICLE_RE = re.compile(r"\b(the|a|an)\s+")
POSSESSIVE_RE = re.compile(r"'s\b")
# Length asymmetry beyond this makes a similarity score meaningless.
LENGTH_SKEW = 0.4


def normalize(text: str) -> str:
    for pattern, repl in MARKUP_RE:
        text = pattern.sub(repl, text)
    text = unicodedata.normalize("NFC", text).casefold()
    text = ARTICLE_RE.sub(" ", text)
    text = POSSESSIVE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t.,;:!?-—–")


def quote_tokens(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def claim_id(doc_unit: str, normalized: str) -> str:
    return "claim:" + hashlib.sha256(f"{doc_unit}:{normalized}".encode()).hexdigest()


def cluster(keys: list[str], quotes: dict[str, set[frozenset[str]]],
            threshold: float, quote_gate: float) -> list[list[str]]:
    """Single-linkage connected components. Order-independent by construction —
    greedy pairwise merging would depend on visit order, since similarity is not
    transitive."""
    keys = sorted(keys)
    parent = {k: k for k in keys}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if abs(len(a) - len(b)) / max(len(a), len(b)) > LENGTH_SKEW:
                continue
            if difflib.SequenceMatcher(None, a, b).ratio() < threshold:
                continue
            if quote_gate:
                best = max((jaccard(qa, qb) for qa in quotes[a] for qb in quotes[b]),
                           default=0.0)
                if best < quote_gate:
                    continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

    groups: dict[str, list[str]] = collections.defaultdict(list)
    for k in keys:
        groups[find(k)].append(k)
    return [sorted(v) for v in groups.values()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("passes", nargs="+", type=Path)
    ap.add_argument("--quorum", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=0.85)
    ap.add_argument("--quote-gate", type=float, default=0.7)
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--label", default=None)
    # illustrative/aspirational claims never reach Phase E grounding, and they
    # do not canonicalize: a code example has no single right phrasing, so five
    # independent passes produce five different claims about it. On wool they
    # are 7% of volume and cost ~6 points of measured agreement. Filtered before
    # reconciliation, retained in the excluded file — never silently dropped.
    ap.add_argument("--types", default="factual,instructional",
                    help="comma-separated claimTypes to reconcile; 'all' to disable")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    # normalized text -> {passes, quotes, representative claim}
    keep_types = (None if args.types.strip().lower() == "all"
                  else {s.strip() for s in args.types.split(",") if s.strip()})

    units: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    subsystem = None
    per_pass: list[int] = []
    excluded: list[dict[str, Any]] = []
    excluded_types: collections.Counter = collections.Counter()

    for idx, path in enumerate(sorted(args.passes), start=1):
        payload = json.loads(path.read_text())
        subsystem = subsystem or payload.get("subsystem")
        claims = payload["claims"]
        per_pass.append(len(claims))
        for claim in claims:
            if keep_types is not None and claim.get("claimType") not in keep_types:
                excluded_types[claim.get("claimType")] += 1
                excluded.append({"pass": idx, **claim})
                continue
            key = normalize(claim["claimText"])
            if not key:
                continue
            entry = units[claim["docUnitId"]].setdefault(
                key, {"passes": set(), "quotes": set(), "claims": []})
            entry["passes"].add(idx)
            entry["quotes"].add(quote_tokens(claim.get("sourceQuote")))
            entry["claims"].append(claim)

    tier1_clusters = sum(len(g) for g in units.values())

    survivors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    support_hist: collections.Counter = collections.Counter()

    for doc_unit, groups in sorted(units.items()):
        quotes = {k: v["quotes"] for k, v in groups.items()}
        for keys in cluster(list(groups), quotes, args.threshold, args.quote_gate):
            passes: set[int] = set()
            variants: list[str] = []
            for k in keys:
                passes |= groups[k]["passes"]
                variants.extend(c["claimText"] for c in groups[k]["claims"])
            support_hist[len(passes)] += 1

            # Canonical text is the lexicographically smallest *normalized*
            # variant; the ID is recomputed from it so it is a function of
            # content, not of which pass happened to be visited first.
            canonical_key = min(keys)
            rep = groups[canonical_key]["claims"][0]
            record = {
                "id": claim_id(doc_unit, canonical_key),
                "docUnitId": doc_unit,
                "claimText": rep["claimText"],
                "claimType": collections.Counter(
                    c["claimType"] for k in keys for c in groups[k]["claims"]).most_common(1)[0][0],
                "quantifier": collections.Counter(
                    c["quantifier"] for k in keys for c in groups[k]["claims"]).most_common(1)[0][0],
                "fieldIndex": rep.get("fieldIndex"),
                "sourceQuote": rep.get("sourceQuote"),
                "extractionPasses": sorted(passes),
                "passCount": len(passes),
                "variants": sorted(set(variants)),
            }
            (survivors if len(passes) >= args.quorum else rejected).append(record)

    survivors.sort(key=lambda c: (c["docUnitId"], -c["passCount"], c["id"]))
    rejected.sort(key=lambda c: (c["docUnitId"], -c["passCount"], c["id"]))

    total = len(survivors) + len(rejected)
    avg = sum(per_pass) / len(per_pass) if per_pass else 0
    ge2 = sum(v for k, v in support_hist.items() if k >= 2)
    label = args.label or (subsystem or "claims").replace("layer:", "").replace(":", "_")

    stats = {
        "subsystem": subsystem,
        "passes": len(args.passes),
        "claimsPerPass": per_pass,
        "avgClaimsPerPass": round(avg, 1),
        "tier1Clusters": tier1_clusters,
        "tier2Clusters": total,
        "mergedByTier2": tier1_clusters - total,
        "agreementGE2": round(ge2 / total, 4) if total else 0,
        "agreementGEQuorum": round(len(survivors) / total, 4) if total else 0,
        "supportHistogram": {str(k): support_hist[k] for k in sorted(support_hist)},
        "survivors": len(survivors),
        "rejected": len(rejected),
        "excludedByType": dict(sorted(excluded_types.items())),
        "excludedTotal": len(excluded),
        "params": {"quorum": args.quorum, "threshold": args.threshold,
                   "quoteGate": args.quote_gate, "types": args.types},
    }

    def write(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2, sort_keys=True,
                                   ensure_ascii=False) + "\n")

    write(args.out / f"claims-{label}.json",
          {"schemaVersion": SCHEMA_VERSION, "stats": stats, "claims": survivors})
    write(args.out / f"claims-rejected-{label}.json",
          {"schemaVersion": SCHEMA_VERSION, "note": "sub-quorum; retained deliberately",
           "stats": stats, "claims": rejected})
    if excluded:
        write(args.out / f"claims-excluded-{label}.json",
              {"schemaVersion": SCHEMA_VERSION,
               "note": "filtered by claimType before reconciliation; not reconciled, not lost",
               "stats": stats, "claims": excluded})

    print(f"{label}: {per_pass} claims/pass (avg {avg:.0f})")
    print(f"  tier1 {tier1_clusters} → tier2 {total} clusters ({tier1_clusters - total} merged)")
    print(f"  >={2}/{len(args.passes)}: {stats['agreementGE2']:.1%}   "
          f">={args.quorum}/{len(args.passes)}: {stats['agreementGEQuorum']:.1%}")
    print(f"  survivors {len(survivors)}, rejected {len(rejected)}"
          + (f", excluded {len(excluded)} {dict(excluded_types)}" if excluded else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
