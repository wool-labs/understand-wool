#!/usr/bin/env node
/**
 * extract-structure.mjs
 *
 * Deterministic structural extraction script for the file-analyzer agent.
 * Uses PluginRegistry (TreeSitterPlugin + non-code parsers) from @understand-anything/core
 * to replace the LLM-generated throwaway regex scripts in Phase 1.
 *
 * Usage:
 *   node extract-structure.mjs <input.json> <output.json>
 *
 * Input JSON:
 *   { projectRoot, batchFiles: [{path, language, sizeLines, fileCategory}], batchImportData }
 *
 * Output JSON:
 *   { scriptCompleted, filesAnalyzed, filesSkipped, results: [...] }
 */

import { createRequire } from 'node:module';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { existsSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import {
  analyzeFileWithOutcomes,
  buildResult as buildExtractResult,
} from './extract-structure-result.mjs';

export {
  analyzeFileWithOutcomes,
  buildResult,
} from './extract-structure-result.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
// skills/understand/ -> plugin root is two dirs up
const pluginRoot = resolve(__dirname, '../..');
const require = createRequire(resolve(pluginRoot, 'package.json'));

// ---------------------------------------------------------------------------
// Resolve @understand-anything/core
//
// Node ESM dynamic import() requires a file:// URL on Windows; passing a raw
// absolute path like "C:\..." throws ERR_UNSUPPORTED_ESM_URL_SCHEME because the
// loader parses "C:" as a URL scheme. Wrap both resolutions in pathToFileURL().
// ---------------------------------------------------------------------------
let core;
try {
  core = await import(pathToFileURL(require.resolve('@understand-anything/core')).href);
} catch {
  // Fallback: direct path for installed plugin cache layouts
  core = await import(pathToFileURL(resolve(pluginRoot, 'packages/core/dist/index.js')).href);
}

const {
  TreeSitterPlugin,
  PluginRegistry,
  builtinLanguageConfigs,
  registerAllParsers,
  resolveCalls,
  selectGraphSymbols,
} = core;

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
/** Result-shape symbols (startLine/endLine) back to core's SymbolInfo shape. */
function toSymbolInfo(entry) {
  return {
    qualname: entry.qualname,
    name: entry.name,
    kind: entry.kind,
    parentQualname: entry.parentQualname,
    lineRange: [entry.startLine, entry.endLine],
    params: entry.params ?? [],
    returnType: entry.returnType,
    depth: entry.depth,
    isAsync: entry.isAsync,
    exported: entry.exported,
    isStub: entry.isStub,
  };
}

/**
 * Resolve every call site in the batch, then decide which symbols become nodes.
 *
 * Deliberately a second pass over all results rather than per-file work:
 *
 *  - a call from file A to file B in the *same batch* must resolve against B's
 *    real symbol table, which does not exist yet while A is being analyzed; and
 *  - `selectGraphSymbols` needs to know whether a symbol is a call endpoint
 *    *anywhere in the batch*, otherwise a target node can be filtered away and
 *    the edge pointing at it silently dropped by the merge script.
 *
 * Neighbour files named in `batchImportData` but outside the batch are parsed
 * once each and memoized; a hub module imported by eight batch files costs one
 * parse, and the total is bounded by import fan-out rather than repo size.
 */
function resolveBatchCalls(registry, projectRoot, results, batchImportData, priorEndpoints) {
  const tables = new Map();
  for (const result of results) {
    if (!result.symbols) continue;
    tables.set(result.path, {
      filePath: result.path,
      symbols: result.symbols.map(toSymbolInfo),
      imports: [],
    });
  }

  const neighborTable = (relPath) => {
    if (tables.has(relPath)) return tables.get(relPath);
    let table = null;
    try {
      const content = readFileSync(join(projectRoot, relPath), 'utf-8');
      const { analysis } = analyzeFileWithOutcomes(
        registry,
        { path: relPath, fileCategory: 'code' },
        content,
      );
      if (analysis?.symbols?.length) {
        table = { filePath: relPath, symbols: analysis.symbols, imports: [] };
      }
    } catch (err) {
      process.stderr.write(`Warning: extract-structure: neighbor parse failed for ${relPath}: ${err.message}\n`);
    }
    tables.set(relPath, table);
    return table;
  };

  // Pass 1 — resolve, collecting every endpoint qualname across the batch.
  const perFile = new Map();
  const endpointsByFile = new Map();
  let resolvedTotal = 0;
  let unresolvedTotal = 0;

  for (const result of results) {
    const own = tables.get(result.path);
    if (!own || !result.callGraph) continue;

    const imported = (batchImportData?.[result.path] ?? [])
      .map(neighborTable)
      .filter(Boolean);

    const { resolved, unresolved } = resolveCalls(result.path, result.callGraph, own, imported, {
      uniqueNameTier: process.env.UA_CALLS_UNIQUE_NAME_TIER !== '0',
    });
    perFile.set(result.path, resolved);
    resolvedTotal += resolved.length;
    unresolvedTotal += unresolved.length;

    for (const call of [...resolved]) {
      for (const id of [call.source, call.target]) {
        // id is "<kind>:<path>:<qualname>" — split off the leading kind, then
        // the path, so a qualname containing ':' cannot corrupt the parse.
        const firstColon = id.indexOf(':');
        const lastColon = id.lastIndexOf(':');
        const path = id.slice(firstColon + 1, lastColon);
        const qualname = id.slice(lastColon + 1);
        if (!endpointsByFile.has(path)) endpointsByFile.set(path, new Set());
        endpointsByFile.get(path).add(qualname);
      }
    }
  }

  // Fold in endpoints discovered by *other batches* in an earlier pass.
  //
  // Endpoints collected above are batch-scoped, but call targets are run-scoped:
  // a call in batch 1 to a symbol defined in batch 0 marks that symbol as an
  // endpoint only in batch 1's bookkeeping, so batch 0 may filter it away and
  // the merge script then drops the edge as dangling. Measured on wool: 5 of 386
  // edges lost exactly this way, silently.
  for (const [path, quals] of Object.entries(priorEndpoints ?? {})) {
    if (!endpointsByFile.has(path)) endpointsByFile.set(path, new Set());
    for (const q of quals) endpointsByFile.get(path).add(q);
  }

  // Pass 2 — select nodes now that run-global endpoints are known.
  let truncatedTotal = 0;
  for (const result of results) {
    const own = tables.get(result.path);
    if (!own) continue;
    const { selected, truncated } = selectGraphSymbols(
      own,
      endpointsByFile.get(result.path) ?? new Set(),
    );
    truncatedTotal += truncated;
    if (selected.length > 0) {
      result.graphSymbols = selected.map(s => ({
        qualname: s.qualname,
        kind: s.kind === 'class' ? 'class' : 'function',
        startLine: s.lineRange[0],
        endLine: s.lineRange[1],
      }));
    }
    const edges = perFile.get(result.path) ?? [];
    if (edges.length > 0) result.callEdges = edges;
    result.metrics = {
      ...(result.metrics ?? {}),
      callEdgeCount: edges.length,
      symbolsTruncated: truncated,
    };
  }

  return {
    resolved: resolvedTotal,
    unresolved: unresolvedTotal,
    symbolsTruncated: truncatedTotal,
    uniqueNameTier: process.env.UA_CALLS_UNIQUE_NAME_TIER !== '0',
    // Every endpoint this batch saw, so a second pass can hand it to the other
    // batches. Keyed by file, since that is how selection is scoped.
    endpoints: Object.fromEntries(
      [...endpointsByFile.entries()].map(([path, set]) => [path, [...set].sort()]),
    ),
  };
}

async function main() {
  const [,, inputPath, outputPath] = process.argv;
  if (!inputPath || !outputPath) {
    process.stderr.write('Usage: node extract-structure.mjs <input.json> <output.json>\n');
    process.exit(1);
  }

  // Read input
  const inputRaw = readFileSync(inputPath, 'utf-8');
  const input = JSON.parse(inputRaw);
  const { projectRoot, batchFiles, batchImportData } = input;

  if (!projectRoot || !Array.isArray(batchFiles)) {
    throw new Error('Invalid input: must contain projectRoot and batchFiles array');
  }

  // Create tree-sitter plugin with all configs that have WASM grammars
  const tsConfigs = builtinLanguageConfigs.filter(c => c.treeSitter);
  const tsPlugin = new TreeSitterPlugin(tsConfigs);
  await tsPlugin.init();

  // Create registry and register tree-sitter + all non-code parsers
  const registry = new PluginRegistry();
  registry.register(tsPlugin);
  registerAllParsers(registry);

  const results = [];
  const filesSkipped = [];
  const analysisOutcomes = {
    structure: { succeeded: 0, failed: 0 },
    callGraph: { succeeded: 0, failed: 0, skipped: 0 },
  };

  for (const file of batchFiles) {
    const absolutePath = join(projectRoot, file.path);

    // Read file content
    let content;
    try {
      content = readFileSync(absolutePath, 'utf-8');
    } catch {
      filesSkipped.push(file.path);
      continue;
    }

    // Line counts. POSIX text files end in a trailing newline, which makes
    // `split('\n')` produce one extra empty element. Match `wc -l` semantics
    // (used by the project scanner for `sizeLines`) so the two counts agree.
    const lines = content.split('\n');
    const totalLines = content.endsWith('\n') ? Math.max(0, lines.length - 1) : lines.length;
    const nonEmptyLines = lines.filter(l => l.trim().length > 0).length;

    const { analysis, callGraph, structureOutcome, callGraphOutcome } =
      analyzeFileWithOutcomes(registry, file, content);

    if (structureOutcome === 'skipped') {
      filesSkipped.push(file.path);
      continue;
    }

    analysisOutcomes.structure[structureOutcome] += 1;
    analysisOutcomes.callGraph[callGraphOutcome] += 1;

    // Build result object
    const result = buildExtractResult(file, totalLines, nonEmptyLines, analysis, callGraph, batchImportData);
    results.push(result);
  }

  // Optional third argument: a merged endpoint map from a prior pass over every
  // batch. Without it, cross-batch call targets can be filtered out of the batch
  // that owns them and their edges dropped at merge time.
  const endpointsPath = process.argv[4];
  let priorEndpoints = {};
  if (endpointsPath && existsSync(endpointsPath)) {
    try {
      priorEndpoints = JSON.parse(readFileSync(endpointsPath, 'utf-8'));
    } catch (err) {
      process.stderr.write(`Warning: extract-structure: could not read endpoints ${endpointsPath}: ${err.message}\n`);
    }
  }

  const callResolution = resolveBatchCalls(registry, projectRoot, results, batchImportData, priorEndpoints);

  // Write output
  const output = {
    scriptCompleted: true,
    filesAnalyzed: results.length,
    filesSkipped,
    analysisOutcomes,
    callResolution,
    results,
  };

  writeFileSync(outputPath, JSON.stringify(output, null, 2), 'utf-8');

  if (!existsSync(outputPath)) {
    throw new Error(`output file missing after write: ${outputPath}`);
  }
}

// ---------------------------------------------------------------------------
// Run only when executed directly as a CLI; importing the module (e.g. from
// tests) must not trigger main().
//
// Canonicalize both sides through realpathSync. Node ESM resolves
// import.meta.url through symlinks but pathToFileURL(process.argv[1]) preserves
// them, so a raw equality check silently no-ops when the script is invoked via
// a symlinked plugin install path (the default in Claude Code / Copilot CLI
// caches). See GitHub issue #162.
// ---------------------------------------------------------------------------
function isCliEntry() {
  if (!process.argv[1]) return false;
  try {
    const modulePath = realpathSync(fileURLToPath(import.meta.url));
    const argvPath = realpathSync(process.argv[1]);
    return modulePath === argvPath;
  } catch {
    return false;
  }
}

if (isCliEntry()) {
  try {
    await main();
  } catch (err) {
    process.stderr.write(`extract-structure.mjs failed: ${err.message}\n${err.stack}\n`);
    process.exit(1);
  }
}
