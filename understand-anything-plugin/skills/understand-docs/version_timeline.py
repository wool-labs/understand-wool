#!/usr/bin/env python3
"""version_timeline.py — date each doc unit and resolve it to a release.

Stage 0b of the documentation claim graph (see CLAIM-GRAPH-PLAN.md). Deterministic,
stdlib + git only, no LLM.

Answers one question: **can a contradiction between two doc units be oriented by
recency?** If it can, Phase D emits a directed `supersedes` instead of a symmetric
`contradicts`, which takes the claim graph out of the degenerate case where Dung
semantics collapse to "claims nothing contradicts."

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

**The go/no-go signal is `--report`.** If most doc units cluster into one release,
blame is being defeated by bulk reformatting and `supersedes` is not viable —
Phase D should keep symmetric `contradicts` and the plan should say so.

Usage:
    python3 version_timeline.py <repo-root> --doc-units <doc-units.json> [--out DIR]

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
from collections import Counter, defaultdict
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
    """Tags as (committer_timestamp, name), oldest first.

    Sorted by date rather than parsed semver: date order is what bisection needs,
    and it sidesteps pre-release ordering entirely (`v0.12.0-rc10` vs `-rc9` sorts
    correctly by date and incorrectly by string).
    """
    raw = git(root, "for-each-ref", "--sort=creatordate",
              "--format=%(creatordate:unix)\t%(refname:short)", "refs/tags")
    tags: list[tuple[int, str]] = []
    for line in raw.strip().split("\n"):
        if not line:
            continue
        ts, name = line.split("\t", 1)
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

    One blame per file rather than one per doc unit: 49 subprocess calls instead
    of 420, same result.
    """
    try:
        raw = git(root, "blame", "--line-porcelain", "--", rel)
    except subprocess.CalledProcessError:
        return {}

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
            out[lineno] = (sha, author_time)
            sha = lineno = author_time = None
    return out


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
    out_dir = args.out or (resolve_ua_dir(root) / "docs")
    out_dir.mkdir(parents=True, exist_ok=True)

    doc_units = json.loads(args.doc_units.read_text())["docUnits"]
    tags = load_tags(root)
    if not tags:
        print("no tags — cannot resolve releases", file=sys.stderr)

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

    by_series = Counter(e["minorSeries"] or "unknown" for e in entries)
    by_release = Counter(e["release"] or "unknown" for e in entries)
    total = len(entries)
    largest = by_series.most_common(1)[0] if by_series else ("none", 0)
    distinct = len([s for s in by_series if s != "unknown"])

    # Viability: supersedes needs pairs to land in *different* minor series often
    # enough to be worth orienting. If one series holds nearly everything, blame
    # has been flattened by a bulk rewrite and orientation is mostly unavailable.
    concentration = largest[1] / total if total else 1.0
    orientable = sum(c for s, c in by_series.items() if s != "unknown") - largest[1]

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "root": str(root),
        "tagCount": len(tags),
        "stats": {
            "docUnits": total,
            "unknown": unknown,
            "distinctMinorSeries": distinct,
            "largestSeries": largest[0],
            "largestSeriesShare": round(concentration, 4),
            "outsideLargestSeries": orientable,
            "byMinorSeries": dict(sorted(by_series.items())),
            "byRelease": dict(sorted(by_release.items())),
        },
        "entries": entries,
    }
    path = out_dir / "version-timeline.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    print(f"{total} doc units across {len(tags)} tags → {distinct} minor series")
    print(f"  unknown (no blame): {unknown}")
    for series, count in sorted(by_series.items()):
        bar = "#" * max(1, round(40 * count / total))
        print(f"  {series:>10}  {count:>4}  {bar}")
    print(f"→ {path}")

    if args.report:
        print("\n--- supersedes viability ---", file=sys.stderr)
        print(f"largest series {largest[0]!r} holds {concentration:.1%} of doc units",
              file=sys.stderr)
        print(f"{orientable} doc units sit outside it and could orient a contradiction",
              file=sys.stderr)
        if concentration > 0.9:
            print("VERDICT: not viable — blame is flattened into one series. "
                  "Keep symmetric `contradicts`.", file=sys.stderr)
        elif distinct < 3:
            print("VERDICT: marginal — too few distinct series to orient much.",
                  file=sys.stderr)
        else:
            print("VERDICT: viable — enough spread to orient contradictions by recency.",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
