# understand-docs — documentation claim graph

Extracts every individual claim from a codebase's docstrings, reconciles them across independent extraction passes, verifies each against the code, and emits the result as nodes on the project's knowledge graph.

Not a user-facing slash command. These scripts are run in sequence by an operator or a driving session; the LLM work happens in dispatched subagents using the prompts in `prompts/`.

---

## Why it is shaped this way

Three constraints drive nearly every design decision here, and all three were measured rather than assumed:

**Extraction is not deterministic, so it is repeated.** Five independent agents extract from the same doc units; claims that fewer than three passes produce are dropped. Canonical-form rules in the extraction prompt moved agreement 41% → 59% on wool with no other change — prompt text is the highest-leverage lever in this pipeline.

**Verification must not be circular.** Claims come *from* docstrings, so an agent verifying one will happily quote the docstring back as proof. `strip_docstrings.py` removes the option: agents read a mirror where every docstring is blanked but **line numbers are preserved**, so cited evidence still maps to the real file.

**Evidence is checked, not trusted.** Every citation names a file, a line range, and a verbatim quote — all three machine-verified against the mirror before any consensus is computed. A consensus over fabricated citations launders a guess into a number. Measured across two rounds: 4,273 citations, 0 rejected.

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

Steps 12–14 are the *understanding* half and are independent of 7–11; a graph can be emitted with claims ungrounded, and the verdict tags simply do not appear.

`audit_graph_structure.py` is a separate measuring instrument, not part of the sequence — it reports how much of the real call graph a knowledge graph contains.

`lint_claims.py` guards the seam between extraction and grounding. Grounding grades a claim against code; if extraction has already widened the claim past what the docstring said, grounding correctly finds a counterexample and reports a defect the documentation never committed. Measured on wool: 3 of 63 `overstated` verdicts were manufactured that way. 10 flags across 813 claims is the baseline; a jump above it is a prompt regression.

---

## Grounding: two passes plus an adaptive tie-break

Blanket triple-passing wastes agents. Measured on wool, two independent passes agreed on **98.2%** of 813 claims; only 15 needed a third opinion, and every one of those landed on one of the two priors.

So: two passes, then `build_tiebreak_bundle.py` isolates only the disagreements, and one adjudicator resolves them with both priors in view. That is deliberate asymmetry — extraction passes are kept blind from each other because they want independent samples; adjudication wants the *best* answer, and the disagreement is itself evidence.

## Verdicts

`supported` · `overstated` · `contradicted` · `unverifiable`

Defined once in `prompts/_taxonomy.md`, which is included **byte-identical** in both agent prompts. `test_prompts.py` asserts that, because a drift between them means the adjudicator applies a different rule than the passes it adjudicates — on exactly the claims that were hardest.

`overstated` exists because the three-verdict taxonomy discarded its most actionable output: three independent passes all graded *"reports each dispatch outcome back"* as `supported` while noting, in free text, the case where it does not hold. It requires **two-sided evidence** — a citation that the mechanism exists and one showing where it stops — and a bound-less `overstated` is automatically downgraded to `supported` rather than to `unverifiable`, because losing the bound loses the qualification, not the support.

### The boundary takes two questions, not one

The first 813-claim run shipped with a single discriminator — *does anything survive the weakening?* — and it was **too permissive**, measured: 10 claims moved out of `contradicted` against a pre-registered budget of 4, and reading all 10, 7 should have stayed. Both passes agreed on every one, so it was the rule and not agent variance. Almost any false sentence narrows to something that survives.

The second question is the fix: **is the excluded case safe to walk into?** A reader who relies on the sentence and lands in the excluded case is either inconvenienced or wrong. Wrong → `contradicted`, however cleanly it narrows. Calibration examples F and G in `_taxonomy.md` are the pair that isolates it: they narrow by the same amount and differ only in what the reader does with the sentence.

`overstatement.readerImpact` is that question made structural, exactly as the bound made the first question structural. Its absence does **not** move the verdict — losing the bound loses the qualification, but a missing impact statement is evidence of nothing, and escalating on it would turn a forgotten field into a false bug report. Those rows are counted as `impactMissing` and routed to adjudication via `build_tiebreak_bundle.py --also-claims`.

---

## Invariants worth not breaking

- **The mirror is the only source agents read.** Pointing them at real source reintroduces circularity silently — the output still looks fine.
- **Evidence verification runs before consensus**, never after.
- **Quorum counts distinct pass indices**, never a sum. Summing lets one pass clear quorum alone by emitting two wordings of the same assertion.
- **Excluded and rejected claims are written out**, never dropped silently.
- **Claim IDs are content hashes** (`docUnitId` + normalized text). Re-deriving them on a changed corpus invalidates any paired before/after comparison.

## Running the tests

```bash
python3 test_prompts.py            # prompt/script taxonomy invariants
python3 test_prompts.py --sync     # rewrite the shared block into both prompts, then check
python3 lint_claims.py --bundles <DIR>   # extraction-artifact review queue
```

Edit the taxonomy in `prompts/_taxonomy.md` and run `--sync`; never hand-edit the copies inside the two prompts. The copies are what the agents read, and a hand-edit that lands in one of them is the exact drift `test_prompts.py` exists to catch.

Requires Python 3.10+ (`X | None` syntax throughout).
