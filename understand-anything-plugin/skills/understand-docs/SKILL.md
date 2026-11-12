# understand-docs — documentation claim graph

Extracts every individual claim from a codebase's docstrings, reconciles them across independent extraction passes, verifies each against the code, and emits the result as nodes on the project's knowledge graph.

Not a user-facing slash command. These scripts are run in sequence by an operator or a driving session; the LLM work happens in dispatched subagents using the prompts in `prompts/`.

---

## Why it is shaped this way

Three constraints drive nearly every design decision here, and all three were measured rather than assumed:

**Extraction is not deterministic, so it is repeated.** Five independent agents extract from the same doc units; claims that fewer than three passes produce are dropped. Canonical-form rules in the extraction prompt moved agreement on wool's load-balancing subsystem from 41.5% to 60.0% (claims of all four types produced by at least two of five passes, exact match under the shipped normaliser) with no other change — prompt text is the highest-leverage lever in this pipeline.

**Verification must not be circular.** Claims come *from* docstrings, so an agent verifying one will happily quote the docstring back as proof. `strip_docstrings.py` removes the option: agents read a mirror where every docstring is blanked but **line numbers are preserved**, so cited evidence still maps to the real file. Code that shares a line with a docstring stays in the mirror, and a file that cannot be parsed is left out of the mirror, because a verbatim copy would carry its docstrings.

**Evidence is checked, not trusted.** Every citation names a file, a line range, and a verbatim quote — all three machine-verified against the mirror before any consensus is computed. Every non-blank line of the quote must appear within two lines of the cited range, on either side. The file must resolve to a location inside the mirror: a path that leaves it through `..`, a symlink, or an absolute path is rejected with the reason `citation escapes the mirror`, since the file it reaches still has its docstrings. Such a citation is more than a bad citation: it shows the pass read source outside the mirror, so step 8 prints a `WARNING … citation(s) escape the mirror` line naming the pass. A consensus over fabricated citations launders a guess into a number. Measured across every archived wool round: 9,485 citations checked, 1 rejected. That one quoted an attribute docstring the first mirror had failed to strip; it verified against that mirror and is rejected against the current one. The two-line tolerance is not what produces the figure: 9,484 of the 9,485 match their cited range exactly, with no tolerance.

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
| 4b | **Check quotes** — must exit 0 before step 5 | `lint_claims.py --doc-units --quotes-only` | pass or fail on stdout |
| 5 | **Reconcile** passes into a claim set | `reconcile_claims.py` | `claims-<sub>.json` + rejected/excluded |
| 5b | **Lint** claims against their source quotes | `lint_claims.py --doc-units` | review queue on stdout |
| 6 | **Bundle** claims for grounding — must exit 0 | `build_grounding_bundles.py` | `ground-<sub>-<n>.json`, `orphaned-<sub>.json` |
| 7 | **Ground** — *2 agents per bundle* | agents + `prompts/ground-claims.md` | `<bundle>-pass<N>.json` |
| 8 | **Reconcile verdicts** | `reconcile_verdicts.py` | `grounded-<label>.json`, report |
| 9 | **Tie-break bundle** — disputed claims only | `build_tiebreak_bundle.py` | `tiebreak-<label>.json` |
| 10 | **Adjudicate** — *1 agent* | agent + `prompts/tiebreak-verdicts.md` | `verdicts-tiebreak.json` |
| 11 | **Re-reconcile** with adjudication | `reconcile_verdicts.py --tiebreak` | final `grounded-*.json` |
| 12 | **Design lens** — roles + blocking | `design_lens.py` | `design-claims.json`, `design-blocks.json` |
| 13 | **Design edges** — *N agents* | agents | `design-edges-<n>.json` |
| 14 | **Emit** into the knowledge graph | `emit_claim_graph.py` | merged `knowledge-graph.json` |

