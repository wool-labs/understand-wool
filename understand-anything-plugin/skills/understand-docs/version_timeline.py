#!/usr/bin/env python3
"""version_timeline.py — date each doc unit and resolve it to a release.

Step 2b of the documentation claim pipeline (optional). Deterministic, stdlib +
git only, no LLM.

Measures one thing: **are doc units spread over enough release series that
recency could tell which of two contradicting doc units is newer?** It was
written for a design in which the answer would let the pipeline emit a directed
`supersedes` edge in place of a symmetric `contradicts`. That design was not
built: no step reads this script's output, and the emitter has no `supersedes`
edge type. What remains is the measurement, reported to the user.

Method, per doc unit:

    git blame -L <docLineRange> <file>   →  a commit per line
    take the EARLIEST author date        →  when the docstring first appeared
    bisect against date-sorted tags      →  the release it landed in

The earliest line is used, not the latest, because `git blame` reports when a line
was last *touched*, not when the claim was *written*. A reformat, lint pass, or
rename resets the latest and would invert the orientation; the earliest survives
edits to any single line of a multi-line docstring. It is still defeated by a
wholesale rewrite, which is why the unknown/recent rate is reported rather than
assumed away.

**The signal is `--report`.** It prints one verdict. If most resolved doc units
cluster into one release series the verdict is `not viable`. If most doc units
resolve to no series at all, or the clone is shallow, the verdict is
`undetermined`, with a reason that separates a measurement that failed (a
shallow clone, blame returning nothing) from a fact about the repository (work
since the last tag, tags that are not versions). The verdict and its reason are
also written to the output file as `stats.verdict` and `stats.verdictReason`.

The verdict is a measurement and nothing more. No later step of the pipeline
reads it, and `supersedes` is not an edge type the emitter writes.

Usage:
    python3 version_timeline.py <repo-root> --doc-units <doc-units.json>
        [--out DIR] [--report]

Output:
    <out>/version-timeline.json
"""

from __future__ import annotations

import argparse
import bisect
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "0.1.0"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def resolve_ua_dir(root: Path) -> Path:
    legacy = root / ".understand-anything"
    return legacy if legacy.is_dir() else root / ".ua"


# ---------------------------------------------------------------------------
# Release timeline
# ---------------------------------------------------------------------------


def load_tags(root: Path) -> list[tuple[int, str]]:
    """Version tags as (committer_timestamp, name), oldest first.

    Sorted by date rather than parsed semver: date order is what bisection needs,
    and it sidesteps pre-release ordering entirely (`v0.12.0-rc10` vs `-rc9` sorts
    correctly by date and incorrectly by string).

    Only tags that name a version are kept. A `nightly-2021` tag created between
    two releases would otherwise be the "release" of everything written before
    it, and those doc units would resolve to no series though they shipped in
    the next version.
    """
    raw = git(root, "for-each-ref", "--sort=creatordate",
              "--format=%(creatordate:unix)\t%(refname:short)", "refs/tags")
    tags: list[tuple[int, str]] = []
    for line in raw.strip().split("\n"):
        if not line:
            continue
        ts, name = line.split("\t", 1)
        if RELEASE_RE.match(name):
            tags.append((int(ts), name))
    return tags


def release_for(timestamp: int, tags: list[tuple[int, str]]) -> Optional[str]:
    """First tag created at or after this timestamp — the release it landed in."""
    idx = bisect.bisect_left(tags, (timestamp, ""))
    return tags[idx][1] if idx < len(tags) else None


RELEASE_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def minor_series(tag: Optional[str]) -> Optional[str]:
    """`v0.12.0-rc10` → `0.12`. The comparison unit for supersession."""
    if not tag:
        return None
    m = RELEASE_RE.match(tag)
    return f"{m.group(1)}.{m.group(2)}" if m else None


# ---------------------------------------------------------------------------
# Blame
# ---------------------------------------------------------------------------


