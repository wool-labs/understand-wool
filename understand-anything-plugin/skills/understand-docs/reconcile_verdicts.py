#!/usr/bin/env python3
"""reconcile_verdicts.py — verify and reconcile Phase E grounding passes.

Deterministic, stdlib only, no LLM.

Two jobs, in order. The verification comes first because a consensus computed
over fabricated evidence is worse than no consensus at all — it launders a guess
into a number.

**1. Verify every citation against the source.** Each evidence item names a file,
a line range, and a verbatim quote. All three are checkable, and this script
checks them:

    - the file exists in the mirror
    - the cited range is within the file
    - every non-blank line of the quote appears inside the cited range
    - the quote is not a `<docstring removed>` marker or a bare `...` stub

An item failing any of these is `unverified`. A verdict of `supported` or
`contradicted` whose evidence is entirely unverified is **downgraded to
`unverifiable`** before consensus, and counted. This is the only defense against
an agent that reasons well and cites carelessly, and on a first run it is
normally non-zero.

**2. Reconcile.** Per claim, take the majority verdict across passes. Report:

    unanimous   all passes agree — the trustworthy set
    majority    n-1 of n agree — usable, flagged
    split       no majority — needs a human, and is the interesting pile

Agreement here is a *different and stronger* measurement than extraction
agreement. Extraction asks "did agents write the same sentence?"; grounding asks
"did agents reach the same conclusion from the same code?" Two passes can phrase
a verdict differently and still agree, so this number is not inflated by wording.

Usage:
    python3 reconcile_verdicts.py VERDICTS.json [VERDICTS.json ...] \\
        --bundles DIR --mirror DIR [--out DIR] [--label NAME]

Output:
    <out>/grounded-<label>.json          per-claim consensus verdicts
    <out>/grounding-report-<label>.md    human-readable triage report
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "0.1.0"

# Ordered as a severity ladder; this drives the report table and the stdout line,
# so the order is load-bearing rather than cosmetic.
#
# There were briefly four. `overstated` — the claim is true but promises more
# than the code delivers — was built, shipped, and then measured against a
# control arm: re-grounding the same 71 claims with fresh agents reproduced
# `contradicted` 14/14 and `overstated` only 17/31. Rewording the prompt did not
# help (52% on the same set), because the instability is the category: a verdict
# whose whole content is "there exists an excluded case, and here it is" depends
# on a second grader *finding* that case, and eleven of the fourteen misses were
# graders who simply did not. The bound it cited is a different matter — those
# citations verify at 100% — so the pointer is kept as a scope note and only the
# label was withdrawn. Do not re-propose it without a reproduction measurement.
VERDICTS = ("supported", "contradicted", "unverifiable")

# Verdicts that assert something about the code and therefore must cite it.
EVIDENCE_REQUIRED = ("supported", "contradicted")

# Evidence roles that establish where a documented behaviour *stops*. These are
# what a scope note is built from: the claim holds, and here is the line where
# it stops holding.
BOUND_ROLES = ("limits", "contradicts")

# Verdicts accepted on input but resolved to something in VERDICTS. The frozen
# grounding archive is full of `overstated` votes; folding rather than rejecting
# them keeps it replayable, which is the only way to measure what the fold did.
LEGACY_VERDICTS = ("overstated",)


def scope_note(vote: dict, verified_bounds: list[dict],
               verdict: str | None = None) -> dict | None:
    """The finding an `overstated` vote carried, without the verdict.

    `asSupported` is the narrowed form the code actually supports; the bound is
    the verified citation showing where the documented behaviour stops. Together
    they answer "the docstring says X — where does that stop being true?", which
    is the part a consumer can act on. `asWritten` is dropped because it merely
    restates `claimText`, and `readerImpact` with the harm test that required it.

    A note belongs only on a claim that HOLDS. `BOUND_ROLES` includes
    `contradicts`, so without the verdict guard a `contradicted` vote carrying an
    `asSupported` payload acquires a note, inflates `scopeNotes`, and renders
    under a report heading that reads "claims that hold" — the opposite of what
    it found. `unverifiable` is excluded for the same reason: nothing was
    established, so nothing can be bounded.
    """
    if verdict is not None and verdict != "supported":
        return None
    payload = vote.get("scopeNote") or vote.get("overstatement") or {}
    if not isinstance(payload, dict) or not verified_bounds:
        return None
    narrowed = (payload.get("asSupported") or "").strip()
    if not narrowed:
        return None
    return {"asSupported": narrowed,
            "bound": [{"file": b.get("file"), "lines": b.get("lines"),
                       "role": b.get("role")} for b in verified_bounds]}


# Tie-break order for an unresolved split, most-surfaced first.
#
# With two passes every disagreement is a 1-1 tie, so *something* has to break
# it. The rule is: surface the finding rather than bury it. Displaying
# `supported` for a claim one pass called `unverifiable` asserts a verification
# that is actually disputed, which is the more damaging error — and the row is
# headed for adjudication regardless, where the displayed value is replaced.
TIE_PRIORITY = ("contradicted", "unverifiable", "supported")

STUB_MARKERS = ("<docstring removed>",)


def norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def verify_evidence(item: dict, mirror: Path) -> tuple[bool, str]:
    """Return (ok, reason). Checks the citation actually exists as claimed."""
    raw_file = (item.get("file") or "").strip()
    if not raw_file:
        return False, "no file"
    rel = raw_file.split("src-stripped/")[-1].lstrip("/")
    path = mirror / rel
    if not path.exists():
        return False, f"no such file: {rel}"

    lines = path.read_text(encoding="utf-8").splitlines()
    span = item.get("lines")
    # A single-element `[473]` is a one-line citation, not a malformed one. Two
    # of eight passes in the boundary run emitted that shape and had 45
    # otherwise-valid citations discarded, which reads in the stats exactly like
    # fabricated evidence. Normalise the unambiguous case rather than measure
    # the agents' JSON habits.
    if isinstance(span, list) and len(span) == 1 and isinstance(span[0], int):
        span = [span[0], span[0]]
    if not (isinstance(span, list) and len(span) == 2
            and all(isinstance(x, int) for x in span)):
        return False, "malformed line range"
    start, end = span
    if start < 1 or end > len(lines) or start > end:
        return False, f"range {start}-{end} outside file (1-{len(lines)})"

    code = item.get("code") or ""
    if any(m in code for m in STUB_MARKERS):
        return False, "quotes a stripped-docstring marker"
    quoted = [norm(l) for l in code.splitlines() if norm(l)]
    if not quoted:
        return False, "empty quote"
    if all(q in {"...", "pass"} for q in quoted):
        return False, "quotes only a stub body"

    # Allow a small slop: agents commonly cite the decorator or `def` line one or
    # two above the statement they mean. Wider than that is a real miss.
    lo, hi = max(0, start - 3), min(len(lines), end + 3)
    window = {norm(l) for l in lines[lo:hi] if norm(l)}
    missing = [q for q in quoted if q not in window]
    if missing:
        return False, f"{len(missing)}/{len(quoted)} quoted lines not at {start}-{end}"
    return True, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("verdicts", nargs="+", type=Path)
    ap.add_argument("--bundles", type=Path, required=True)
    ap.add_argument("--mirror", type=Path, required=True)
    ap.add_argument("--claims", type=Path, default=None,
                    help="reconciled claims file, for claim text in the report")
    ap.add_argument("--out", type=Path, default=Path("."))
    ap.add_argument("--label", default="claims")
    ap.add_argument("--tiebreak", type=Path, default=None,
                    help="adjudicated verdicts that override consensus on disputed claims")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    # Expected claims, from the bundles — the authority on what must be covered.
    expected: dict[str, dict] = {}
    for path in sorted(args.bundles.glob("ground-*.json")):
        bundle = json.loads(path.read_text())
        for unit in bundle["units"]:
            for claim in unit["claims"]:
                expected[claim["claimId"]] = {
                    **claim,
                    "sourceFile": unit["sourceFile"],
                    "qualname": unit["qualname"],
                    "groundingTarget": unit["groundingTarget"],
                    "docUnitId": unit["docUnitId"],
                }

    # Which bundle each claim belongs to. Needed because a claim is only ever
    # voted on by the passes over *its own* bundle: comparing vote count against
    # the global pass total marks every claim "majority" and silently destroys
    # the unanimity statistic.
    claim_bundle: dict[str, str] = {}
    for path in sorted(args.bundles.glob("ground-*.json")):
        bundle = json.loads(path.read_text())
        for unit in bundle["units"]:
            for claim in unit["claims"]:
                claim_bundle[claim["claimId"]] = path.stem

    per_claim: dict[str, list[dict]] = collections.defaultdict(list)
    ev_stats = collections.Counter()
    ev_failures: list[dict] = []
    downgrades: list[dict] = []
    folded: list[dict] = []
    pass_ids: set[str] = set()
    coverage: dict[str, set[str]] = collections.defaultdict(set)

    for path in sorted(args.verdicts):
        payload = json.loads(path.read_text())
        pid = path.stem
        pass_ids.add(pid)
        for v in payload.get("verdicts", []):
            cid = v.get("claimId")
            if cid not in expected:
                ev_stats["verdict for unknown claimId"] += 1
                continue
            coverage[pid].add(cid)

            verified, unverified = [], []
            for item in v.get("evidence") or []:
                ok, reason = verify_evidence(item, args.mirror)
                ev_stats["evidence ok" if ok else "evidence BAD"] += 1
                if ok:
                    verified.append(item)
                else:
                    unverified.append({**item, "failure": reason})
                    ev_failures.append({"pass": pid, "claimId": cid,
                                        "file": item.get("file"),
                                        "lines": item.get("lines"),
                                        "failure": reason})

            verified_bounds = [
                item for item in verified
                if (item.get("role") or "").strip().lower() in BOUND_ROLES
            ]

            verdict = v.get("verdict")
            original = verdict

            # A legacy `overstated` vote resolves to `supported` and keeps its
            # bound as a scope note. The claim is true — that was never the
            # dispute — so `supported` is the honest label, and the citation
            # showing where it stops is the part worth keeping.
            #
            # It resolves like any other evidence-bearing verdict when nothing
            # verifies: `unverifiable`, recorded. Gating the whole branch on
            # `verified` left that case matching neither arm, since `overstated`
            # is no longer in EVIDENCE_REQUIRED — it fell through to the
            # bare-coercion line below and was counted nowhere, which is exactly
            # where `foldedOverstated` is meant to be loudest.
            #
            # `folded` counts every legacy verdict SEEN, whatever it became, so
            # it stays a true stale-prompt detector. A vote can therefore appear
            # in both ledgers: the label was legacy *and* its evidence failed.
            if verdict in LEGACY_VERDICTS:
                if verified:
                    verdict = "supported"
                else:
                    verdict = "unverifiable"
                    downgrades.append({"pass": pid, "claimId": cid, "from": original,
                                       "reason": "no verifiable evidence"})
                folded.append({"pass": pid, "claimId": cid,
                               "from": original, "to": verdict})
            elif verdict in EVIDENCE_REQUIRED and not verified:
                verdict = "unverifiable"
                downgrades.append({"pass": pid, "claimId": cid, "from": original,
                                   "reason": "no verifiable evidence"})
            if verdict not in VERDICTS:
                verdict = "unverifiable"

            # Built after the verdict resolves, so the guard sees the final
            # label rather than the one the agent wrote.
            note = scope_note(v, verified_bounds, verdict)

            per_claim[cid].append({
                "pass": pid,
                "verdict": verdict,
                "originalVerdict": original,
                "confidence": v.get("confidence"),
                "reasoning": v.get("reasoning"),
                "evidence": verified,
                "verifiedBounds": verified_bounds,
                "unverifiedEvidence": unverified,
                "scopeChecked": v.get("scopeChecked"),
                "searchedFor": v.get("searchedFor"),
                # Where the claim stops holding, when the grader located it.
                # Machine-collectable, and the deliverable a bare `supported`
                # would otherwise discard.
                "scopeNote": note,
            })

    # How many passes covered each bundle — the denominator for unanimity.
    passes_per_bundle: collections.Counter = collections.Counter()
    for pid, cids in coverage.items():
        bundles_seen = {claim_bundle[c] for c in cids if c in claim_bundle}
        for b in bundles_seen:
            passes_per_bundle[b] += 1

    # Consensus
    results: list[dict[str, Any]] = []
    agree = collections.Counter()
    final = collections.Counter()
    n_passes = len(pass_ids)

    for cid, meta in sorted(expected.items()):
        votes = per_claim.get(cid, [])
        tally = collections.Counter(v["verdict"] for v in votes)
        # Expected votes come from this claim's own bundle, not the global total.
        expected_votes = passes_per_bundle.get(claim_bundle.get(cid), 0)
        if not votes:
            level, verdict = "missing", None
        else:
            # Counter.most_common breaks ties by insertion order — deterministic
            # here only by accident of sorted file iteration. Order explicitly so
            # a rerun cannot silently flip a split row. See TIE_PRIORITY.
            top, n = max(tally.items(),
                         key=lambda kv: (kv[1], -TIE_PRIORITY.index(kv[0])))
            if n == len(votes) and len(votes) >= expected_votes and expected_votes > 1:
                level, verdict = "unanimous", top
            elif n > len(votes) / 2:
                level, verdict = "majority", top
            else:
                level, verdict = "split", top
        agree[level] += 1
        if verdict:
            final[verdict] += 1

        # Prefer the vote that actually wrote the scope note, then bound count,
        # then evidence count. Note-presence must outrank bound-count: a vote
        # citing two bounds without a narrowing would otherwise beat the one
        # vote that cited a single bound *and* said what the claim narrows to,
        # discarding the only finding this taxonomy still records. Keying on
        # evidence count alone is worse again — five supporting citations would
        # outrank the vote that located where the claim stops.
        best = max((v for v in votes if v["verdict"] == verdict),
                   key=lambda v: (bool(v.get("scopeNote")),
                                  len(v.get("verifiedBounds") or []),
                                  len(v["evidence"])),
                   default=None)
        results.append({
            "claimId": cid,
            "claimText": meta["claimText"],
            "claimType": meta["claimType"],
            "quantifier": meta["quantifier"],
            "extractionPasses": meta["passCount"],
            "docUnitId": meta["docUnitId"],
            "sourceFile": meta["sourceFile"],
            "qualname": meta["qualname"],
            "groundingTarget": meta["groundingTarget"],
            "verdict": verdict,
            "agreement": level,
            "voteCounts": dict(tally),
            "evidence": best["evidence"] if best else [],
            "reasoning": best["reasoning"] if best else None,
            "scopeNote": best.get("scopeNote") if best else None,
            "votes": votes,
        })

    # Adjudicated verdicts override consensus. They pass through exactly the
    # same verification and downgrade ladder as any other vote — an adjudicator
    # citing bad evidence is no more trustworthy than a first-pass grader doing
    # the same.
    adjudicated = 0
    if args.tiebreak and args.tiebreak.exists():
        tb = {v["claimId"]: v for v in json.loads(args.tiebreak.read_text()).get("verdicts", [])}
        by_id = {r["claimId"]: r for r in results}
        for cid, v in tb.items():
            record = by_id.get(cid)
            if record is None:
                continue
            verified, bounds = [], []
            for item in v.get("evidence") or []:
                ok, reason = verify_evidence(item, args.mirror)
                ev_stats["evidence ok" if ok else "evidence BAD"] += 1
                if ok:
                    verified.append(item)
                    if (item.get("role") or "").strip().lower() in BOUND_ROLES:
                        bounds.append(item)
                else:
                    ev_failures.append({"pass": "tiebreak", "claimId": cid,
                                        "file": item.get("file"), "lines": item.get("lines"),
                                        "failure": reason})
            verdict = v.get("verdict")
            original = verdict
            # Same ladder as the pass path, deliberately. Gating the fold on
            # `verified` here was worse than in the pass path: the row fell to
            # `continue` and the entire adjudication was discarded — no verdict
            # change, no `adjudicated` increment, nothing printed. An
            # adjudicator's answer must never vanish silently, whatever it said.
            if verdict in LEGACY_VERDICTS:
                if verified:
                    verdict = "supported"
                else:
                    verdict = "unverifiable"
                    downgrades.append({"pass": "tiebreak", "claimId": cid,
                                       "from": original,
                                       "reason": "no verifiable evidence"})
                folded.append({"pass": "tiebreak", "claimId": cid,
                               "from": original, "to": verdict})
            elif verdict in EVIDENCE_REQUIRED and not verified:
                downgrades.append({"pass": "tiebreak", "claimId": cid,
                                   "from": original, "reason": "no verifiable evidence"})
                verdict = "unverifiable"
            # Anything still outside the taxonomy is coerced, not dropped. The
            # `continue` that used to live here was the last way an
            # adjudicator's answer could vanish: no verdict change, no
            # `adjudicated` increment, nothing printed — and the row's evidence
            # already counted into `ev_stats` above, so the ledgers disagreed
            # about whether it had been processed at all. A typo, a cased
            # variant, or a label from a future prompt now lands in
            # `downgrades` where a reader can see it.
            if verdict not in VERDICTS:
                downgrades.append({"pass": "tiebreak", "claimId": cid,
                                   "from": original,
                                   "reason": "unrecognised verdict"})
                verdict = "unverifiable"
            note = scope_note(v, bounds, verdict)
            if record["verdict"]:
                final[record["verdict"]] -= 1
            agree[record["agreement"]] -= 1
            record["verdict"] = verdict
            record["agreement"] = "adjudicated"
            record["reasoning"] = v.get("reasoning") or record["reasoning"]
            # Fall back to the consensus note only while the claim still holds.
            # `scope_note` returns None for a non-`supported` verdict by design;
            # `note or record.get(...)` then resurrected the supported-era note
            # onto an overturned claim, so a `contradicted` row rendered under
            # "claims that hold" and inflated `scopeNotes` — exactly the failure
            # the verdict guard was added to prevent.
            record["scopeNote"] = (note or record.get("scopeNote")
                                   if verdict == "supported" else None)
            if verified:
                record["evidence"] = verified
            final[verdict] += 1
            agree["adjudicated"] += 1
            adjudicated += 1

    total = len(expected)
    stats = {
        "claims": total,
        "passes": n_passes,
        "coveragePerPass": {p: len(c) for p, c in sorted(coverage.items())},
        "agreement": dict(agree),
        "agreementRate": {k: round(v / total, 4) for k, v in agree.items()} if total else {},
        "verdicts": dict(final),
        "verdictRate": {k: round(v / total, 4) for k, v in final.items()} if total else {},
        "evidenceChecks": dict(ev_stats),
        "evidenceFailureRate": round(
            ev_stats["evidence BAD"] / max(1, ev_stats["evidence ok"] + ev_stats["evidence BAD"]), 4),
        "downgraded": len(downgrades),
        # Legacy `overstated` votes resolved to `supported`. Non-zero only when
        # replaying pass files written under the four-verdict taxonomy; on a
        # current run it should be 0, and a rising number means an agent is
        # working from a stale prompt.
        "foldedOverstated": len(folded),
        # How many claims carry a located bound. This is the retrievable output
        # the fourth verdict used to represent, now measured directly.
        "scopeNotes": sum(1 for r in results if r.get("scopeNote")),
        "adjudicated": adjudicated,
        # Scope notes cluster on universal claims; this is how you tell a real
        # boundary finding from a hedge applied to every quantified sentence.
        "verdictByQuantifier": {
            f"{q}/{v}": n for (q, v), n in sorted(collections.Counter(
                (r["quantifier"], r["verdict"]) for r in results if r["verdict"]
            ).items())
        },
    }

    (args.out / f"grounded-{args.label}.json").write_text(json.dumps({
        "schemaVersion": SCHEMA_VERSION, "stats": stats,
        "evidenceFailures": ev_failures, "downgrades": downgrades,
        "foldedOverstated": folded,
        "claims": results,
    }, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    # ---- report ----
    def section(title: str, rows: list[dict], limit: int = 100) -> str:
        if not rows:
            return f"### {title}\n\nNone.\n"
        out = [f"### {title} ({len(rows)})\n"]
        for r in rows[:limit]:
            out.append(f"**{r['claimText']}**  ")
            out.append(f"`{r['qualname']}` · {r['sourceFile']} · "
                       f"{r['quantifier']} · agreement: {r['agreement']} "
                       f"({r['voteCounts']})  ")
            if r.get("reasoning"):
                out.append(f"{r['reasoning']}  ")
            if r.get("scopeNote"):
                out.append(f"**holds as far as: {r['scopeNote']['asSupported']}**  ")
            # Bound-role citations first: where a scope note exists the limiting
            # citation is the whole point and may otherwise fall outside [:2].
            ev = sorted(
                r.get("evidence") or [],
                key=lambda e: (e.get("role") or "").strip().lower() not in BOUND_ROLES,
            )
            for e in ev[:2]:
                loc = f"{e.get('file')}:{e.get('lines')}"
                snippet = (e.get("code") or "").strip().splitlines()
                body = "\n".join(snippet[:6])
                out.append(f"\n```python\n# {loc}\n{body}\n```\n")
            out.append("")
        if len(rows) > limit:
            out.append(f"_…and {len(rows) - limit} more._\n")
        return "\n".join(out)

    contradicted = [r for r in results if r["verdict"] == "contradicted"]
    scoped = [r for r in results if r.get("scopeNote")]
    split = [r for r in results if r["agreement"] == "split"]
    supported = [r for r in results if r["verdict"] == "supported"]
    unver = [r for r in results if r["verdict"] == "unverifiable"]

    report = [
        f"# Grounding report — {args.label}\n",
        f"{total} claims · {n_passes} independent passes · "
        f"{stats['evidenceChecks'].get('evidence ok', 0)} evidence citations verified, "
        f"{stats['evidenceChecks'].get('evidence BAD', 0)} rejected "
        f"({stats['evidenceFailureRate']:.1%} failure rate)\n",
        "## Verdicts\n",
        "| verdict | n | share |",
        "|---|---:|---:|",
    ]
    for k in VERDICTS:
        n = final.get(k, 0)
        report.append(f"| {k} | {n} | {n / total:.1%} |" if total else f"| {k} | {n} | — |")
    report += ["", "## Verdict agreement across passes\n",
               "| level | n | share |", "|---|---:|---:|"]
    for k in ("unanimous", "majority", "split", "missing"):
        n = agree.get(k, 0)
        report.append(f"| {k} | {n} | {n / total:.1%} |" if total else f"| {k} | {n} | — |")
    report += ["",
               f"_{len(downgrades)} verdicts were downgraded to `unverifiable` "
               f"because no cited evidence survived verification._\n",
               f"_{len(folded)} legacy `overstated` votes were folded into "
               f"`supported`; the located bound survives as a scope note._\n",
               "## Contradicted — documentation that disagrees with the code\n",
               section("Contradicted", contradicted),
               "## Scope notes — claims that hold, with a located boundary\n",
               section("Scoped", scoped),
               "## Split — no majority verdict, needs a human\n",
               section("Split verdicts", split, limit=40),
               ]
    (args.out / f"grounding-report-{args.label}.md").write_text("\n".join(report) + "\n")

    print(f"{args.label}: {total} claims, {n_passes} passes")
    print(f"  evidence: {ev_stats['evidence ok']} verified, {ev_stats['evidence BAD']} rejected "
          f"({stats['evidenceFailureRate']:.1%} bad), {len(downgrades)} verdicts downgraded")
    print("  verdicts:  " + "  ".join(f"{k} {final.get(k, 0)}" for k in VERDICTS))
    if folded:
        print(f"  folded {len(folded)} legacy `overstated` votes into `supported`")
    print(f"  scope notes: {stats['scopeNotes']} claims carry a located bound")
    print("  agreement: " + "  ".join(
        f"{k} {agree.get(k, 0)} ({agree.get(k, 0) / total:.0%})" for k in
        ("unanimous", "majority", "split", "missing") if agree.get(k)))
    print(f"→ {args.out}/grounded-{args.label}.json")
    print(f"→ {args.out}/grounding-report-{args.label}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
