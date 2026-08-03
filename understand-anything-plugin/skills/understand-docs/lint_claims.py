#!/usr/bin/env python3
"""lint_claims.py — catch claims that assert more than the docstring did.

Deterministic, stdlib only, no LLM. Reads extracted or reconciled claims and
compares each `claimText` against the `sourceQuote` it came from.

WHY THIS EXISTS

Grounding grades a claim against code. If extraction has already widened the
claim past what the docstring said, grounding correctly finds a counterexample
and reports a documentation defect that the documentation never committed — a
bug we filed against someone else's repo, in our own wording. Measured on wool:
3 of the 63 claims that carried a located boundary were manufactured this way
(~5%), so this is a narrow leak rather than a systemic one — but it is invisible
without a check like this, and the only alternative is re-running a 5-pass
extraction to find out.

The two checks correspond to the two amended canonical-form rules in
`prompts/extract-claims.md`:

  DROPPED QUALIFIER   the quote narrows with `when` / `only` / `unless` / `via`
                      and the claim does not carry it. Rule 5.
  ONE-SIDED SPLIT     the quote coordinates two things and the claim asserts one
                      side while dropping every distinguishing word of the
                      other. Rule 6.

Both checks report *lost material*, not direction. A claim that dropped "or None
if not started" is stronger than the docstring; one that dropped "and its
services" is weaker. Only the first can manufacture a false verdict, but the
second is still an extraction miss, and separating them needs a reader.

NOT DETECTED, deliberately: subject re-attachment — *"...so the pool can
register it"* extracted as *"`start` registers the worker with the pool"*. The
subject legitimately comes from the doc unit rather than the quote (rule 4), so
"subject absent from quote" fires on nearly every well-formed claim. That case
needs a reader, and this file does not pretend otherwise.

MEASURED BASELINE (wool, 813 claims, the corpus these rules were derived from):
3 dropped qualifiers, 7 one-sided splits — 1.2%, and every one a real loss on
inspection. Restricted to the 53 claims that carried a boundary: 1 flag. Treat a
jump above that as a prompt regression, not a corpus that suddenly got worse.

Usage:
    python3 lint_claims.py CLAIMS.json [CLAIMS.json ...]   reconciled claim sets
    python3 lint_claims.py --bundles DIR                   grounding bundles
    python3 lint_claims.py ... --verdicts GROUNDED.json    restrict to one verdict
    python3 lint_claims.py ... --max-flags N               exit 1 above N flags
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Words that narrow the assertion itself rather than its subject. Dropping one
# changes what is being claimed; rule 5 forbids it.
QUALIFIERS = ("when", "if", "unless", "only", "except", "via", "while",
              "during", "after", "before", "once", "provided", "until")

COORDINATORS = (" and ", " or ")

STOP = {"the", "a", "an", "this", "that", "these", "those", "its", "it", "is",
        "are", "was", "were", "be", "been", "to", "of", "for", "in", "on", "at",
        "by", "with", "from", "as", "not", "no", "and", "or", "but", "so",
        "which", "each", "any", "all", "into", "onto", "then"}

MARKUP = re.compile(r"(:[a-z]+:)?`+|\*+|_{2,}|<[^>]+>")
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def clean(text: str) -> str:
    """Strip reST/markdown markup and collapse whitespace."""
    return re.sub(r"\s+", " ", MARKUP.sub(" ", text or "")).strip()


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text)}


def content(text: str) -> set[str]:
    return {w for w in words(text) if len(w) > 2 and w not in STOP}


def dropped_qualifiers(claim: str, quote: str) -> list[str]:
    """Qualifiers in the quote whose *governed clause* is missing from the claim.

    Checking for the bare word alone is far too noisy: a claim routinely
    rephrases *"If True, send keepalive pings"* as *"...set to True sends
    keepalive pings"*, which carries the condition without the word `if`. What
    matters is whether the thing being conditioned on survived. So the flag
    requires that the qualifier is absent **and** none of the content words it
    governs made it into the claim. On wool that took the check from 8 flags at
    ~40% precision to 3 at 100%.
    """
    cw, qw = words(claim), words(quote)
    tokens = [w.lower() for w in WORD.findall(quote)]
    flagged = []
    for q in QUALIFIERS:
        if q not in qw or q in cw:
            continue
        # The clause the qualifier introduces, approximated by the next few words.
        try:
            i = tokens.index(q)
        except ValueError:
            continue
        governed = {w for w in tokens[i + 1:i + 5] if len(w) > 2 and w not in STOP}
        if not governed or not (governed & cw):
            flagged.append(q)
    return flagged


def one_sided_split(claim: str, quote: str, siblings: list[str]) -> str | None:
    """Return a description when a coordination lost a side and nobody kept it.

    Rule 6 *instructs* splitting — *"Start the worker and register it with the
    pool"* is meant to become two claims, and each of them looks one-sided on
    its own. The discriminator is whether the other side survived **somewhere in
    the same doc unit**: a proper split leaves a sibling claim carrying the
    dropped words, a mangled coordination leaves nothing. Without this check the
    flag fires on every correct split (30 flags on wool, nearly all legitimate);
    with it, only genuinely lost material is reported.
    """
    if any(c in f" {claim.lower()} " for c in COORDINATORS):
        return None  # the claim coordinates too — nothing was dropped
    low = f" {quote.lower()} "
    for coord in COORDINATORS:
        if coord not in low:
            continue
        left, right = low.split(coord, 1)
        lset, rset = content(left), content(right)
        # Only the words unique to a side distinguish it.
        lonly, ronly = lset - rset, rset - lset
        if not lonly or not ronly:
            continue
        cw = content(claim)
        keeps_left, keeps_right = bool(lonly & cw), bool(ronly & cw)
        if keeps_left == keeps_right:
            continue
        dropped = ronly if keeps_left else lonly
        if any(dropped & content(s) for s in siblings):
            continue  # a sibling claim from the same doc unit kept that side
        return (f"kept {'left' if keeps_left else 'right'} of '{coord.strip()}', "
                f"dropped {sorted(dropped)[:6]} — no sibling claim carries it")
    return None


def load_claims(args: argparse.Namespace) -> list[dict]:
    out: list[dict] = []
    for path in args.claims:
        payload = json.loads(path.read_text())
        for c in payload.get("claims", []):
            out.append({"claimId": c.get("id") or c.get("claimId"),
                        "docUnitId": c.get("docUnitId"),
                        "claimText": c.get("claimText", ""),
                        "sourceQuote": c.get("sourceQuote", ""),
                        "source": path.name})
    if args.bundles:
        for path in sorted(args.bundles.glob("ground-*.json")):
            payload = json.loads(path.read_text())
            for unit in payload.get("units", []):
                for c in unit.get("claims", []):
                    out.append({"claimId": c.get("claimId"),
                                "docUnitId": unit.get("docUnitId"),
                                "claimText": c.get("claimText", ""),
                                "sourceQuote": c.get("sourceQuote", ""),
                                "source": path.name})
    # Bundles overlap by construction (a claim appears once per bundle only, but
    # several inputs may cover the same run). Dedupe on identity, not on text.
    seen: dict[str, dict] = {}
    for c in out:
        seen.setdefault(c["claimId"] or c["claimText"], c)
    return list(seen.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("claims", nargs="*", type=Path)
    ap.add_argument("--bundles", type=Path, default=None,
                    help="directory of ground-*.json bundles")
    ap.add_argument("--verdicts", type=Path, default=None,
                    help="grounded-*.json; restrict to claims with --verdict")
    ap.add_argument("--verdict", default="contradicted")
    ap.add_argument("--max-flags", type=int, default=None,
                    help="exit 1 when total flags exceed this")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    claims = load_claims(args)
    if not claims:
        print("no claims found — pass claim files or --bundles", file=sys.stderr)
        return 2

    # Sibling lookup is built over the *whole* input, before any verdict filter:
    # the claim that kept the other half of a coordination is usually
    # `supported`, so restricting the corpus first would resurrect every
    # legitimate split as a false flag.
    by_unit: dict[str, list[str]] = {}
    for c in claims:
        by_unit.setdefault(c.get("docUnitId") or "", []).append(c["claimText"])

    scope = ""
    if args.verdicts:
        grounded = json.loads(args.verdicts.read_text())
        keep = {c["claimId"] for c in grounded["claims"] if c["verdict"] == args.verdict}
        claims = [c for c in claims if c["claimId"] in keep]
        scope = f" (restricted to verdict `{args.verdict}`)"

    qual_flags, split_flags = [], []
    for c in claims:
        claim, quote = clean(c["claimText"]), clean(c["sourceQuote"])
        if not quote:
            continue
        lost = dropped_qualifiers(claim, quote)
        if lost:
            qual_flags.append((c, lost))
        siblings = [s for s in by_unit.get(c.get("docUnitId") or "", [])
                    if s != c["claimText"]]
        split = one_sided_split(claim, quote, siblings)
        if split:
            split_flags.append((c, split))

    print(f"lint_claims: {len(claims)} claims{scope}")
    print(f"  dropped qualifier : {len(qual_flags)}")
    print(f"  one-sided split   : {len(split_flags)}")

    if not args.quiet:
        for c, lost in qual_flags:
            print(f"\n  QUALIFIER {lost}  [{c['source']}]")
            print(f"    claim: {c['claimText']}")
            print(f"    quote: {clean(c['sourceQuote'])}")
        for c, why in split_flags:
            print(f"\n  SPLIT {why}  [{c['source']}]")
            print(f"    claim: {c['claimText']}")
            print(f"    quote: {clean(c['sourceQuote'])}")

    total = len(qual_flags) + len(split_flags)
    if args.max_flags is not None and total > args.max_flags:
        print(f"\nFAIL: {total} flags exceeds --max-flags {args.max_flags}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
