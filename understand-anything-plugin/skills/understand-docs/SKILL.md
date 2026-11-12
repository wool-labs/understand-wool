# understand-docs — documentation claim graph

Extracts every individual claim from a codebase's docstrings, reconciles them across independent extraction passes, verifies each against the code, and emits the result as nodes on the project's knowledge graph.

Not a user-facing slash command. These scripts are run in sequence by an operator or a driving session; the LLM work happens in dispatched subagents using the prompts in `prompts/`.

---

## Why it is shaped this way

Three constraints drive nearly every design decision here, and all three were measured rather than assumed:

**Extraction is not deterministic, so it is repeated.** Five independent agents extract from the same doc units; claims that fewer than three passes produce are dropped. Canonical-form rules in the extraction prompt moved agreement 41% → 59% on wool with no other change — prompt text is the highest-leverage lever in this pipeline.

**Verification must not be circular.** Claims come *from* docstrings, so an agent verifying one will happily quote the docstring back as proof. `strip_docstrings.py` removes the option: agents read a mirror where every docstring is blanked but **line numbers are preserved**, so cited evidence still maps to the real file. Code that shares a line with a docstring stays in the mirror, and a file that cannot be parsed is left out of the mirror, because a verbatim copy would carry its docstrings.

**Evidence is checked, not trusted.** Every citation names a file, a line range, and a verbatim quote — all three machine-verified against the mirror before any consensus is computed. The file must resolve to a location inside the mirror: a path that leaves it through `..`, a symlink, or an absolute path is rejected with the reason `citation escapes the mirror`, since the file it reaches still has its docstrings. Such a citation is more than a bad citation: it shows the pass read source outside the mirror, so step 8 prints a `WARNING … citation(s) escape the mirror` line naming the pass. A consensus over fabricated citations launders a guess into a number. Measured across two rounds: 4,273 citations, 0 rejected.

---

## Phase order

Each script is deterministic, stdlib-only, and prints what it did. Nothing here calls an LLM; the agent steps are marked.

| # | step | script / agent | output |
|---|---|---|---|
| 1 | **Harvest** doc units + E0 field checks | `harvest_docs.py` | `doc-units.json`, `e0-candidates.json` |
| 2 | **Scope facts** — subsystems, vocabulary | `seed_scope_facts.py` | `scope-facts.json` |
| 2b | **Version timeline** (optional) | `version_timeline.py` | `version-timeline.json` |
| 3 | **Strip** docstrings into a mirror | `strip_docstrings.py --verify` | mirror tree |
| 4 | **Extract claims** — *N agents per subsystem, identical prompt* | agents + `prompts/extract-claims.md` | `claims-<sub>-pass<N>.json` |
| 5 | **Reconcile** passes into a claim set | `reconcile_claims.py` | `claims-<sub>.json` + rejected/excluded |
| 5b | **Lint** claims against their source quotes | `lint_claims.py` | review queue on stdout |
| 6 | **Bundle** claims for grounding | `build_grounding_bundles.py` | `ground-<sub>-<n>.json` |
| 7 | **Ground** — *2 agents per bundle* | agents + `prompts/ground-claims.md` | `<bundle>-pass<N>.json` |
| 8 | **Reconcile verdicts** | `reconcile_verdicts.py` | `grounded-<label>.json`, report |
| 9 | **Tie-break bundle** — disputed claims only | `build_tiebreak_bundle.py` | `tiebreak-<label>.json` |
| 10 | **Adjudicate** — *1 agent* | agent + `prompts/tiebreak-verdicts.md` | `verdicts-tiebreak.json` |
| 11 | **Re-reconcile** with adjudication | `reconcile_verdicts.py --tiebreak` | final `grounded-*.json` |
| 12 | **Design lens** — roles + blocking | `design_lens.py` | `design-claims.json`, `design-blocks.json` |
| 13 | **Design edges** — *N agents* | agents | `design-edges-<n>.json` |
| 14 | **Emit** into the knowledge graph | `emit_claim_graph.py` | merged `knowledge-graph.json` |