def blame_file(root: Path, rel: str) -> dict[int, tuple[str, int]]:
    """line number → (sha, author_timestamp), for the whole file.

    One blame per file rather than one per doc unit: the same result for a
    tenth of the subprocess calls on wool, which has 473 doc units in 40 files.
    """
    try:
        raw = git(root, "blame", "--line-porcelain", "--", rel)
    except subprocess.CalledProcessError:
        return {}

    # Blame attributes a line that is not in any commit to an all-zero sha and
    # dates it to the moment of the run. Such a line has no history to date, so
    # it is left out, and a doc unit made only of such lines counts as unblamed.
    uncommitted = "0" * 40
    out: dict[int, tuple[str, int]] = {}
    sha: Optional[str] = None
    lineno: Optional[int] = None
    author_time: Optional[int] = None
    commit_times: dict[str, int] = {}

    for line in raw.split("\n"):
        header = re.match(r"^([0-9a-f]{40}) \d+ (\d+)", line)
        if header:
            sha, lineno = header.group(1), int(header.group(2))
            author_time = commit_times.get(sha)
            continue
        if line.startswith("author-time ") and sha:
            author_time = int(line.split(" ", 1)[1])
            commit_times[sha] = author_time
            continue
        if line.startswith("\t") and sha and lineno is not None and author_time is not None:
            if sha != uncommitted:
                out[lineno] = (sha, author_time)
            sha = lineno = author_time = None
    return out


# ---------------------------------------------------------------------------
# Viability
# ---------------------------------------------------------------------------

# Above this share of resolved doc units in one series, blame has been flattened.
FLATTENED_SHARE = 0.9
# Below this many distinct series there is too little spread to orient much.
MIN_SERIES = 3
# Above this share of doc units with no series, the resolved ones are a minority
# and their distribution says nothing about the corpus.
UNRESOLVED_SHARE = 0.5

VERDICT_TEXT = {
    "viable": "the resolved doc units are spread over enough series that "
              "recency could orient a contradiction.",
    "marginal": "the resolved doc units fall in fewer than "
                f"{MIN_SERIES} series, so recency could orient few pairs.",
    "not viable": f"one series holds more than {FLATTENED_SHARE:.0%} of the "
                  "resolved doc units. Either a bulk reformat flattened blame "
                  "or the project has had one minor series; recency cannot "
                  "orient contradictions here.",
    "undetermined": "the timeline could not be measured.",
}

# Why a verdict is `undetermined`. The causes call for different responses, so
# the report names one rather than leaving the operator to guess. None of the
# responses changes the repository: the operator reports, or re-runs with
# corrected arguments.
REASON_TEXT = {
    "shallow clone": "this clone is shallow, so blame and tags are truncated "
                     "and any distribution would be an artifact. Report that "
                     "step 2b was not measured. Do not unshallow the clone "
                     "without asking the user.",
    "no doc units": "the doc-units file lists no doc units. If step 1 has not "
                    "run, run it; if it ran and found none, report that.",
    "no version tags": "this clone has no tag of the form 1.2.3 or v1.2.3, so "
                       "no doc unit can be placed in a release. If "
                       "`git ls-remote --tags origin` lists version tags, the "
                       "clone is missing them: ask the user before fetching. "
                       "Otherwise report that the project has no version tags.",
    "blame failed": "git blame returned no committed line for most of the "
                    "unresolved doc units. Either <repo-root> is not the "
                    "directory step 1 harvested from, or the doc units' files "
                    "have changes that are not committed. If the root was "
                    "wrong, run once more with the right one. Otherwise report "
                    "it. Do not stage, commit, or stash anything.",
    "unreleased": "most doc units were written after the last version tag. "
                  "This describes the repository; running again will not "
                  "change it. Report it.",
}

# Printed under every verdict. The verdict is a measurement for the user, and
# nothing downstream consumes it.
STANDING_RULE = ("No pipeline step reads this verdict, and step 13 is "
                 "unaffected by it. Report the verdict to the user and "
                 "continue the pipeline.")


