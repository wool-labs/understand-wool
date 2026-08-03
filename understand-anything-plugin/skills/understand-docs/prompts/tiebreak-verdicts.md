# Task: adjudicate disputed grounding verdicts

Two independent agents graded each of these claims against the code and reached **different verdicts**. Decide which is right — or reject both.

This is deliberately *not* a blind third pass. You are given both prior verdicts and their reasoning, because the disagreement itself is evidence about where the difficulty lies.

## Read source from the stripped mirror

```
{MIRROR}
```

Every docstring there is replaced by `"""<docstring removed>"""`; line numbers are identical to the real files. **Evidence must be executable code — never prose.** These claims were extracted *from* docstrings, so citing one proves nothing. Comments are fine for navigation but are not evidence.

<!-- BEGIN SHARED TAXONOMY -->
<!--
SHARED TAXONOMY BLOCK.

This section is included verbatim in BOTH `ground-claims.md` and
`tiebreak-verdicts.md`. If the two drift, the adjudicator applies a different
rule than the passes it adjudicates — on precisely the claims that were hardest
to judge. Keep them byte-identical; `test_prompts.py` asserts it.
-->

## Verdicts

Exactly one per claim. Decide by asking a question about **repair**:

> To make the documentation and the code agree, what is the smallest change, and does it fall on the doc or on the code?

| verdict | minimal repair |
|---|---|
| `supported` | none — the sentence is already true across the range its quantifier covers |
| `overstated` | **weaken the sentence.** Add a qualifier, narrow the quantifier, or scope the subject. The code does not change, and a real mechanism survives the weakening |
| `contradicted` | **change the code**, or delete/reverse the sentence. No weakening saves it, or the weakening leaves nothing |
| `unverifiable` | no repair is determinable — no executable code bears on it |

The discriminator between `overstated` and `contradicted` has **two** questions, and both must pass for `overstated`.

**1. Does anything survive the weakening?** If the narrowed claim still says something true and useful about a mechanism that exists, this question passes. If the narrowed claim is vacuous, or asserts the opposite of what the docstring asserts, it is `contradicted`.

**2. Is the excluded case safe to walk into?** A weakening is only a weakening if it is. Ask: *a reader who relies on this sentence as written and lands in the excluded case — are they inconvenienced, or are they wrong?* If following the sentence makes them write incorrect code, mis-diagnose a state, or rely on a guarantee that is not there, the sentence is `contradicted` however cleanly it narrows.

Question 1 alone is too permissive: almost any false sentence narrows to something that survives. *"`WorkerProcess.port` returns `None` if not started"* narrows cleanly to *"...and no fixed port was requested"*, and a mechanism survives — but a reader uses `port is None` as a started-check, and with a fixed port that check silently reports the wrong state. Question 2 is what separates a sentence that is merely imprecise from one that misleads.

`unverifiable` is a real answer, not a cop-out — correct for claims about intent, about what *another docstring* says, about CPython/stdlib/gRPC semantics, or about prior releases. A wrong `supported` is far more damaging than an honest `unverifiable`.

### `overstated` requires two-sided evidence

This is a structural requirement, not a matter of judgement:

1. at least one verified citation showing **the mechanism exists** (`role: "supports"`), and
2. at least one verified citation showing **where the mechanism stops** (`role: "limits"`).

If you cannot cite the bound in executable code, you do not have `overstated` — you have `supported` with an unchecked scope. A bound-less `overstated` is automatically downgraded to `supported` by the reconciler, so asserting one without locating the limit simply loses your finding.

You must also supply `overstatement: {asWritten, asSupported, readerImpact}`:

- `asWritten` — the claim as written
- `asSupported` — the narrowed form the code actually supports, strictly weaker than `asWritten`
- `readerImpact` — **the harm test, written out.** What does a reader who relies on `asWritten` actually do, what happens when they land in the excluded case, and why is that still safe? One or two sentences.

`readerImpact` is a required field for the same reason the bound is: it makes question 2 impossible to skip silently. If you cannot finish that sentence — if the honest answer is "they would be wrong" — you have found a `contradicted`, not an `overstated`. A missing `readerImpact` does not change your verdict automatically; it sends the claim to adjudication, which is slower and wastes the work you already did.

### Intent is irrelevant

> `overstated` makes no claim that the code is wrong. A narrowing that is deliberate, correct, and explained in an inline comment **still overstates** if the docstring does not carry the narrowing. You are grading the sentence against the code, not the code against your judgement.

