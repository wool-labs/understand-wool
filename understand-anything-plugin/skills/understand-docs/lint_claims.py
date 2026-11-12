#!/usr/bin/env python3
"""lint_claims.py — catch claims that assert more than the docstring did.

Deterministic, stdlib only, no LLM. Reads extracted or reconciled claims and
compares each `claimText` against the `sourceQuote` it came from.

WHY THIS EXISTS

Grounding grades a claim against code. If extraction has already widened the
claim past what the docstring said, grounding correctly finds a counterexample
and reports a documentation defect that the documentation never committed — a
bug we filed against someone else's repo, in our own wording. On wool this is a
narrow leak rather than a systemic one (see the baseline below) — but it is
invisible without a check like this, and the only alternative is re-running a
5-pass extraction to find out.

The two checks correspond to the two amended canonical-form rules in
`prompts/extract-claims.md`:

  DROPPED QUALIFIER   the quote narrows with `when` / `only` / `unless` / `via`
                      and the claim does not carry it. Rule 5.
  ONE-SIDED SPLIT     the quote coordinates two things and the claim asserts one
                      side while dropping every distinguishing word of the
                      other. Rule 6.

A third check runs when `--doc-units` is given. The other two depend on it, so
it is printed first and it alone decides the exit code without `--max-flags`:

  QUOTE NOT IN UNIT   the `sourceQuote` does not appear verbatim, up to
                      whitespace, in the `text` of the doc unit the claim names.
                      The extraction prompt tells agents this is checked; this is
                      the check. A quote the docstring does not contain makes the
                      reconciler's merge gate and both checks above reason from
                      nothing. Any such quote exits 1.

The reconciler keeps one representative quote per surviving claim, so checking
the reconciled claims covers one quote per cluster. Every quote is covered by
running `--quotes-only` over the extraction pass files before reconciling.

The first two checks report *lost material*, not direction. A claim that dropped "or None
if not started" is stronger than the docstring; one that dropped "and its
services" is weaker. Only the first can manufacture a false verdict, but the
second is still an extraction miss, and separating them needs a reader.

NOT DETECTED, deliberately: subject re-attachment — *"...so the pool can
register it"* extracted as *"`start` registers the worker with the pool"*. The
subject legitimately comes from the doc unit rather than the quote (rule 4), so
"subject absent from quote" fires on nearly every well-formed claim. That case
needs a reader, and this file does not pretend otherwise.

MEASURED BASELINE (wool's 813-claim grounding set, the corpus these rules were
derived from): 3 dropped qualifiers and 7 one-sided splits, 10 flags or 1.2%.
One of the 10 falls on the 53 claims of that set that the four-verdict round
graded `overstated`. With `--doc-units`, every one of the 813 quotes is found
in its doc unit, when each subsystem is checked against the doc-units file of
the harvest it was extracted from; the two subsystems come from two harvests,
and one harvest's file alone leaves the other's doc units unknown. Treat a
jump above that as a prompt regression, not a corpus that suddenly got worse.

Usage:
    python3 lint_claims.py CLAIMS.json [CLAIMS.json ...]   reconciled claim sets
    python3 lint_claims.py --bundles DIR                   grounding bundles
    python3 lint_claims.py ... --doc-units DOC-UNITS.json  also check each quote
    python3 lint_claims.py PASS.json ... --doc-units DOC-UNITS.json --quotes-only
    python3 lint_claims.py ... --verdicts GROUNDED.json    restrict to one verdict
    python3 lint_claims.py ... --max-flags N               exit 1 above N flags

Exit codes: 0 clean; 1 when a quote is not in its doc unit, when a file given to
`--quotes-only` holds no claims, or when the two review checks together exceed
`--max-flags`; 2 when no claims were read at all, or `--quotes-only` is given
without `--doc-units`.
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


def squeeze(text: str) -> str:
    """Collapse every run of whitespace to one space. Nothing else changes."""
    return re.sub(r"\s+", " ", text or "").strip()


def quote_missing(quote: str, unit_text: str) -> bool:
    """Whether `quote` fails to appear verbatim in the doc unit's text.

    Verbatim up to whitespace: a docstring wraps its lines, and a quote that
    spans a line break is still a quote. Markup, case, and punctuation must
    match, because the extraction prompt asks for the span as written and the
    reconciler's merge gate and the two checks below all reason from it. A quote
    the docstring does not contain is evidence of nothing.
    """
    return squeeze(quote) not in squeeze(unit_text)


def dropped_qualifiers(claim: str, quote: str) -> list[str]:
    """Qualifiers in the quote whose *governed clause* is missing from the claim.

    Checking for the bare word alone is far too noisy: a claim routinely
    rephrases *"If True, send keepalive pings"* as *"...set to True sends
    keepalive pings"*, which carries the condition without the word `if`. What
    matters is whether the thing being conditioned on survived. So the flag
    requires that the qualifier is absent **and** none of the content words it
    governs made it into the claim. On wool's 813-claim set that took the check
    from 8 flags to 3.
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
    flag fires on every correct split (30 flags on wool's 813-claim set against 7
    with it); with it, only genuinely lost material is reported.
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


