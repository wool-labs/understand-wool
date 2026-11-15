# Task: extract design relations and candidate principles from documentation claims

You are given claims extracted from the docstrings of a Python codebase, grouped into blocks. A block is a set of claims that mention the same code symbol, or a rationale claim together with the claims it most plausibly explains.

Your job is **not** to check whether claims are true; that was done separately. Your job is to recover **why the system is shaped the way it is**. Produce two things: edges between claims, and candidate design principles.

## Your input

`{INPUT}` — one bundle, a JSON object with:

- `bundle` — the bundle number; echo it back
- `blocks` — each with `blockId`, `kind` (`symbol` or `rationale`), `claimIds`, and `roleMix`. A `symbol` block names its `symbol`; a `rationale` block names its `anchorClaimId` and `anchorText`, the claim the others may explain.
- `claims` — keyed by claim id, each with `claimText`, `docUnitId`, `roles`, `subsystem`, `symbols`, `quantifier`, `passCount`, and `grounding`. Claims with one `docUnitId` come from one docstring.

`roles` are heuristic tags (`rationale`, `responsibility`, `invariant`, `lifecycle`, `omission`, `mechanism`). Treat them as hints: when a claim tagged `mechanism` is plainly a design commitment, use it.

`grounding` is `null`, or the verdict a grader reached against the code, with evidence. A claim graded `supported` is stronger evidence for a principle than an ungraded one. A claim graded `contradicted` must **not** support a principle; when one appears where it would, say so in your final message.

You may read the source at `{MIRROR}`, where every docstring is replaced by `<docstring removed>`, to check what a symbol does. You are not required to. Do not read the original source.

## Part 1 — Design edges

Emit an edge only where the relationship is evident in the claim texts themselves. Do not invent a connection you find plausible but cannot point at. Each edge runs `from` claim A `to` claim B.

| edge | when to use it |
|---|---|
| `justifies` | A gives the reason for B. "Coupling selection with dispatch forces every balancer to reimplement retry boilerplate" justifies "DispatchingLoadBalancerLike is deprecated". |
| `refines` | A is a strictly more specific case of B. |
| `enables` | A is a mechanism, and B a separate property or guarantee that A's mechanism makes possible. "The proxy retries on a fresh worker" enables "a dispatch survives one worker's death". |
| `realizes` | B states a design commitment, and A is the code-level mechanism that carries out that same commitment. "The balancer only yields candidates; the proxy performs the dispatch" realizes "selection policy is separated from dispatch". |
| `exemplifies` | B makes a general statement, and A is one case it covers, without being the mechanism behind it. "The discovery cache is rebuilt on every subscription" exemplifies "no component trusts cached membership". |
| `owns` | A assigns responsibility for the concern B describes to a named component. "Eviction is WorkerProxy's responsibility." |
| `delegates-to` | A says one component hands the concern B describes to another component. |
| `tensions-with` | A and B pull in different directions without being contradictory: a trade-off the design makes. |
| `contradicts` | A and B cannot both hold. |

`tensions-with` is under-used and matters most: a design's philosophy is most visible where it trades one property for another, such as bounded teardown against guaranteed cleanup.

**A contradiction is one `contradicts` edge per pair of claims, in either order.** Do not order the pair by which claim seems newer, and never write a `supersedes` edge or any type not in the table: the emitter drops an edge whose type it does not know, and the contradiction would vanish from the graph.

## Part 2 — Candidate principles

A principle is a design commitment that shows up in more than one place. Name it as an assertion about the design, not as a topic.

- Good: `Policy is separated from mechanism at every seam`
- Good: `Resource lifetime is owned by the layer that pools it, never by the handle holder`
- Bad: `Load balancing` (a topic, not a commitment)
- Bad: `The code is well structured` (vacuous)

For each principle give:

- `statement` — one sentence, the commitment itself
- `realizedBy` — claim ids of the claims that show the commitment in effect, **two or more**, ideally from different symbols or subsystems
- `justifiedBy` — claim ids giving the reason, where present (often a claim with the `rationale` role)
- `tension` — what the principle costs, if the claims show a cost; otherwise `null`
- `scope` — the subsystems it is visible in, taken from the claims' `subsystem` fields
- `confidence` — `high` if realized by three or more claims across two or more symbols; `medium` if realized by two or more claims from two or more doc units; `low` if every claim realizing it has one `docUnitId`

Be conservative. One docstring saying something once is a fact, not a principle: mark a candidate whose claims all share one `docUnitId` `low`, or leave it out. Three well-supported principles are worth more than twelve speculative ones. A principle that says what one claim says is that claim, not a principle.

## Output

Write JSON to `{OUTPUT}`:

```json
{
  "bundle": 1,
  "edges": [
    {"from": "claim:1f…", "to": "claim:8c…", "type": "justifies",
     "rationale": "<one sentence: why this edge holds>"}
  ],
  "principles": [
    {"statement": "...", "realizedBy": ["claim:1f…", "claim:a2…"],
     "justifiedBy": ["claim:8c…"], "tension": null,
     "scope": ["load-balancing"], "confidence": "medium"}
  ]
}
```

The emitter reads exactly these fields. It drops, and counts on its `dropped:` line, every edge whose `type` is not in the table above, every edge whose `from` or `to` is not a claim it holds, and every principle with fewer than two `realizedBy` claims it holds.

## Before you write

1. Every claim id you cite is a key of your bundle's `claims`, copied exactly.
2. Every edge `type` is one of the nine in the table.
3. Every principle has two or more `realizedBy` ids and is not a restatement of one claim.
4. No principle is supported by a claim graded `contradicted`.

Fix what fails, then write. Your final message is a short summary: how many edges and principles, the strongest principle you found, and anything about the input that looked wrong.