### Quantifiers are not symmetric

| quantifier | rule |
|---|---|
| `universal` ("never", "every", "all") | Enumerate the domain the quantifier ranges over and look for counterexamples. **None** → `supported`. **Found, but the mechanism holds on the nominal path** → `overstated`, and cite the counterexample. **Counterexamples cover the nominal path, or the mechanism is absent/reversed** → `contradicted`. |
| `existential` ("may raise", "can return") | One witness supports. Refuting needs exhaustive absence, which is usually out of reach — prefer `unverifiable` over a weakly-argued `contradicted`. `overstated` is rare here: only when a claimed capability exists in a materially weaker form than stated. |
| `particular` | Check that one thing. `overstated` applies when the claim carries a qualifier the code does not support. |

**A scope you did not check is not a shortfall you found.** If you checked one path and it agreed, that is `supported` with `confidence: "low"` and a note in `scopeChecked` — exactly as before. `overstated` requires that you *located the excluded case and cited it*.

For `instructional` claims ("arguments must be picklable"), judge against **what the code requires in order to function**, not against what it enforces. An unenforced requirement is not an overstatement, or every `must` in a corpus becomes one.

### Contradiction requires a real defect

Report `contradicted` only where the code genuinely conflicts. Do **not** report one for:

- a claim that is vaguer than the code but not wrong
- an omission — the code doing *more* than documented is not a contradiction
- naming or phrasing you would have chosen differently

A contradiction is a bug someone should fix. Hold it to that bar. Where a claim promises more than the code delivers but the mechanism is real, that is `overstated`, not `contradicted`.

### Calibration

These are real cases, with the verdict each should receive.

**A — `overstated`.** *"WorkerProxy reports each dispatch outcome back to the balancer's generator."* (universal)
- supports: `proxy.py:1044` `trailing = await generator.asend(uid)`; `proxy.py:1030` `uid = await generator.athrow(exc)`
- limits: `proxy.py:1023` `except RpcError as exc:` — the sole except clause; a `TimeoutError` from `connection.dispatch` reaches the outer `finally` at `proxy.py:1066` and is never reported
- minimal repair: `each dispatch outcome` → `each **RPC** dispatch outcome`. Code unchanged.
- Not `contradicted`: reporting covers success and RPC failure — the nominal paths.
- readerImpact: a balancer author writes a generator expecting a send or throw per dispatch. On a `TimeoutError` it is never resumed and is closed by the outer `finally` — it loses a signal, but never receives a wrong one, and the close path is the same one it already handles.
- An inline comment says the narrowing is deliberate. **Irrelevant.**

**B — `overstated`.** *"RoundRobinLoadBalancer advances the index after each yielded candidate."* (universal)
- supports: `roundrobin.py:97` `self._index[context] = index + 1`, unconditional per iteration
- limits: that same line precedes `yield uid`, and iterations that `continue` on an evicted candidate advance without yielding at all
- minimal repair: `after each yielded candidate` → `before each **selected** candidate`
- Not `contradicted`: the invariant a reader depends on — one advance per candidate — holds exactly.
- readerImpact: a reader reasons about fairness across candidates and gets the right answer; the before/after distinction is not observable from outside the generator.

**C — `overstated`.** *"The record a balancer read is never the record dispatched through."* (universal)
- supports: `proxy.py:1010` `current = ctx.workers.get(uid)` — the proxy re-reads rather than trusting the balancer
- limits: that re-read returns the *same tuple object* when membership has not changed, so `never` falls to one counterexample
- minimal repair: `is never` → `is not guaranteed to be`
- readerImpact: the sentence tells a reader not to trust the record the balancer saw. Acting on that is correct in every case; the excluded case only means their re-read sometimes hands back the same object, which costs nothing.
- A universal that is true only under a charitable re-reading is `overstated`; the re-reading *is* the weakening.

**D — stays `supported`.** *"Only RpcError is treated as a worker-health signal."* One except clause, exhaustively checkable, no weakening needed. Code doing *more* than documented is not overstatement — `overstated` is strictly the reverse direction.

**E — stays `contradicted`.** *"A uid that has left the pool can never recur."* Weakening `never` to `rarely` leaves a sentence both untrue and useless, and the universal *is* the docstring's stated rationale for reseeding the cycle boundary. Nothing survives — **question 1 fails.**

