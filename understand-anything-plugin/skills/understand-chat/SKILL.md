---
name: understand-chat
description: Use when you need to ask questions about a codebase or understand code using a knowledge graph
argument-hint: "[query]"
---

# /understand-chat

Answer questions about this codebase using the knowledge graph in the project's data directory (`.ua/knowledge-graph.json`, or the legacy `.understand-anything/knowledge-graph.json` when that directory is present).

## Graph Structure Reference

The knowledge graph JSON has this structure:
- `project` — {name, description, languages, frameworks, analyzedAt, gitCommitHash}
- `nodes[]` — each has {id, type, name, filePath?, summary, tags[], complexity, languageNotes?}
  - Code node types: file, function, class, module, concept
  - Non-code node types: config, document, service, table, endpoint, pipeline, schema, resource
  - Domain/knowledge node types: domain, flow, step, article, entity, topic, claim, source
  - IDs use the node type as prefix, e.g. `file:path`, `function:path:name`, `config:path`, `article:path`
- `edges[]` — each has {source, target, type, direction, weight}
  - Key types: imports, contains, calls, depends_on, configures, documents, deploys, triggers, contains_flow, flow_step, related, cites
- `layers[]` — each has {id, name, description, nodeIds[]}
- `tour[]` — each has {order, title, description, nodeIds[]}

## Reading Claim Nodes

A `claim` node states something the documentation asserts about the code. The rules in this section apply to claim nodes carrying `emittedBy: "understand-docs"`. A claim node without that field comes from another producer or an older run: its `name` may be a short title, and its grounding status is **not recorded**. Say so; do not report it as checked, unchecked, or inconclusive.

`groundingVerdict` says whether the assertion was checked against the code:

| `groundingVerdict` | meaning |
|---|---|
| `supported` | checked; the code does what the claim says |
| `contradicted` | checked; the code does otherwise |
| `unverifiable` | checked; no executable code bears on it |
| `ungrounded` | **never checked** |
| `mixed` | the claim is stated in several places and their verdicts differ; read `groundingOccurrences` |

- Read `groundingVerdict` for a node's status. Do not infer status from the words `unverified` or `ungrounded` in `tags`: the tag `unverified` marks an `unverifiable` claim, which was examined.
- Report an `ungrounded` claim as unchecked, never as inconclusive.
- **To list every claim with a given status, match the node-level field or any entry of `groundingOccurrences`.** A `mixed` node hides its occurrences' verdicts from a node-level query. The `DRIFT` tag is safe to search: it is on every node with at least one `contradicted` occurrence. For example, every claim the code has drifted from:

  ```bash
  jq '.nodes[] | select(.type=="claim")
    | select(.groundingVerdict=="contradicted"
             or any(.groundingOccurrences[]?; .groundingVerdict=="contradicted"))
    | {id, name, groundingVerdict, groundingOccurrences}' "$UA_DIR/knowledge-graph.json"
  ```

- When a node has `groundingOccurrences`, each entry's `groundingVerdict`, `groundingAgreement`, and `scopeNote` hold only for that entry's occurrence. `attachedTo` names its symbol; `filePath` and `lineRange` locate its docstring. Answer per occurrence and name the symbol. The node-level `filePath` and `lineRange` belong to the first entry.
- On any node with `groundingOccurrences`, a missing node-level `groundingAgreement` or `scopeNote` means the value differs per occurrence, not that there is none. Read the entries.
- `groundingAgreement` qualifies the verdict. `split` means the graders disagreed and no tie-break has run: the verdict shown is a placeholder, and the claim is disputed. `majority` means only one grader returned a verdict: it is uncorroborated. `missing` means a grader was asked and returned nothing. Say which applies.
- A `scopeNote` gives the narrower form of the claim that the code supports. Quote its `asSupported` text when you cite the claim, and do not report a scoped claim as plainly supported.
- `name` is the full claim text. Cite a claim by its `id`.

## How to Read Efficiently

1. Use Grep to search within the JSON for relevant entries BEFORE reading the full file
2. Only read sections you need — don't dump the entire graph into context
3. Node names and summaries are the most useful fields for understanding
4. Edges tell you how components connect — follow imports and calls for dependency chains

## Instructions

1. **Resolve the data directory `$UA_DIR`.** Run `UA_DIR=$([ -d .understand-anything ] && echo .understand-anything || echo .ua)` — this is the legacy `.understand-anything/` when it already exists, otherwise the new `.ua/`. Check that `$UA_DIR/knowledge-graph.json` exists in the current project root. If not, tell the user to run `/understand` first.

2. **Check graph freshness before using graph-derived context**:
   - Read `project.gitCommitHash` from the graph metadata as `GRAPH_COMMIT_RAW`. Resolve it as a commit before using it in any Git diff, then compare it with `git rev-parse HEAD` and inspect project-scoped committed and working-tree changes from the project root:
     ```bash
     GRAPH_COMMIT=$(git rev-parse --verify --end-of-options "${GRAPH_COMMIT_RAW}^{commit}" 2>/dev/null)
     git rev-parse HEAD
     git diff --name-only "$GRAPH_COMMIT" HEAD -- .
     git diff --cached --name-only -- .
     git diff --name-only -- .
     git ls-files --others --exclude-standard -- .
     ```
   - The `-- .` pathspec is required: commits that only touch a sibling monorepo project must not make this graph stale. A hash mismatch alone is not stale when the project diff is empty.
   - Ignore the selected data directory (`.ua/` or legacy `.understand-anything/`) in every command's output because it contains generated graph artifacts, not project source drift.
   - If the committed diff or any working-tree command reports project files, warn before answering that graph-derived context may omit those changes. Suggest: Run `/understand` to refresh the graph.
   - Run the commit diff only when `GRAPH_COMMIT_RAW` resolves successfully. If the graph commit or Git metadata is missing, invalid, or unavailable, give a brief best-effort warning and continue instead of blocking.

3. **Read project metadata only** — use Grep or Read with a line limit to extract just the `"project"` section from the top of the file for context (name, description, languages, frameworks).

4. **Search for relevant nodes** — use Grep to search the knowledge graph file for the user's query keywords: "$ARGUMENTS"
   - Search `"name"` fields: `grep -i "query_keyword"` in the graph file
   - Search `"summary"` fields for semantic matches
   - Search `"tags"` arrays for topic matches
   - Note the `id` values of all matching nodes

5. **Find connected edges** — for each matched node ID, Grep for that ID in the `edges` section to find:
   - What it imports or depends on (downstream)
   - What calls or imports it (upstream)
   - This gives you the 1-hop subgraph around the query

6. **Read layer context** — Grep for `"layers"` to understand which architectural layers the matched nodes belong to.

7. **Answer the query** using only the relevant subgraph:
   - For every `claim` node in the subgraph, read the whole node before citing it and apply **Reading Claim Nodes** above. A name match alone does not show the verdict; fetch the node by id: `jq --arg id "<claim id>" '.nodes[] | select(.id==$id)' "$UA_DIR/knowledge-graph.json"`
   - Reference specific files, functions, and relationships from the graph
   - Explain which layer(s) are relevant and why
   - Be concise but thorough — link concepts to actual code locations
   - If the query doesn't match any nodes, say so and suggest related terms from the graph