**Step 3 gates step 4.** Run it as:

```bash
python3 strip_docstrings.py <src-root> <dest-root> --verify
```

Proceed to step 4 in exactly two cases:

1. The script exits 0 and prints `verify: line counts preserved, 0 docstrings leaked, N symbols kept in place`.
2. The script exits 1, `verify FAILED: UNPARSEABLE` is the only `verify FAILED:` heading on stderr, and every file named in the `UNPARSEABLE — N file(s) left out of the mirror` list printed above that heading is one the table below tells you to leave out.

In every other case, stop. Steps 1 and 3 read files the same way, so a file step 3 leaves out is one step 1 skipped: step 1 prints a `SKIPPED (unparseable)` line on stderr for each, and no claim is extracted from it. A claim made elsewhere about code in a left-out file has nothing to be checked against and grades `unverifiable`. The script exits 1 before mirroring anything, with a `NO SOURCE` line, when `<src-root>` does not exist or holds no `.py` file; correct the path and run it again.

`--verify` runs five checks on the finished mirror and prints every failure, each under its own heading, before it exits:

| stderr heading | what it means | what to do |
|---|---|---|
| `UNPARSEABLE` | A source file could not be decoded or parsed as Python. It was left out of the mirror, and each such file is named in the list above the heading with its error. | Decide per file by compiling it under the Python the corpus targets (its `requires-python`, or the newest interpreter installed): `pythonX.Y -m py_compile <file>`. **If it compiles**, the interpreter running the pipeline is wrong for the corpus: re-run the pipeline **from step 1** under that Python. Re-running step 3 alone clears this failure and leaves the file's claims missing, because step 1 skipped the same file. **If it does not compile**, the file is broken, by intent (a syntax-error fixture) or by mistake: leave it out, continue to step 4, and name each such file to the user when you report the run. **If no such interpreter is available to test with**, stop and ask the user. |
| `LINE COUNT MISMATCH` | A mirrored file has a different number of lines than its source, so every citation into it would point at the wrong line. | Stop. This is a defect in `strip_docstrings.py`; do not ground against this mirror. |
| `MIRROR DOES NOT PARSE` | Stripping turned a valid source file into invalid Python. | Stop, as above. |
| `LEAK` | A docstring other than the `<docstring removed>` marker is still in the mirror, at the file and line listed. | Stop, as above. |
| `SYMBOL LOST` | A class or function in the source is absent from the mirror at its line. A grader looking for it would find nothing and answer `unverifiable`. | Stop, as above. |

