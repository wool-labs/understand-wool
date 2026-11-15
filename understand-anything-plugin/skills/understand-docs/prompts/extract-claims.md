# Task: extract atomic claims from documentation

You are reading docstrings from a Python library and breaking each one into the individual assertions it makes about the code. You are **not** judging whether they are true — a later phase grounds every claim against source. Your only job is to isolate what the documentation actually asserts, and to word it the same way an independent reader of the same docstring would.

Several agents are running this identical task on the same input. Claims that fewer than three passes produce are dropped. **You are not trying to agree with them** — you are trying to be complete and to use the canonical wording, which is what makes agreement happen without collusion.

## Your input

`{INPUT}` — one bundle, a JSON object with:

- `subsystem` — the subsystem the bundle's doc units belong to; echo it back unchanged as `subsystem`, since the bundles of a large subsystem have different labels and share this value
- `label` — the bundle's name; echo it back as `bundle`
- `chunk` — `i/k`, this bundle's place among the subsystem's bundles
- `docUnits` — the doc units to extract from

Each doc unit carries:

- `id` — the `docunit:<hash>` you must echo back on every claim from it
- `sourceFile`, `sourceLine`, `docLineRange`
- `attachedTo` — `<kind>:<file>:<qualname>`, the symbol the docstring documents
- `attachedKind` — `module` | `class` | `function` | `method` | `attribute`
- `text` — the docstring, or one part of it, markup intact
- `fields` — parsed `param` / `returns` / `yields` / `raises` entries, in order
- `partIndex`, `partCount` — set when a long docstring is split into parts, each its own doc unit; `null` otherwise
- `context` — on every part after the first, the opening summary line of the whole docstring; `null` otherwise

**Use `context` for orientation only, and never extract a claim from it.** The first part holds that line in its `text` and extracts its claims; a claim taken from `context` is extracted once per part, and its `sourceQuote` is not in the `text` of the doc unit it names.

Do not read source code. Do not open the repository. A claim is what the *documentation* says; whether the code agrees is somebody else's phase, and peeking imports the code's answer into the question.

## What counts as a claim

One assertion, checkable in principle against code. Extract from prose *and* from fields — a `param` description asserting "must be picklable" is a claim.

Do not extract:

- pure restatement of the signature ("takes a `host` argument") with no content
- cross-references and links with no assertion of their own
- example blocks, unless the prose around them asserts something
- section headers, formatting, version markers

Each claim gets:

| field | values |
|---|---|
| `claimType` | `factual` (states what the code does) · `instructional` (states what a caller must do) · `aspirational` (states intent or a goal) · `illustrative` (explains by analogy or example) |
| `quantifier` | `universal` ("never", "every", "all", "always") · `existential` ("may raise", "can return") · `particular` (one specific thing) |
| `sourceQuote` | the **shortest verbatim span** of `text` the claim came from: one contiguous run of characters, copied exactly |
| `fieldIndex` | index into the unit's `fields` when the claim came from one; `null` for prose |

Only `factual` and `instructional` claims reach the grounding phase, but label all four — the excluded counts are reported, and silently dropping a type at extraction time hides how much of a corpus is aspiration.

`sourceQuote` is not decoration. Reconciliation merges two passes' wordings only when their quotes overlap, because lexical similarity alone systematically mis-merges the two halves of a disjunction — the very claims rule 6 tells you to split. A sloppy quote either blocks a real merge or permits a false one.

## Canonical form — six rules

Agreement between passes depends on the prompt, not the model. Without these rules, independent passes agree on *what* to extract and disagree on how to word it, and reconciliation collapses.

1. **Use the exact code identifier, never a prose paraphrase.** `WorkerProxy`, not "the proxy". `LoadBalancerContext.remove_worker`, not "the removal method". If the docstring says "this class", resolve it from `attachedTo`.
2. **No markup in `claimText`.** Strip reST roles, backticks, emphasis, links. `sourceQuote` keeps the markup; `claimText` does not.
3. **Declarative statements only.** Never address the reader. "Arguments must be picklable", not "make sure your arguments are picklable".
4. **Name the subject explicitly, at the front.** Every claim stands alone without its doc unit. "`WorkerPool.__aexit__` stops all workers", not "stops all workers".
5. **Omit only *subject-scope* qualifiers the doc unit already supplies.** If the docstring is attached to `WorkerProxy` and says "in this class", drop it; rule 4 has already put the subject in the sentence. **Never drop a qualifier that narrows the assertion itself** — `when`, `if`, `only`, `unless`, `via`, `except`, `after`. Those change what is being claimed, and dropping one manufactures an overstatement that the documentation never made. A claim whose truth depends on a qualifier must carry that qualifier in `claimText` *and* inside `sourceQuote`.
   > *"Raised when the generator ends via `athrow` or the initial `anext`"* extracts as a claim about **that** termination path. Extracted as *"...when the generator ends"* it becomes a claim the docstring did not make, and grounding correctly finds a counterexample — in our own wording.
6. **Split coordination only where each half is a complete assertion.** Split *"Starts the worker and registers it with the pool"* into two claims: both halves share the sentence's subject and each has its own verb. Do **not**:
   - **promote a member of a coordinated noun phrase into a predicate.** *"Contains the information necessary for routing, and result handling"* yields one claim about what the object contains — not a separate claim that it "contains the information necessary for result handling", which asserts something the sentence never separates.
   - **re-attach a verb to a subject the sentence does not give it.** *"...so the pool can register it"* does not license *"`start` registers the worker with the pool"*. Keep the subject the sentence assigns.

   When in doubt, do not split. An unsplit claim is coarse; a mis-split claim is an assertion nobody wrote, and it will be graded against code as though someone had.

## Output

Write JSON to `{OUTPUT}`.

```json
{
  "bundle": "<label from your input>",
  "subsystem": "<subsystem from your input>",
  "pass": <pass number from your dispatch prompt>,
  "claims": [
    {
      "docUnitId": "docunit:...",
      "claimText": "WorkerPool.__aexit__ stops all workers.",
      "claimType": "factual",
      "quantifier": "universal",
      "sourceQuote": "Stop all workers",
      "fieldIndex": null
    }
  ]
}
```

Rules:

- Every `docUnitId` must be one from the input, copied exactly.
- `sourceQuote` must appear **verbatim** in that unit's `text`. It is checked, by a script, as one contiguous span: same words, same case, same markup, with only line breaks and runs of spaces treated as equal. Never join two parts of a sentence with `...`, and never fix or re-case the text. When a qualifier the claim depends on sits far from the verb, quote the longer span that contains both. A pass with one quote that fails the check is discarded and re-run.
- Do not emit `claimId` — it is a content hash derived downstream, and inventing one breaks the paired comparisons every measurement in this pipeline depends on.

## Before you write

1. Each claim reads as a standalone sentence with an explicit subject.
2. Each `sourceQuote` is one contiguous span copied exactly from its doc unit's `text`, with no `...` in it that the docstring does not have.
3. No claim drops a `when` / `only` / `unless` / `via` its truth depends on.
4. No claim asserts something the sentence coordinates but does not separate.
5. No `claimText` contains backticks, roles, or emphasis markup.
6. `bundle` and `subsystem` are copied from your input.
