# Task: ground documentation claims against source code

You are verifying claims extracted from docstrings in a Python library. For each claim, decide whether the **code** supports it, contradicts it, or neither.

This is an independent verification pass. Other agents are doing the identical task on the same input; your job is to be correct, not to match them.

## The one rule that matters

**Evidence must be executable code. Never prose.**

The claims you are checking were themselves extracted from docstrings. If you cite a docstring, a comment, or a type-stub signature as proof, you have proved nothing — you have quoted the claim back to yourself.

To make this structural rather than a matter of discipline, **read source only from the stripped mirror**, where every docstring is replaced by `"""<docstring removed>"""`:

```
{MIRROR}
```

Line numbers in the mirror are **identical** to the real files, so cite them directly. Do not read the real source tree. If you find yourself wanting to, that is the signal the claim is not groundable in code — a legitimate verdict.

Comments *are* present and may be used for navigation, but a comment is not evidence. Evidence is a statement that executes.

## Your input

`{INPUT}` — doc units and their claims. Each unit carries a `groundingTarget`:

- **`direct`** — the documented symbol has a real body. Verify against it.
- **`implementor`** — the symbol is a `Protocol` or a `...` stub. **There is no code at the declaration site.** The docstring states a contract; the behaviour lives in implementors and callers, and you must go find them.

  `candidateSites` is a *starting point and is neither complete nor authoritative* — Python protocols are structural, so an implementor need not reference the protocol at all. Search by method name, by call site, by duck-typed usage. If after real effort you find no implementing code, say so: verdict `unverifiable`, and set `searchedFor`.

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


## Output

Write JSON to `{OUTPUT}`. Exactly one entry per claim — do not skip, merge, or add.

```json
{
  "bundle": "<bundle filename>",
  "pass": <pass number from your dispatch prompt>,
  "verdicts": [
    {
      "claimId": "claim:...",
      "verdict": "supported | contradicted | unverifiable",
      "confidence": "high | medium | low",
      "evidence": [
        {"file": "wool/runtime/loadbalancer/roundrobin.py", "lines": [88, 94],
         "code": "<verbatim executable lines from the mirror>",
         "role": "supports | limits | contradicts"}
      ],
      "scopeNote": {"asSupported": "..."},
      "reasoning": "<2-3 sentences: what the code does, and why that settles it>",
      "scopeChecked": "<for universal claims: what you actually checked; else null>",
      "searchedFor": "<for implementor units where you found nothing; else null>"
    }
  ]
}
```

Rules:

- `evidence` **must be non-empty** for `supported` and `contradicted`. It may be empty for `unverifiable`.
- `scopeNote` is optional and belongs only on a `supported` verdict. It requires at least one evidence item with `role: "limits"`; without a verified limiting citation the note is dropped, so a narrowing you cannot cite simply loses the finding.
- `code` must be copied verbatim from the mirror, and `lines` must be the real line numbers. These are machine-checked; invented citations are caught and discarded.
- Keep `reasoning` to 2–3 sentences. A human triaging results reads it.

## Before you write

1. Every `claimId` in the bundle appears exactly once.
2. Every `supported`/`contradicted` verdict has at least one evidence item.
3. Every `scopeNote` has a `limits` item and an `asSupported` strictly weaker than the claim. Re-read each one and ask whether a reader relying on the claim as written would be merely bounded or actually wrong. If wrong, the verdict is `contradicted`, not a scope note.
4. Every evidence `code` string actually appears at those `lines` in that mirror file — re-read a sample.
5. No evidence quotes a `<docstring removed>` marker or a bare `...` stub body.

Fix anything that fails, then write the file.
