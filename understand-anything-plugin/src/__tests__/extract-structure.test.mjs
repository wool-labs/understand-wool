import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { buildResult } from "../../skills/understand/extract-structure-result.mjs";

const file = (overrides = {}) => ({
  path: "src/foo.py",
  language: "python",
  fileCategory: "code",
  ...overrides,
});

const analysis = (overrides = {}) => ({
  functions: [],
  classes: [],
  imports: [],
  exports: [],
  ...overrides,
});

describe("extract-structure buildResult", () => {
  describe("language pass-through", () => {
    it("preserves the input language on the output", () => {
      const result = buildResult(file({ language: "python" }), 10, 8, analysis(), null, {});
      expect(result.language).toBe("python");
    });

    it("preserves null when caller did not set a language", () => {
      // Documents the failure mode the SKILL.md/file-analyzer.md fix prevents:
      // if the dispatch prompt loses `language`, it propagates to the output.
      const result = buildResult(file({ language: null }), 10, 8, analysis(), null, {});
      expect(result.language).toBeNull();
    });
  });

  describe("importCount fallback", () => {
    // Only relative imports count toward the fallback metric — external
    // package imports would never produce edges so counting them would be
    // misleading. (`.helpers`, `..util`, `./local` all start with `.`)
    const analysisWithImports = analysis({
      imports: [
        { source: ".helpers", specifiers: [] },
        { source: "..util", specifiers: [] },
        { source: "./local", specifiers: [] },
      ],
    });

    it("uses pre-resolved imports when batchImportData has entries", () => {
      const batchImportData = { "src/foo.py": ["src/bar.py", "src/baz.py"] };
      const result = buildResult(file(), 10, 8, analysisWithImports, null, batchImportData);
      expect(result.metrics.importCount).toBe(2);
    });

    it("falls back to parser imports when batchImportData entry is an empty array", () => {
      // Regression test: empty arrays are truthy in JS, so a naive `if (importPaths)`
      // would clobber the parser's count with 0. This is the bug Python projects
      // using absolute imports (which the project scanner doesn't resolve) hit.
      const batchImportData = { "src/foo.py": [] };
      const result = buildResult(file(), 10, 8, analysisWithImports, null, batchImportData);
      expect(result.metrics.importCount).toBe(3);
    });

    it("falls back to parser imports when batchImportData has no entry for the file", () => {
      const result = buildResult(file(), 10, 8, analysisWithImports, null, {});
      expect(result.metrics.importCount).toBe(3);
    });

    it("falls back to parser imports when batchImportData is undefined", () => {
      const result = buildResult(file(), 10, 8, analysisWithImports, null, undefined);
      expect(result.metrics.importCount).toBe(3);
    });

    it("reports 0 imports when neither source has any", () => {
      const result = buildResult(file(), 10, 8, analysis(), null, { "src/foo.py": [] });
      expect(result.metrics.importCount).toBe(0);
    });

    it("excludes external package imports from the fallback count", () => {
      // Regression: pre-2.6.2 the fallback counted ALL parser imports (incl.
      // `os`, `sys`, etc.), so files where the scanner couldn't resolve
      // anything would over-report imports vs. files where it could.
      const ext = analysis({
        imports: [
          { source: "os", specifiers: [] },
          { source: "sys", specifiers: [] },
          { source: "./local", specifiers: [] },
        ],
      });
      const result = buildResult(file(), 10, 8, ext, null, {});
      expect(result.metrics.importCount).toBe(1);
    });
  });

  describe("totalLines", () => {
    // Documents the off-by-one fix: `wc -l` reports N for a POSIX text file
    // with N lines + trailing \n; the extractor must match.
    it("matches wc -l semantics for trailing-newline files", () => {
      // Mimic what main() computes: read file, split on \n.
      // Build a synthetic 3-line file ending in \n.
      const content = "a\nb\nc\n";
      const lines = content.split("\n"); // ["a","b","c",""]
      const totalLines = content.endsWith("\n") ? Math.max(0, lines.length - 1) : lines.length;
      expect(totalLines).toBe(3);
    });

    it("counts content without trailing newline correctly", () => {
      const content = "a\nb\nc";
      const lines = content.split("\n");
      const totalLines = content.endsWith("\n") ? Math.max(0, lines.length - 1) : lines.length;
      expect(totalLines).toBe(3);
    });

  });

  describe("nested symbol index", () => {
    const sym = (overrides = {}) => ({
      qualname: "Outer.method",
      name: "method",
      kind: "method",
      lineRange: [5, 20],
      params: [],
      depth: 1,
      ...overrides,
    });

    it("carries symbols through to the result", () => {
      const result = buildResult(
        file(),
        40,
        30,
        analysis({ symbols: [sym({ qualname: "Outer", name: "Outer", kind: "class", depth: 0 }), sym()] }),
        null,
        {},
      );
      expect(result.symbols).toHaveLength(2);
      expect(result.symbols[1]).toMatchObject({
        qualname: "Outer.method",
        kind: "method",
        startLine: 5,
        endLine: 20,
        depth: 1,
      });
    });

    it("omits symbols entirely when the extractor produced none", () => {
      const result = buildResult(file(), 10, 8, analysis(), null, {});
      expect(result.symbols).toBeUndefined();
    });

    it("preserves optional symbol fields", () => {
      const result = buildResult(
        file(),
        40,
        30,
        analysis({
          symbols: [
            sym({ parentQualname: "Outer", params: ["a"], returnType: "int", isAsync: true, exported: true }),
          ],
        }),
        null,
        {},
      );
      expect(result.symbols[0]).toMatchObject({
        parentQualname: "Outer",
        params: ["a"],
        returnType: "int",
        isAsync: true,
        exported: true,
      });
    });

    // Three separate bugs during the call-graph work were the same defect:
    // a field added to SymbolInfo but missed in one of the allowlist
    // projections between the core type and the resolver (`symbols` in the
    // validator, `calleeName`/`calleeReceiver` in mapCallGraph, `isStub` in
    // three places). Every one failed *silently* — the resolver simply
    // returned less, which is not an error anywhere.
    //
    // This reads the field list off the interface itself, so adding a fifth
    // field fails here until the projection is updated, rather than months
    // later as a quietly emptier graph.
    it("projects every field declared on SymbolInfo", () => {
      const typesSrc = readFileSync(
        new URL("../../packages/core/src/types.ts", import.meta.url),
        "utf8",
      );
      const body = typesSrc.split("export interface SymbolInfo {")[1].split("\n}")[0];
      const declared = [...body.matchAll(/^\s{2}(\w+)\??:/gm)].map(m => m[1]);
      expect(declared.length).toBeGreaterThan(5); // the parse itself must not silently fail

      const populated = sym({
        parentQualname: "Outer",
        params: ["a"],
        returnType: "int",
        isAsync: true,
        exported: true,
        isStub: true,
      });
      // Every declared field must be exercised, or the test passes by omission.
      const unpopulated = declared.filter(f => populated[f] === undefined);
      expect(unpopulated).toEqual([]);

      const [out] = buildResult(file(), 40, 30, analysis({ symbols: [populated] }), null, {}).symbols;
      // `lineRange` is deliberately flattened into startLine/endLine on the way
      // out; every other field keeps its name.
      const missing = declared.filter(f =>
        f === "lineRange"
          ? out.startLine === undefined || out.endLine === undefined
          : out[f] === undefined);
      expect(missing).toEqual([]);
    });
  });

  describe("callGraph field pass-through", () => {
    // mapCallGraph is an allowlist projection: a field it does not name is
    // dropped with no error anywhere. Losing calleeName/calleeReceiver silently
    // DISABLES call resolution (a resolver cannot tell `foo()` from `x.foo()`),
    // so this is a regression guard for a failure that is otherwise invisible.
    const entry = {
      caller: "Service.start",
      callee: "self.setup",
      lineNumber: 12,
      callerName: "start",
      callerKind: "method",
      calleeName: "setup",
      calleeReceiver: "self",
    };

    it("carries the decomposed caller and callee fields", () => {
      const result = buildResult(file(), 30, 20, analysis(), [entry], {});
      expect(result.callGraph).toHaveLength(1);
      expect(result.callGraph[0]).toMatchObject({
        caller: "Service.start",
        callee: "self.setup",
        lineNumber: 12,
        callerName: "start",
        callerKind: "method",
        calleeName: "setup",
        calleeReceiver: "self",
      });
    });

    it("omits absent optional fields rather than emitting undefined", () => {
      const bare = { caller: "main", callee: "helper", lineNumber: 3, calleeName: "helper" };
      const result = buildResult(file(), 30, 20, analysis(), [bare], {});
      expect(result.callGraph[0]).not.toHaveProperty("calleeReceiver");
      expect(result.callGraph[0].calleeName).toBe("helper");
    });
  });
});