def load_claims(args: argparse.Namespace) -> tuple[list[dict], list[dict], list[str],
                                                   dict[str, int]]:
    """Read every input.

    Returns (claims to lint, every claim read, files skipped, claims per file).
    The second list is the first before de-duplication; sibling lookup needs it,
    and `--quotes-only` lints it as it stands.
    """
    out: list[dict] = []
    skipped: list[str] = []
    per_file: dict[str, int] = {}
    for path in args.claims:
        payload = json.loads(path.read_text())
        per_file[path.name] = 0
        # `reconcile_claims.py` writes its rejected and excluded claims beside
        # the survivors, under names a `claims-*.json` glob also matches, and
        # marks them with a `note`. They are not the claim set. Linted with it,
        # they inflate every count, and a rejected claim that happens to carry
        # the other half of a coordination hides a real one-sided split.
        #
        # Not under `--quotes-only`. That mode is a gate over extraction pass
        # files, and a gate that sets a file aside on the strength of a key an
        # agent could have written is a gate with a way round it.
        if "note" in payload and not args.quotes_only:
            skipped.append(path.name)
            continue
        per_file[path.name] = len(payload.get("claims") or [])
        for c in payload.get("claims") or []:
            out.append({"claimId": c.get("id") or c.get("claimId"),
                        "docUnitId": c.get("docUnitId"),
                        "claimText": c.get("claimText", ""),
                        "sourceQuote": c.get("sourceQuote") or "",
                        "source": path.name})
    if args.bundles:
        for path in sorted(args.bundles.glob("ground-*.json")):
            payload = json.loads(path.read_text())
            for unit in payload.get("units", []):
                for c in unit.get("claims", []):
                    out.append({"claimId": c.get("claimId"),
                                "docUnitId": unit.get("docUnitId"),
                                "claimText": c.get("claimText", ""),
                                "sourceQuote": c.get("sourceQuote") or "",
                                "source": path.name})
    # Several inputs may cover the same run (a claims file and its bundles), so
    # a claim with an id is linted once. A pass file's claims have no id yet, and
    # there the file is part of the identity: two passes that word a claim alike
    # each wrote their own quote, and each quote is checked.
    seen: dict[object, dict] = {}
    for c in out:
        key = c["claimId"] or (c["source"], c["docUnitId"], c["claimText"])
        seen.setdefault(key, c)
    return list(seen.values()), out, skipped, per_file


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("claims", nargs="*", type=Path)
    ap.add_argument("--bundles", type=Path, default=None,
                    help="directory of ground-*.json bundles")
    ap.add_argument("--doc-units", type=Path, default=None,
                    help="doc-units.json; check that each sourceQuote appears "
                         "verbatim in its doc unit's text. Exit 1 if any does not")
    ap.add_argument("--quotes-only", action="store_true",
                    help="run the sourceQuote check alone; for extraction pass "
                         "files, where the other two checks do not apply")
    ap.add_argument("--verdicts", type=Path, default=None,
                    help="grounded-*.json; restrict to claims with --verdict")
    ap.add_argument("--verdict", default="contradicted")
    ap.add_argument("--max-flags", type=int, default=None,
                    help="exit 1 when dropped-qualifier and one-sided-split "
                         "flags together exceed this")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.quotes_only and not args.doc_units:
        print("--quotes-only needs --doc-units", file=sys.stderr)
        return 2

    claims, everything, skipped, per_file = load_claims(args)
    for name in skipped:
        print(f"SKIPPED {name}: rejected or excluded claims written by "
              f"reconcile_claims.py, not a claim set", file=sys.stderr)
    if not claims:
        print("no claims found — pass claim files or --bundles", file=sys.stderr)
        return 2

    empty: list[str] = []
    if args.quotes_only:
        # Every claim of every file, with no de-duplication: two claims worded
        # alike in one pass each carry a quote, and each quote reaches the
        # reconciler's merge gate. A file that yields no claims has not been
        # checked, which is a failure of the gate and not a pass.
        claims = everything
        empty = sorted(name for name, n in per_file.items() if n == 0)

    # Sibling lookup is built before any verdict filter: the claim that kept
    # the other half of a coordination is usually `supported`, so restricting
    # the corpus first would resurrect every legitimate split as a false flag.
    # It is built per input file, because a sibling is a claim from the same
    # claim set. A claim from another file about the same doc unit (a rejected
    # claim, another pass's wording) is not one, and counting it suppressed
    # real flags.
    by_unit: dict[tuple[str, str], list[str]] = {}
    for c in everything:
        by_unit.setdefault((c["source"], c.get("docUnitId") or ""), []).append(c["claimText"])

    scope = ""
    if args.verdicts:
        grounded = json.loads(args.verdicts.read_text())
        keep = {c["claimId"] for c in grounded["claims"] if c["verdict"] == args.verdict}
        claims = [c for c in claims if c["claimId"] in keep]
        scope = f" (restricted to verdict `{args.verdict}`)"

    # The quote check runs first and on the raw quote. The two checks after it
    # compare the claim with its quote and so assume the quote is what the
    # docstring said; this is the check of that assumption.
    unit_text: dict[str, str] | None = None
    if args.doc_units:
        unit_text = {u["id"]: u.get("text") or ""
                     for u in json.loads(args.doc_units.read_text())["docUnits"]}

    qual_flags, split_flags, quote_flags = [], [], []
    for c in claims:
        if unit_text is not None:
            unit_id = c.get("docUnitId")
            if not squeeze(c["sourceQuote"]):
                quote_flags.append((c, "the claim has no sourceQuote"))
            elif unit_id not in unit_text:
                quote_flags.append((c, f"doc unit {unit_id} is not in --doc-units"))
            elif quote_missing(c["sourceQuote"], unit_text[unit_id]):
                quote_flags.append((c, "the quote is not in the doc unit's text"))
        if args.quotes_only:
            continue
        claim, quote = clean(c["claimText"]), clean(c["sourceQuote"])
        if not quote:
            continue
        lost = dropped_qualifiers(claim, quote)
        if lost:
            qual_flags.append((c, lost))
        siblings = [s for s in by_unit.get((c["source"], c.get("docUnitId") or ""), [])
                    if s != c["claimText"]]
        split = one_sided_split(claim, quote, siblings)
        if split:
            split_flags.append((c, split))

    print(f"lint_claims: {len(claims)} claims{scope}")
    if args.quotes_only:
        for name, n in per_file.items():
            print(f"  {name}: {n} claims")
    if unit_text is None:
        print("  quote not in unit : not checked (pass --doc-units)")
    else:
        print(f"  quote not in unit : {len(quote_flags)}")
    if args.quotes_only:
        print("  dropped qualifier : not checked (--quotes-only)")
        print("  one-sided split   : not checked (--quotes-only)")
    else:
        print(f"  dropped qualifier : {len(qual_flags)}")
        print(f"  one-sided split   : {len(split_flags)}")

    if not args.quiet:
        for c, why in quote_flags:
            print(f"\n  QUOTE {why}  [{c['source']}]")
            print(f"    claim: {c['claimText']}")
            print(f"    quote: {c['sourceQuote'] or '(none)'}")
        for c, lost in qual_flags:
            print(f"\n  QUALIFIER {lost}  [{c['source']}]")
            print(f"    claim: {c['claimText']}")
            print(f"    quote: {clean(c['sourceQuote'])}")
        for c, why in split_flags:
            print(f"\n  SPLIT {why}  [{c['source']}]")
            print(f"    claim: {c['claimText']}")
            print(f"    quote: {clean(c['sourceQuote'])}")

    failed = False
    # A quote that is not in its doc unit always fails the run. The other two
    # checks are a review queue with a nonzero baseline; this one is a promise
    # made to the extraction agents, with a baseline of zero.
    if quote_flags:
        print(f"\nFAIL: {len(quote_flags)} sourceQuote(s) not in their doc unit")
        failed = True
    if empty:
        print(f"\nFAIL: no claims read from {', '.join(empty)}; "
              f"its quotes were not checked")
        failed = True
    review = len(qual_flags) + len(split_flags)
    if args.max_flags is not None and review > args.max_flags:
        print(f"\nFAIL: {review} review flags exceeds --max-flags {args.max_flags}")
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
