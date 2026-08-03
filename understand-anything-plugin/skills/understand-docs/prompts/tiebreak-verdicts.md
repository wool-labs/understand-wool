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
to judge. Keep them byte-identical; `test_prompts.py` asserts it, and
`test_prompts.py --sync` regenerates them from this file.
-->

## Verdicts

Exactly one per claim. Decide by asking a question about **repair**:

> To make the documentation and the code agree, what is the smallest change, and does it fall on the doc or on the code?

| verdict | minimal repair |
|---|---|
| `supported` | none — the sentence is true of the code |
| `contradicted` | **change the code**, or delete/reverse the sentence — the two genuinely disagree |
| `unverifiable` | no repair is determinable — no executable code bears on it |

Three verdicts, and there is no fourth. A sentence that is true but narrower than it sounds is **`supported`** — record the narrowing as a scope note (below) rather than reaching for a softer verdict. There was once an `overstated` verdict for exactly that case; it was withdrawn after measurement, because independent graders reproduced `contradicted` 14 times in 14 and `overstated` only 17 times in 31. Its evidence held up perfectly; only the label was unstable. So the evidence is what you record.

`unverifiable` is a real answer, not a cop-out — correct for claims about intent, about what *another docstring* says, about CPython/stdlib/gRPC semantics, or about prior releases. A wrong `supported` is far more damaging than an honest `unverifiable`.

### Scope notes

When a claim is true **and** you have located, in executable code, the case where it stops being true, record that:

```json
"scopeNote": {"asSupported": "<the narrowed form the code actually supports>"}
```

and cite the boundary with `role: "limits"` in your evidence. Both halves are required — a scope note without a verified limiting citation is dropped, because an unsupported narrowing is an opinion.

The verdict stays `supported`. You are not hedging it; you are telling a reader where the guarantee ends, which is a different and more useful thing than doubting whether it exists.

**A scope you did not check is not a boundary you found.** If you checked one path and it agreed, that is `supported` with `confidence: "low"` and a note in `scopeChecked` — no scope note. A scope note requires that you *located the excluded case and cited it*.

### Contradiction requires a real defect

Report `contradicted` only where the code genuinely conflicts. Do **not** report one for:

- a claim that is vaguer than the code but not wrong
- a claim that is true within a narrower range than it implies — that is `supported` with a scope note
- an omission — the code doing *more* than documented is not a contradiction
- naming or phrasing you would have chosen differently

A contradiction is a bug someone should fix. Hold it to that bar.

The one thing that does make a narrow claim `contradicted`: when following the sentence as written leads a reader to a **wrong action** — code that breaks, a state mis-diagnosed, a guarantee relied on that is not there. *"`WorkerProcess.port` returns `None` if not started"* is narrow-but-true only if you ignore that `port is None` is exactly how a reader tests whether the process is up, and with a fixed port that test silently reports the wrong state. Narrowness is a scope note; misleading is a contradiction.

### Quantifiers are not symmetric

| quantifier | rule |
|---|---|
| `universal` ("never", "every", "all") | Enumerate the domain the quantifier ranges over and look for counterexamples. **None** → `supported`. **Found, and the mechanism still holds on the nominal path** → `supported` with a scope note citing the counterexample. **Counterexamples cover the nominal path, or the mechanism is absent or reversed** → `contradicted`. |
| `existential` ("may raise", "can return") | One witness supports. Refuting needs exhaustive absence, which is usually out of reach — prefer `unverifiable` over a weakly-argued `contradicted`. |
| `particular` | Check that one thing. |

For `instructional` claims ("arguments must be picklable"), judge against **what the code requires in order to function**, not against what it enforces. An unenforced requirement is not a defect, or every `must` in a corpus becomes one.

### Intent is irrelevant

> A narrowing that is deliberate, correct, and explained in an inline comment is still a narrowing the docstring does not carry. You are grading the sentence against the code, not the code against your judgement.

### Calibration

These are real cases, with the verdict each should receive.