def unresolved_cause(entry: dict[str, Any]) -> Optional[str]:
    """Why an entry has no series, or None when it has one."""
    if entry.get("minorSeries"):
        return None
    return "afterLastTag" if entry.get("sha") else "noBlame"


def viability(entries: list[dict[str, Any]], shallow: bool = False,
              tagged: bool = True) -> dict[str, Any]:
    """Whether doc units spread across enough release series to orient by recency.

    Every figure is computed over the doc units that resolved to a series. A
    unit resolves to none when blame found no committed line for it, or when it
    postdates the last version tag. Those units are counted as `unresolved`,
    broken down by cause in `unresolvedBy`, and never compete for
    `largestSeries`: counted as a series, they could win it, and a repository
    where blame had simply failed was then reported as "flattened into one
    series", which names the opposite cause and calls for the opposite remedy.

    `shallow` says the clone is shallow and `tagged` that it has at least one
    version tag; either condition alone decides the verdict.

    The verdict is one of the keys of `VERDICT_TEXT`. `not viable` is tested
    before `marginal`, so a corpus in one or two series with one of them above
    the flattened share is `not viable`. When the verdict is `undetermined`,
    `verdictReason` is one of the keys of `REASON_TEXT`; otherwise it is None.
    The reason is `blame failed` when blame failed for more than half of the
    unresolved doc units, and `unreleased` otherwise.
    """
    known = Counter(e["minorSeries"] for e in entries if e["minorSeries"])
    causes = Counter(c for c in map(unresolved_cause, entries) if c)
    total = len(entries)
    resolved = sum(known.values())
    unresolved = total - resolved
    # Ties break on the series name so the result does not depend on entry order.
    largest, largest_count = min(known.items(), key=lambda kv: (-kv[1], kv[0]),
                                 default=(None, 0))
    share = largest_count / resolved if resolved else None
    unresolved_share = unresolved / total if total else 0.0

    reason = None
    if shallow:
        verdict, reason = "undetermined", "shallow clone"
    elif not total:
        verdict, reason = "undetermined", "no doc units"
    elif not tagged:
        verdict, reason = "undetermined", "no version tags"
    elif not resolved or unresolved_share > UNRESOLVED_SHARE:
        verdict = "undetermined"
        reason = ("blame failed" if causes["noBlame"] * 2 > unresolved
                  else "unreleased")
    elif share > FLATTENED_SHARE:
        verdict = "not viable"
    elif len(known) < MIN_SERIES:
        verdict = "marginal"
    else:
        verdict = "viable"

    return {
        "distinctMinorSeries": len(known),
        "largestSeries": largest,
        "largestSeriesShare": None if share is None else round(share, 4),
        "outsideLargestSeries": resolved - largest_count,
        "unresolved": unresolved,
        "unresolvedShare": round(unresolved_share, 4),
        "unresolvedBy": {key: causes[key] for key in ("noBlame", "afterLastTag")},
        "verdict": verdict,
        "verdictReason": reason,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--doc-units", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--report", action="store_true", help="print the viability report")
    args = ap.parse_args()

    root = args.root.resolve()

    # Both arguments are checked before anything is written. Each failure used
    # to end in a traceback with no `VERDICT:` line, and a root that did not
    # exist was created by the `mkdir` below before git was asked about it.
    try:
        doc_units = json.loads(args.doc_units.read_text())["docUnits"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"BAD DOC UNITS — {args.doc_units} is not a doc-units file step 1 "
              f"wrote ({type(exc).__name__}: {exc}). Pass the doc-units file "
              f"and run again.", file=sys.stderr)
        return 1
    try:
        git(root, "rev-parse", "--git-dir")
    except (OSError, subprocess.CalledProcessError):
        print(f"NOT A REPOSITORY — {root} is not inside a git repository. Pass "
              f"the directory step 1 was given and run again.", file=sys.stderr)
        return 1

    out_dir = args.out or (resolve_ua_dir(root) / "docs")
    out_dir.mkdir(parents=True, exist_ok=True)
    tags = load_tags(root)
    if not tags:
        print("no version tags — cannot resolve releases", file=sys.stderr)

    blame_cache: dict[str, dict[int, tuple[str, int]]] = {}
    entries: list[dict[str, Any]] = []
    unknown = 0

    for unit in doc_units:
        rel = unit["sourceFile"]
        span = unit.get("docLineRange")
        if rel not in blame_cache:
            blame_cache[rel] = blame_file(root, rel)
        lines = blame_cache[rel]

        candidates = []
        if span:
            for ln in range(span[0], span[1] + 1):
                if ln in lines:
                    candidates.append(lines[ln])

        if not candidates:
            unknown += 1
            entries.append({
                "docUnitId": unit["id"], "sourceFile": rel,
                "docLineRange": span, "sha": None, "authoredAt": None,
                "release": None, "minorSeries": None,
            })
            continue

        sha, ts = min(candidates, key=lambda c: c[1])
        release = release_for(ts, tags)
        entries.append({
            "docUnitId": unit["id"], "sourceFile": rel,
            "docLineRange": span, "sha": sha,
            "authoredAt": ts, "release": release,
            "minorSeries": minor_series(release),
        })

    entries.sort(key=lambda e: (e["sourceFile"], e["docLineRange"] or [0], e["docUnitId"]))

    # One word per meaning. `unknown` is a doc unit blame returned nothing for;
    # `(none)` is the bucket of doc units with no release and so no series,
    # which also holds units that were blamed and postdate the last tag.
    by_series = Counter(e["minorSeries"] or "(none)" for e in entries)
    by_release = Counter(e["release"] or "(none)" for e in entries)
    total = len(entries)
    shallow = git(root, "rev-parse", "--is-shallow-repository").strip() == "true"
    v = viability(entries, shallow=shallow, tagged=bool(tags))
    distinct = v["distinctMinorSeries"]

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "root": str(root),
        "tagCount": len(tags),
        "stats": {
            "docUnits": total,
            "unknown": unknown,
            "distinctMinorSeries": distinct,
            "largestSeries": v["largestSeries"],
            "largestSeriesShare": v["largestSeriesShare"],
            "outsideLargestSeries": v["outsideLargestSeries"],
            "unresolved": v["unresolved"],
            "unresolvedShare": v["unresolvedShare"],
            "unresolvedBy": v["unresolvedBy"],
            "shallowClone": shallow,
            "verdict": v["verdict"],
            "verdictReason": v["verdictReason"],
            "byMinorSeries": dict(sorted(by_series.items())),
            "byRelease": dict(sorted(by_release.items())),
        },
        "entries": entries,
    }
    path = out_dir / "version-timeline.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    print(f"{total} doc units across {len(tags)} version tags → {distinct} minor series")
    print(f"  unknown (no blame): {unknown}")
    for series, count in sorted(by_series.items()):
        bar = "#" * max(1, round(40 * count / total))
        print(f"  {series:>10}  {count:>4}  {bar}")
    print(f"→ {path}")

    if args.report:
        by = v["unresolvedBy"]
        print("\n--- timeline viability ---", file=sys.stderr)
        print(f"{v['unresolved']} of {total} doc units ({v['unresolvedShare']:.1%}) "
              f"resolved to no release series: {by['noBlame']} no blame, "
              f"{by['afterLastTag']} after the last version tag", file=sys.stderr)
        if v["largestSeries"] is not None:
            print(f"largest series {v['largestSeries']!r} holds "
                  f"{v['largestSeriesShare']:.1%} of the doc units that did",
                  file=sys.stderr)
            print(f"{v['outsideLargestSeries']} doc units sit outside it",
                  file=sys.stderr)
        if v["verdictReason"]:
            print(f"VERDICT: {v['verdict']} ({v['verdictReason']}) — "
                  f"{REASON_TEXT[v['verdictReason']]}", file=sys.stderr)
        else:
            print(f"VERDICT: {v['verdict']} — {VERDICT_TEXT[v['verdict']]}",
                  file=sys.stderr)
        print(STANDING_RULE, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