Steps 12–14 are the *understanding* half and are independent of 7–11; a graph can be emitted with claims ungrounded, and each such claim node then reads `groundingVerdict: "ungrounded"` (see [Reading a claim node](#reading-a-claim-node)).

**To add verdicts to a graph that was emitted ungrounded**, run these in order:

1. Re-run step 12 with the grounded files: `design_lens.py claims-*.json --grounded grounded-*.json --out <dir>`. Verdicts reach the emitter only through the `design-claims.json` this writes; re-running step 14 alone leaves every node `ungrounded`.
2. Re-run step 14 with the earlier output as `--graph` **and the same `--edges` files as before**. The emitter removes every claim and principle node an earlier run wrote, with their edges, and writes them again; omitting `--edges` therefore deletes the principles and design edges without a warning.
3. Confirm the emitter printed `replaced N node(s)` and that the `ungrounded` count on the `occurrences:` line dropped.

**After step 14, read the two count lines the emitter prints** and report them with the graph:

- `occurrences:` counts claim occurrences by status before merging. Its `ungrounded` entry is the number of claims no grader checked. This is the coverage figure.
- `grounding:` counts claim nodes by status after merging. A `mixed` node may be fully graded, and may hold a `contradicted` or an `ungrounded` occurrence, so this line does not measure coverage.

A status absent from either line has a count of zero. A `WARNING unmapped verdicts` line means a stale artifact reached the emitter and the graph is not publishable.

`audit_graph_structure.py` is a separate measuring instrument, not part of the sequence — it reports how much of the real call graph a knowledge graph contains.

`lint_claims.py` guards the seam between extraction and grounding. Grounding grades a claim against code; if extraction has already widened the claim past what the docstring said, grounding correctly finds a counterexample and reports a defect the documentation never committed. Measured on wool: 3 of the 63 claims carrying a located boundary were manufactured that way. 10 flags across 813 claims is the baseline; a jump above it is a prompt regression.

---

## Grounding: two passes plus an adaptive tie-break

Blanket triple-passing wastes agents. Measured on wool, two independent passes agreed on **98.2%** of 813 claims; only 15 needed a third opinion, and every one of those landed on one of the two priors.

So: two passes, then `build_tiebreak_bundle.py` isolates only the disagreements, and one adjudicator resolves them with both priors in view. That is deliberate asymmetry — extraction passes are kept blind from each other because they want independent samples; adjudication wants the *best* answer, and the disagreement is itself evidence.

## Verdicts

`supported` · `contradicted` · `unverifiable`

Defined once in `prompts/_taxonomy.md`, which is included **byte-identical** in both agent prompts — `check_prompts.py --sync` regenerates the copies and the default run asserts they match, because a drift means the adjudicator applies a different rule than the passes it adjudicates, on exactly the claims that were hardest.

### Scope notes, and the fourth verdict that isn't

A claim that is true but narrower than it sounds stays `supported` and carries a **scope note**: the narrowed form the code actually supports, plus a verified `role: "limits"` citation of where it stops. No limiting citation, no note — an unsupported narrowing is an opinion.

There was briefly a fourth verdict, `overstated`, for exactly that case. It produced real findings (39 on the first full run) and was then measured against a control arm: the same 71 boundary-sensitive claims, re-grounded by fresh agents. `contradicted` reproduced **14/14**; `overstated` **17/31**. Rewording the prompt around a harm test scored 52% on the same set, so the instability was the category rather than its definition — a verdict whose whole content is *"there exists an excluded case, and here it is"* depends on a second grader **finding** that case, and eleven of the fourteen misses were graders who did not.

Its evidence never had that problem. Every `overstated` cited a bound that verified, and citation verification has not failed once across ~8,000 checks. So the label was withdrawn and the pointer kept.

Folding it back had a second effect worth knowing: replaying the 813-claim archive under three verdicts moved agreement from 787 unanimous / 26 split to **809 / 4**. Twenty-two of the twenty-six disputes were `supported` vs `overstated` — the category was generating most of the pipeline's disagreement, which corroborates the control arm from the opposite direction.

`reconcile_verdicts.py` still accepts `overstated` on input (`LEGACY_VERDICTS`) and folds it, so the frozen archive stays replayable. On a current run `foldedOverstated` should be 0; a rising number means an agent is working from a stale prompt.

**Do not re-propose the fourth verdict without a reproduction measurement.**

---

## Reading a claim node

This section is the contract for any agent that reads claim nodes out of the emitted graph. It applies to claim nodes carrying `emittedBy: "understand-docs"`. A `claim` node without that field comes from another producer (the article analyzer names claims `claim:<article>:<slug>` and gives them short titles); none of the guarantees below hold for it, and its grounding status is not recorded.

Three rules cover most reads:

- Key on `id`, and read `groundingVerdict` for a node's status. Do not infer status from the words `unverified` or `ungrounded` in `tags`.
- **To find every claim with a given status, match the node-level field or any entry of `groundingOccurrences`.** A node whose occurrences disagree reads `mixed` at node level, so a query for `contradicted` on the node-level field alone misses it. The `DRIFT` tag is the one tag that is safe to query: the emitter sets it on every node with at least one `contradicted` occurrence.
- **To find the claims about a symbol, follow `documents` edges into that symbol.** A merged node has one edge per symbol it is stated on.

### `groundingVerdict`

Every claim node `emit_claim_graph.py` writes carries `groundingVerdict`. On a current run it takes one of five values. Three are the grader verdicts above. Two are statuses the emitter derives, which no grader casts and which therefore do not appear in `prompts/_taxonomy.md`:

| `groundingVerdict` | tag | what it states |
|---|---|---|
| `supported` | `verified` | graded; the code does what the claim says |
| `contradicted` | `DRIFT` | graded; the code does otherwise |
| `unverifiable` | `unverified` | graded; no executable code bears on the claim |
| `ungrounded` | `ungrounded` | **not graded** — no grader cast a verdict on this claim |
| `mixed` | `mixed` (plus `DRIFT` if any occurrence is `contradicted`) | the node merges occurrences whose verdicts differ; read `groundingOccurrences` |

`ungrounded` and `unverifiable` are opposite statements about how much work has been done. Report an `ungrounded` claim as unchecked, never as inconclusive. The tag `unverified` belongs to `unverifiable`: it marks a claim a grader examined.

A sixth value can appear only when a stale artifact reaches the emitter without passing back through `reconcile_verdicts.py`, for example a pre-fold `grounded-*.json` carrying `overstated`. The emitter passes that string through unchanged and prints `WARNING unmapped verdicts`. A graph emitted with that warning is not publishable; re-run step 8 on the stale input, which folds the legacy verdict, and emit again.

### `groundingAgreement`

`groundingAgreement` records how the graders arrived at the verdict. Read it beside the verdict, because three levels change what the verdict means: `majority`, `split`, and `missing`.

| `groundingAgreement` | what it states |
|---|---|
| `unanimous` | every grader returned the same verdict |
| `majority` | more than half of the returned verdicts agree, and not every expected grader agreed. With the two passes this pipeline runs, that means **only one grader returned a verdict**: the verdict is uncorroborated |
| `adjudicated` | the graders split and the tie-break agent decided |
| `split` | the graders split and **no tie-break has run**; the verdict shown is a placeholder chosen by a fixed priority, and the claim is disputed |
| `missing` | a grounding pass covered the claim's bundle and no grader returned a verdict for it; the node reads `ungrounded` |

An `ungrounded` node with no `groundingAgreement` and no `groundingOccurrences` was never sent to a grader at all. On a merged node, read the level per occurrence.

### Merged occurrences

Two claims are the same sentence when their text is equal after collapsing each run of whitespace to one space, stripping all trailing periods, and case-folding. The emitter merges them into one node. Each occurrence is graded separately, usually against a different symbol. The node takes the `id` and the spelling of the occurrence with the lowest claim id, and carries `groundingOccurrences`, one entry per occurrence, canonical first:

```json
"groundingVerdict": "mixed",
"groundingOccurrences": [
  {"claimId": "claim:1a…", "attachedTo": "function:pkg/pool.py:Pool.spawn",
   "filePath": "pkg/pool.py", "lineRange": [6, 7],
   "groundingVerdict": "supported", "groundingAgreement": "unanimous",
   "scopeNote": {"asSupported": "…", "bound": [{"file": "pkg/pool.py", "lines": [8], "role": "limits"}]}},
  {"claimId": "claim:9f…", "attachedTo": "function:pkg/proxy.py:Proxy.spawn",
   "filePath": "pkg/proxy.py", "lineRange": [40, 41],
   "groundingVerdict": "ungrounded"}
]
```

Rules for reading a node that has `groundingOccurrences`:

- A verdict, agreement level, or scope note inside an entry holds for that entry's occurrence and for no other. `attachedTo` names its symbol, and `filePath` with `lineRange` locate the docstring it was read from. Two entries can share one `attachedTo`; `claimId` tells them apart.
- The node-level `filePath` and `lineRange` are the canonical occurrence's. Cite an occurrence's own location when reporting its verdict.
- The node has one `documents` edge per distinct symbol among its occurrences, and each edge's description states the verdict earned on that symbol.
- The node-level `groundingVerdict` is the shared verdict when all occurrences agree, and `mixed` when they do not.
- Node-level `groundingAgreement` and `scopeNote` appear only when every occurrence carries the same value. When either is absent from the node, read it per occurrence. A `mixed` node never carries either.
- A claim id taken from a `grounded-*.json` or `design-claims.json` file may be a non-canonical occurrence. When no node has that `id`, search `groundingOccurrences[].claimId`.

A node with a single occurrence omits the list, and its node-level fields are that occurrence's.

### `name`

`name` is the full claim text and is unique among the claim nodes this skill emits, so a lookup by `name` returns one claim. On a merged node it is the canonical occurrence's spelling. Prefer lookup by `id` or `groundingOccurrences[].claimId`; to match by text, apply the merge rule above to both sides.

Uniqueness survives re-emission. Running step 14 with a previous output as `--graph` removes every node an earlier run wrote, with its edges, and the emitter prints how many it replaced. It recognizes those nodes by `emittedBy: "understand-docs"`, and, for a graph emitted before that stamp existed, by the id shape only this pipeline mints (`claim:` followed by 64 hex digits). It does not append a second copy.

---

## Invariants worth not breaking

- **The mirror is the only source agents read, and the only place a citation may point.** Pointing agents at real source reintroduces circularity silently — the output still looks fine. `reconcile_verdicts.py` enforces the citation half by rejecting any path that resolves outside `--mirror`. **If step 8 or step 11 prints `WARNING … citation(s) escape the mirror`, discard each pass it names and re-run that pass** before using the result. A pass named `tiebreak` is the adjudication: re-run step 10, then step 11. Any other name is the stem of a step-7 pass file: re-run that agent on its bundle, then re-run step 8 and, if a tie-break follows, steps 9–11. The same warning is at the top of `grounding-report-<label>.md`, the count and pass names are in `stats.escapedCitations` of `grounded-<label>.json` (the key is absent when there are none), and each rejected citation is in `evidenceFailures[].failure`. A pass that read real source may have leaned on it for verdicts whose citations verify, which is why the whole pass is discarded. The warning detects only a citation whose path leaves the mirror; its absence does not show that no pass read real source.
- **A file enters the mirror stripped or not at all.** `strip_docstrings.py` never copies a file it could not parse.
- **Evidence verification runs before consensus**, never after.
- **Quorum counts distinct pass indices**, never a sum. Summing lets one pass clear quorum alone by emitting two wordings of the same assertion.
- **Excluded and rejected claims are written out**, never dropped silently.
- **Every claim node carries `groundingVerdict`, and `name` is never abbreviated.** An absent verdict reads as "inconclusive" to a consumer; a truncated name makes two claims one.
- **Claim IDs are content hashes** (`docUnitId` + normalized text). Re-deriving them on a changed corpus invalidates any paired before/after comparison.

## Running the tests

From the repository root:

```bash
pytest                          # the whole suite — unit + integration
pytest -m integration           # the multi-stage claim pipeline only
pytest -m "not integration"     # unit suites only
```

`tests/skill/docs/` holds one unit suite per script; `tests/integration/` runs the reconcile → lens → emit chain against real on-disk artifacts across a pairwise covering array of the dimensions that vary a run. `check_prompts.py`'s invariants run there too, via `tests/skill/docs/test_check_prompts.py` — they were a manual command for long enough that the taxonomy drifted underneath them.

Two ad-hoc tools, run by hand rather than by CI:

```bash
python3 check_prompts.py --sync          # rewrite the shared block into both prompts, then check
python3 lint_claims.py --bundles <DIR>   # extraction-artifact review queue
```

Edit the taxonomy in `prompts/_taxonomy.md` and run `--sync`; never hand-edit the copies inside the two prompts. The copies are what the agents read, and a hand-edit that lands in one of them is the exact drift `check_prompts.py` exists to catch.

Requires Python 3.10+ (`X | None` syntax throughout).