**Step 2b is a measurement, and no later step reads its result.** It reports whether the dates of doc units are spread widely enough that recency could tell which of two contradicting doc units is newer. Step 13 is unaffected by the result: a contradiction between two claims is one `contradicts` edge for the pair, in either order, whatever step 2b reports. Do not order the pair by recency, and never write a `supersedes` edge: step 14 drops any edge type it does not know, so the contradiction would vanish from the graph. The step is optional; run it when the user wants the measurement, report the verdict to them, and continue the pipeline. Nothing in this step calls for a change to the user's repository: do not commit, stage, stash, fetch, tag, or unshallow on its account.

Run it as `python3 version_timeline.py <repo-root> --doc-units <doc-units.json> --report`, where `<repo-root>` is the directory step 1 was given and the checkout is the commit step 1 harvested. It prints a `VERDICT:` line on stderr and writes the verdict to `stats.verdict` in `version-timeline.json`, with the reason, when there is one, in `stats.verdictReason`. The script exits 0 on every verdict, so read the line. A non-zero exit with a traceback and no `VERDICT:` line means an argument is wrong: `<repo-root>` is not inside a git repository, or the doc-units path is missing or not JSON. Correct the argument and run again.

| verdict | what it means |
|---|---|
| `viable` | The doc units that resolved to a release series fall in three or more series, and no series holds more than 90% of them. |
| `marginal` | They fall in fewer than three series, and no series holds more than 90% of them. |
| `not viable` | One series holds more than 90% of the resolved doc units. This is tested before `marginal`, so a corpus entirely in one series is `not viable`. The cause is a bulk reformat that flattened `git blame`, or a project that has had one minor series; the script cannot tell which. |
| `undetermined` | The spread could not be measured. The line carries a reason in parentheses, listed below. |

An `undetermined` verdict names one of five reasons, tested in this order. In every row, "report" means tell the user the verdict and reason, then continue the pipeline; it never means halting the run.

| reason | what it means | what to do |
|---|---|---|
| `shallow clone` | `git rev-parse --is-shallow-repository` prints `true`, so history and tags are truncated. | Report that step 2b was not measured. Do not unshallow the clone without asking. Running on a different, full clone is valid only if it is at the same commit and step 1 is re-run there to produce the doc-units file; line numbers from another commit date the wrong lines without any warning. |
| `no doc units` | The doc-units file lists no doc units (`"docUnits": []`). | If step 1 has not run, run it. If it ran and found none, report that. |
| `no version tags` | The clone has no tag of the form `1.2.3` or `v1.2.3`. Tags of any other form are ignored. | Run `git ls-remote --tags origin`. If it lists version tags, the clone is missing them: ask the user before fetching. Otherwise report that the project has no version tags. |
| `blame failed` | More than half of the doc units resolved to no series, and for more than half of those `git blame` found no committed line. | Either `<repo-root>` is not the directory step 1 was given, or the files have uncommitted changes. Check the first with `git -C <repo-root> ls-files --error-unmatch <sourceFile>` on one `sourceFile` from the doc-units file; if it fails, run once more with the right root. Otherwise report it. |
| `unreleased` | More than half of the doc units resolved to no series, and `git blame` failed for no more than half of those: the rest were written after the last version tag. | Report it. This is a fact about the repository and a re-run cannot change it. |

Three counts in the output are easy to confuse. `stats.unresolved` is every doc unit with no series. `stats.unresolvedBy` splits it into `noBlame` and `afterLastTag`, and the report's first line prints the same split. `stats.unknown`, printed as `unknown (no blame)`, equals `noBlame`. The `(none)` row of the histogram, of `byMinorSeries`, and of `byRelease` each equals `stats.unresolved`.

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

**Step 4b gates step 5: every `sourceQuote` must be in its doc unit.** The extraction prompt tells agents the quote is checked, and the reconciler merges two passes' wordings on the strength of it. For each subsystem, run the check over that subsystem's pass files and the doc-units file step 1 wrote:

```bash
python3 lint_claims.py claims-<sub>-pass1.json claims-<sub>-pass2.json ... --doc-units doc-units.json --quotes-only
```