**A — `supported`, with a scope note.** *"WorkerProxy reports each dispatch outcome back to the balancer's generator."* (universal)
- supports: `proxy.py:1044` `trailing = await generator.asend(uid)`; `proxy.py:1030` `uid = await generator.athrow(exc)`
- limits: `proxy.py:1023` `except RpcError as exc:` — the sole except clause; a `TimeoutError` from `connection.dispatch` reaches the outer `finally` at `proxy.py:1066` and is never reported
- `asSupported`: *reports each **RPC** dispatch outcome*
- Not `contradicted`: reporting covers success and RPC failure — the nominal paths. A balancer author loses a signal on the excluded path, never receives a wrong one.
- An inline comment says the narrowing is deliberate. **Irrelevant** — the docstring still does not carry it.

**B — `supported`, with a scope note.** *"RoundRobinLoadBalancer advances the index after each yielded candidate."* (universal)
- supports: `roundrobin.py:97` `self._index[context] = index + 1`, unconditional per iteration
- limits: that same line precedes `yield uid`, and iterations that `continue` on an evicted candidate advance without yielding at all
- `asSupported`: *advances the index before each **selected** candidate*
- The invariant a reader depends on — one advance per candidate — holds exactly.

**C — stays `supported`, no scope note.** *"Only RpcError is treated as a worker-health signal."* One except clause, exhaustively checkable, nothing to narrow. Code doing *more* than documented is not a boundary.

**D — `contradicted`.** *"A uid that has left the pool can never recur."* The universal *is* the docstring's stated rationale for reseeding the cycle boundary, and it is false. Narrowing `never` to `rarely` leaves a sentence both untrue and useless — there is no boundary to record, only a defect.

**E — `contradicted`, despite being narrowly true.** *"`WorkerProcess.port` returns `None` if the process is not started."* It does, when the port was left at `0`. But a `WorkerProcess` constructed with a fixed port returns that port before starting, and `port is None` is how a reader will test whether it is up. The excluded case produces a confident wrong answer, not a lost signal. Compare with A: same shape, opposite verdict, and the difference is what the reader does with the sentence.
<!-- END SHARED TAXONOMY -->


## Resolving the disagreement

Your input's `disagreementShapes` tells you which boundaries are actually in dispute. Guidance per shape:

| shape | how to resolve |
|---|---|
| `supported vs unverifiable` | Usually a dispute about stdlib dependence. If the project's own code determines the outcome and the stdlib is merely the substrate, that is `supported`. If the claim's truth turns on a guarantee the project neither implements nor enforces, `unverifiable`. |
| anything involving `contradicted` | The consequential ones. Ask specifically: **is there a concrete input or state where a reader following the docstring would be wrong?** |

**There is no middle value to reach for.** An adjudicator shown two priors will look for something between them; the taxonomy deliberately offers none. If the claim is true within a narrower range than it sounds, that is `supported` with a scope note — a record of where it stops, not a softened verdict. A verdict for exactly that case existed and was withdrawn because independent graders could not reproduce it.

## Input

`{INPUT}` — each entry has `claimId`, `claimText`, `quantifier`, `sourceFile`, `qualname`, and `priorVerdicts` (both passes, with reasoning and evidence). The file also carries `claimCount` and `disagreementShapes`.

## Output

Write JSON to `{OUTPUT}`:

```json
{
  "verdicts": [
    {
      "claimId": "claim:...",
      "verdict": "supported | contradicted | unverifiable",
      "confidence": "high | medium | low",
      "agreesWith": "pass name from priorVerdicts, or null if you reject both",
      "evidence": [
        {"file": "wool/runtime/worker/proxy.py", "lines": [93, 93],
         "code": "<verbatim from the mirror>", "role": "supports | limits | contradicts"}
      ],
      "scopeNote": {"asSupported": "..."},
      "reasoning": "<2-3 sentences: what the code does, and why it settles the dispute>",
      "whyTheOtherPassWasWrong": "<1 sentence>"
    }
  ]
}
```

Rules:

- One entry per claim in the input — cover every one.
- `supported`/`contradicted` require non-empty evidence; `unverifiable` may have none.
- `scopeNote` belongs only on a `supported` verdict and requires a `role: "limits"` item, or it is dropped. You are the last stop: a boundary you do not cite here is not recorded anywhere.
- `code` must be verbatim from the mirror at the cited `lines`; these are machine-checked.
- You may reject both prior verdicts (`agreesWith: null`) if both are wrong. Say so plainly.

Verify before writing: every claim id present exactly once, evidence re-read from the mirror at the stated lines, nothing quoting a `<docstring removed>` marker.

Your final message: a compact table of claimId → verdict → which pass you sided with, plus any claim where you rejected both.