**F — `contradicted`, not `overstated`.** *"`WorkerProcess.port` returns `None` if the process is not started."* (particular)
- supports: the attribute does return `None` before `start()` when the port was left at `0`
- limits: a `WorkerProcess` constructed with a fixed port returns that port before it has started anything
- The narrowing is clean — `...and no fixed port was requested` — so **question 1 passes**. **Question 2 fails**: `port is None` is exactly how a reader will test whether the process is up, and on the excluded path that test reports the wrong state with no error. The reader does not lose a signal; they get a confident wrong one.
- This is the shape to watch for: *a claim that reads as a state predicate*. If a reader would branch on it, the excluded case is never safe.

**G — stays `overstated`.** *"`Task` is single-use as a context manager."* (particular)
- supports: `__enter__` raises when the instance is already active in a `with` block
- limits: `__exit__` clears the guard, so the same instance can be entered again afterwards
- **question 1** passes — the re-entrancy guard is real. **question 2** passes: a reader who believes the sentence constructs a fresh `Task` per use, which costs one unnecessary object and is otherwise correct. Inconvenience, not error.
- F and G narrow by the same amount. The verdicts differ only on what the reader *does* with the sentence.

### Calibration prior

If you are marking more than roughly **one claim in five** `overstated`, you are applying it to vagueness rather than to demonstrated shortfall. A claim vaguer than the code is `supported`.
<!-- END SHARED TAXONOMY -->


## Resolving the disagreement

Your input's `disagreementShapes` tells you which boundaries are actually in dispute. Guidance per shape:

| shape | how to resolve |
|---|---|
| `supported vs overstated` | Did the dissenting pass **cite a bound**, or merely note unchecked scope? A cited limit makes it `overstated`; an unchecked scope leaves it `supported`. |
| `overstated vs contradicted` | Apply **both** discriminator questions. Something must survive the weakening *and* the excluded case must be safe to walk into. A pass that argued `overstated` without answering the second question has answered half of it. |
| `supported vs unverifiable` | Usually a dispute about stdlib dependence. If the project's own code determines the outcome and the stdlib is merely the substrate, that is `supported`. If the claim's truth turns on a guarantee the project neither implements nor enforces, `unverifiable`. |
| `overstated vs unverifiable` | Should be rare. Usually means one pass found no groundable code at all, which `overstated` cannot coexist with. |
| anything involving `contradicted` | The consequential ones. Ask specifically: **is there a concrete input or state where a reader following the docstring would be wrong?** |

**`overstated` is not a compromise between `supported` and `contradicted`.** Choosing it requires its own citation of a bound, exactly as a first-pass grader would. An adjudicator shown two priors and offered a middle value will reach for it; do not.

## Input

`{INPUT}` — each entry has `claimId`, `claimText`, `quantifier`, `sourceFile`, `qualname`, and `priorVerdicts` (both passes, with reasoning and evidence). The file also carries `claimCount` and `disagreementShapes`.

## Output

Write JSON to `{OUTPUT}`:

```json
{
  "verdicts": [
    {
      "claimId": "claim:...",
      "verdict": "supported | overstated | contradicted | unverifiable",
      "confidence": "high | medium | low",
      "agreesWith": "pass name from priorVerdicts, or null if you reject both",
      "evidence": [
        {"file": "wool/runtime/worker/proxy.py", "lines": [93, 93],
         "code": "<verbatim from the mirror>", "role": "supports | limits | contradicts"}
      ],
      "overstatement": {"asWritten": "...", "asSupported": "...", "readerImpact": "..."},
      "reasoning": "<2-3 sentences: what the code does, and why it settles the dispute>",
      "whyTheOtherPassWasWrong": "<1 sentence>"
    }
  ]
}
```

Rules:

- One entry per claim in the input — cover every one.
- `supported`/`overstated`/`contradicted` require non-empty evidence; `unverifiable` may have none.
- `overstated` additionally requires a `role: "limits"` item and an `overstatement` object, or it is downgraded to `supported`. The `overstatement` must carry `readerImpact` as well as `asWritten`/`asSupported` — you are the last stop, and an `overstated` adjudicated without a `readerImpact` has nowhere further to be sent.
- `code` must be verbatim from the mirror at the cited `lines`; these are machine-checked.
- You may reject both prior verdicts (`agreesWith: null`) if both are wrong. Say so plainly.

Verify before writing: every claim id present exactly once, evidence re-read from the mirror at the stated lines, nothing quoting a `<docstring removed>` marker.

Your final message: a compact table of claimId → verdict → which pass you sided with, plus any claim where you rejected both.
