#!/usr/bin/env python3
"""check_prompts.py — guard the invariants the grounding prompts rely on.

Run: python3 check_prompts.py          check
     python3 check_prompts.py --sync   rewrite the shared block, then check

These are not style checks. Each one guards a failure that has actually cost
something, or that would be invisible if it happened.

`--sync` copies `prompts/_taxonomy.md` into the marked region of both prompts.
The block was kept identical by hand-pasting, which is exactly the thing that
drifts — and a drifted taxonomy means the adjudicator applies a different rule
than the passes it adjudicates, on the claims that were hardest to judge.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
PROMPTS = HERE / "prompts"

BEGIN = "<!-- BEGIN SHARED TAXONOMY -->"
END = "<!-- END SHARED TAXONOMY -->"
CONSUMERS = ("ground-claims.md", "tiebreak-verdicts.md")

FAILURES: list[str] = []


def sync() -> int:
    taxonomy = (PROMPTS / "_taxonomy.md").read_text().rstrip("\n")
    for name in CONSUMERS:
        path = PROMPTS / name
        text = path.read_text()
        if BEGIN not in text or END not in text:
            print(f"  FAIL  {name} has no {BEGIN} / {END} markers")
            return 1
        head, rest = text.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        new = f"{head}{BEGIN}\n{taxonomy}\n{END}{tail}"
        if new == text:
            print(f"  ok    {name} already in sync")
        else:
            path.write_text(new)
            print(f"  SYNC  {name} taxonomy block rewritten")
    return 0


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")


def main() -> int:
    if "--sync" in sys.argv:
        print("syncing shared taxonomy block:")
        if sync() != 0:
            return 1
        print()

    taxonomy = (PROMPTS / "_taxonomy.md").read_text()
    ground = (PROMPTS / "ground-claims.md").read_text()
    tiebreak = (PROMPTS / "tiebreak-verdicts.md").read_text()
    reconcile = (HERE / "reconcile_verdicts.py").read_text()

    print("prompt invariants:")

    # If these drift, the adjudicator applies a different rule than the passes it
    # adjudicates — on precisely the claims that were hardest to judge.
    check("taxonomy block is byte-identical in both prompts",
          taxonomy.rstrip("\n") in ground and taxonomy.rstrip("\n") in tiebreak)
    for name, text in (("ground", ground), ("tiebreak", tiebreak)):
        check(f"{name} prompt carries the sync markers",
              BEGIN in text and END in text)

    # The prompts define the vocabulary the script enforces; a mismatch means
    # agents emit a verdict the reconciler silently rewrites.
    verdicts = re.search(r"^VERDICTS = \((.*?)\)", reconcile, re.M | re.S)
    declared = set(re.findall(r'"(\w+)"', verdicts.group(1))) if verdicts else set()
    check("reconcile_verdicts.VERDICTS == 3 known values",
          declared == {"supported", "contradicted", "unverifiable"},
          str(sorted(declared)))
    for v in sorted(declared):
        check(f"  prompt documents verdict `{v}`", f"`{v}`" in taxonomy)

    # A scope note without a limiting citation is dropped by the reconciler.
    # If the prompt does not say so, agents lose findings without knowing why.
    check("prompt names the `limits` role", '"limits"' in taxonomy or "`limits`" in taxonomy)
    check("prompt warns that a bound-less scope note is dropped",
          "a scope note without a verified limiting citation is dropped" in taxonomy)
    check("BOUND_ROLES in the script matches the prompt",
          'BOUND_ROLES = ("limits", "contradicts")' in reconcile)

    # The fourth verdict was withdrawn after a control arm measured it
    # reproducing at 17/31 against `contradicted`'s 14/14. Nothing should
    # reintroduce it, and the taxonomy should say why so the next reader does
    # not re-propose it from first principles.
    check("no verdict named `overstated` survives in the taxonomy",
          "| `overstated`" not in taxonomy)
    check("taxonomy explains why the fourth verdict was withdrawn",
          "withdrawn" in taxonomy and "17 times in 31" in taxonomy)
    check("taxonomy documents scope notes", "scopeNote" in taxonomy)
    for name, text in (("ground", ground), ("tiebreak", tiebreak)):
        check(f"{name} output schema carries scopeNote",
              '"scopeNote"' in text.split("## Output", 1)[-1])
    check("reconcile_verdicts builds scope notes and folds legacy verdicts",
          "def scope_note(" in reconcile and "LEGACY_VERDICTS" in reconcile)

    # Calibration examples move inter-rater agreement more than definitions do.
    # A and E are the pair that matters: same shape, opposite verdict, and the
    # difference is only what a reader does with the sentence.
    for label in ("A —", "B —", "C —", "D —", "E —"):
        check(f"calibration example {label.strip(' —')} present", label in taxonomy)

    # Run-specific counts made the old tie-break prompt single-use.
    hardcoded = re.findall(r"\((\d+) claims\)", tiebreak) + re.findall(r"all (\d+)\b", tiebreak)
    check("tie-break prompt carries no hard-coded run counts", not hardcoded, str(hardcoded))

    # Paths must be injected, or the prompt pins itself to one run's scratch dir.
    for token in ("{MIRROR}", "{INPUT}", "{OUTPUT}"):
        check(f"tie-break prompt uses {token} placeholder", token in tiebreak)
        check(f"ground prompt uses {token} placeholder", token in ground)
    check("no absolute /tmp paths remain in prompts",
          "/tmp/" not in ground and "/tmp/" not in tiebreak)

    # The anti-circularity control is the load-bearing one.
    check("ground prompt forbids prose as evidence",
          "Evidence must be executable code" in ground)
    check("ground prompt points at the stripped mirror",
          "<docstring removed>" in ground)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all prompt invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