It prints the number of claims read from each file, then exits 0 when every quote of every claim appears in the `text` of the doc unit its claim names, as one contiguous span that matches exactly apart from line breaks and runs of spaces. Confirm that every pass file is listed with the number of claims you expect. It exits 1 otherwise, and prints one `QUOTE` block per failure with the reason and, in brackets, the pass file it came from, or a `FAIL: no claims read from …` line for a pass file that held none. Act on the reason:

| reason | what it means | what to do |
|---|---|---|
| `doc unit … is not in --doc-units`, on most claims of every pass | The passes were extracted from a different harvest than the file passed. | Pass the doc-units file the extraction agents were given, and run the check again. Do not re-run extraction. |
| `doc unit … is not in --doc-units`, on a few claims of a pass whose other claims resolve | The agent miscopied or omitted a `docUnitId`. | Discard the pass file named in brackets and re-run that one extraction pass, then run the check again. |
| `the quote is not in the doc unit's text` | The agent paraphrased, re-cased, joined two parts with `...`, or invented the quote. | As the row above. Do not edit the quote or delete the claim by hand. |
| `the claim has no sourceQuote` | The agent omitted the field. | As above. |
| `FAIL: no claims read from …` | The pass file is empty or malformed. | As above. |

Do not start step 5 until the check exits 0 for every subsystem. If a pass fails the check again after one re-run, stop and tell the user which pass and which quotes; do not re-run it a third time.

**Step 5b reviews the reconciled claims.** `lint_claims.py` guards the seam between extraction and grounding: a claim that asserts more than its docstring did makes grounding report a defect the documentation never committed. For each subsystem, pass the one file of surviving claims step 5 wrote, `claims-<sub>.json`:

```bash
python3 lint_claims.py claims-<sub>.json --doc-units doc-units.json
```

Name the file; do not use a `claims-*.json` glob. Step 5 writes `claims-rejected-<sub>.json` and `claims-excluded-<sub>.json` in the same directory, and step 4's pass files match the same pattern. The script skips the rejected and excluded files with a `SKIPPED` line on stderr; it cannot tell a pass file from a claim set, and linting pass files here inflates every count.

It prints three count lines, then one block per flag:

| stdout line | what a flag means | what to do |
|---|---|---|
| `quote not in unit` | As in step 4b, for the one representative quote the reconciler kept per claim. | It should be 0, since the passes were checked at step 4b. If it is not, the pass files were changed or step 4b was skipped: go back to step 4b. |
| `dropped qualifier` | The quote narrows with `when`, `only`, `unless`, `via`, or similar, and the claim does not carry the condition. | See below. |
| `one-sided split` | The quote coordinates two things and the claim asserts one, with no other claim on the same doc unit in the same file carrying the other. | See below. |

A `dropped qualifier` or `one-sided split` flag is a claim to read with care, and the claim set is not changed on its account. Continue to step 6 with every claim as it is; do not edit a `claimText`, which would change the claim's id, and do not delete the claim. Record the flagged claim ids, and when you report the run, list them and mark any `contradicted` verdict on one of them as resting on a claim the linter flagged.

On wool's 813-claim grounding set the baseline is 0 quotes missing, 3 dropped qualifiers, and 7 one-sided splits, 10 review flags or 1.2% of claims, with each of its two subsystems checked against the doc-units file of its own harvest. If the two review counts together exceed 2.5% of the claims linted, twice that rate, stop before step 6 and tell the user: the extraction prompt is the likely cause.

Exit codes: 0 when no quote is missing; 1 when any quote is missing, or when `--max-flags N` is given and the two review counts together exceed `N`; 2 when no claims were read. The two review counts do not fail the run on their own, so read them. Without `--doc-units` the `quote not in unit` line reads `not checked (pass --doc-units)` and the quotes are unverified.

**Step 6 must exit 0.** `build_grounding_bundles.py` prints an `orphaned` count with its bundle list and writes `orphaned-<sub>.json` beside the bundles on every run (`<sub>` is the claims file's name without `claims-` and `.json`, unless `--label` overrides it). A claim is orphaned when the doc unit it names is not in `--doc-units`; it is in no bundle and will receive no verdict. When there are any, the script prints an `ORPHANED` line on stderr and exits 1. With step 4b passed against the same doc-units file, the only cause is a different doc-units file at this step: pass the one used at step 4b and run step 6 again. Do not dispatch grounding agents on the bundles of a run that exited 1.

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

---

## Grounding: two passes plus an adaptive tie-break

Blanket triple-passing wastes agents. Measured on wool in the three-verdict round, two independent passes agreed on **98.2%** of 813 claims (798 unanimous, 15 split); only those 15 needed a third opinion, and every one of them landed on one of the two priors.

So: two passes, then `build_tiebreak_bundle.py` isolates only the disagreements, and one adjudicator resolves them with both priors in view. That is deliberate asymmetry — extraction passes are kept blind from each other because they want independent samples; adjudication wants the *best* answer, and the disagreement is itself evidence.

## Verdicts

`supported` · `contradicted` · `unverifiable`

Defined once in `prompts/_taxonomy.md`, which is included **byte-identical** in both agent prompts — `check_prompts.py --sync` regenerates the copies and the default run asserts they match, because a drift means the adjudicator applies a different rule than the passes it adjudicates, on exactly the claims that were hardest.

### Scope notes, and the fourth verdict that isn't

A claim that is true but narrower than it sounds stays `supported` and carries a **scope note**: the narrowed form the code actually supports, plus a verified `role: "limits"` citation of where it stops. No limiting citation, no note — an unsupported narrowing is an opinion.

There was briefly a fourth verdict, `overstated`, for exactly that case. It produced real findings (53 on the first full run, 39 of them on claims the three-verdict round had graded `supported`) and was then measured against a control arm: the same 71 boundary-sensitive claims, re-grounded by fresh agents. `contradicted` reproduced **14/14**; `overstated` **17/31**, counting the 31 claims both passes had graded `overstated`. Rewording the prompt around a harm test scored 52% on the same set, so the instability was the category rather than its definition — a verdict whose whole content is *"there exists an excluded case, and here it is"* depends on a second grader **finding** that case, and eleven of the fourteen misses were graders who did not.

Its evidence never had that problem. Every `overstated` cited a bound that verified, as do 9,484 of the 9,485 citations in the archive. So the label was withdrawn and the pointer kept.

Folding it back had a second effect worth knowing: replaying the four-verdict round's pass files for the same 813 claims under three verdicts moved agreement from 787 unanimous / 26 split to **809 / 4**. (The 798 / 15 above is the earlier three-verdict round, a separate set of passes over the same claims.) Twenty-two of the twenty-six disputes were `supported` vs `overstated` — the category was generating most of the pipeline's disagreement, which corroborates the control arm from the opposite direction.

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
- **Excluded, rejected, and orphaned claims are written out**, never dropped silently. `reconcile_claims.py` writes the first two; `build_grounding_bundles.py` writes `orphaned-<sub>.json` and exits 1 when it is not empty.
- **Every `sourceQuote` is checked against its doc unit** by `lint_claims.py --doc-units --quotes-only` over the pass files, at step 4b. The extraction prompt promises agents the check, and the reconciler keeps only one quote per surviving claim, so the pass files are the only place every quote can be checked.
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

One tool is run by hand rather than by CI:

```bash
python3 check_prompts.py --sync          # rewrite the shared block into both prompts, then check
```

Edit the taxonomy in `prompts/_taxonomy.md` and run `--sync`; never hand-edit the copies inside the two prompts. The copies are what the agents read, and a hand-edit that lands in one of them is the exact drift `check_prompts.py` exists to catch.

Requires Python 3.10+ (`X | None` syntax throughout).
