import { describe, it, expect } from "vitest";
import { resolveCalls, selectGraphSymbols, type FileSymbolTable } from "../call-resolver.js";
import type { CallGraphEntry, SymbolInfo } from "../types.js";

const sym = (
  qualname: string,
  kind: SymbolInfo["kind"],
  lines: [number, number] = [1, 10],
  extra: Partial<SymbolInfo> = {},
): SymbolInfo => ({
  qualname,
  name: qualname.split(".").pop()!,
  kind,
  lineRange: lines,
  params: [],
  depth: qualname.split(".").length - 1,
  ...extra,
});

const table = (filePath: string, symbols: SymbolInfo[]): FileSymbolTable => ({
  filePath,
  symbols,
  imports: [],
});

const call = (
  caller: string,
  callee: string,
  calleeName?: string,
  calleeReceiver?: string,
): CallGraphEntry => ({
  caller,
  callee,
  lineNumber: 42,
  calleeName: calleeName ?? (calleeReceiver ? callee.split(".").pop() : callee),
  calleeReceiver,
});

describe("resolveCalls", () => {
  describe("Tier 1 — lexical scope walk", () => {
    it("binds a closure calling a sibling closure, innermost first", () => {
      const own = table("a.py", [
        sym("outer", "function"),
        sym("outer.helper", "closure"),
        sym("outer.inner", "closure"),
        sym("helper", "function"), // module-level shadowed by outer.helper
      ]);
      const { resolved } = resolveCalls("a.py", [call("outer.inner", "helper")], own, []);
      expect(resolved).toHaveLength(1);
      // Enclosing function scope wins over module scope.
      expect(resolved[0].target).toBe("function:a.py:outer.helper");
      expect(resolved[0].confidence).toBe("exact");
    });

    it("falls through to module scope when no enclosing scope defines the name", () => {
      const own = table("a.py", [
        sym("outer", "function"),
        sym("outer.inner", "closure"),
        sym("helper", "function"),
      ]);
      const { resolved } = resolveCalls("a.py", [call("outer.inner", "helper")], own, []);
      expect(resolved[0].target).toBe("function:a.py:helper");
    });

    it("SKIPS enclosing class scopes — a method's bare call is not a sibling method", () => {
      // Python's LEGB does not include class scopes. A bare `helper()` inside
      // Service.start resolves to the module-level helper, never Service.helper.
      // Binding the sibling would be a confidently wrong edge.
      const own = table("a.py", [
        sym("Service", "class"),
        sym("Service.start", "method"),
        sym("Service.helper", "method"),
        sym("helper", "function"),
      ]);
      const { resolved } = resolveCalls("a.py", [call("Service.start", "helper")], own, []);
      expect(resolved[0].target).toBe("function:a.py:helper");
      expect(resolved[0].target).not.toBe("function:a.py:Service.helper");
    });

    it("resolves a constructor call to a class: node", () => {
      const own = table("a.py", [sym("main", "function"), sym("Widget", "class")]);
      const { resolved } = resolveCalls("a.py", [call("main", "Widget")], own, []);
      expect(resolved[0].target).toBe("class:a.py:Widget");
    });
  });

  describe("Tier 2 — self. / cls.", () => {
    it("binds self.method to the owning class member", () => {
      const own = table("a.py", [
        sym("Service", "class"),
        sym("Service.start", "method"),
        sym("Service.setup", "method"),
      ]);
      const { resolved } = resolveCalls(
        "a.py",
        [call("Service.start", "self.setup", "setup", "self")],
        own,
        [],
      );
      expect(resolved[0].target).toBe("function:a.py:Service.setup");
    });

    it("finds the owning class through an intervening closure", () => {
      const own = table("a.py", [
        sym("Service", "class"),
        sym("Service.start", "method"),
        sym("Service.start.inner", "closure"),
        sym("Service.setup", "method"),
      ]);
      const { resolved } = resolveCalls(
        "a.py",
        [call("Service.start.inner", "self.setup", "setup", "self")],
        own,
        [],
      );
      expect(resolved[0].target).toBe("function:a.py:Service.setup");
    });

    it("refuses an inherited member rather than guessing (no base-class walk)", () => {
      const own = table("a.py", [sym("Child", "class"), sym("Child.run", "method")]);
      const { resolved, unresolved } = resolveCalls(
        "a.py",
        [call("Child.run", "self.inherited", "inherited", "self")],
        own,
        [],
      );
      expect(resolved).toHaveLength(0);
      expect(unresolved[0].reason).toBe("receiver-unknown");
    });
  });

  describe("Tier 3 — imported names", () => {
    const own = table("a.py", [sym("main", "function")]);

    it("binds a name defined at depth 0 in exactly one imported file", () => {
      const dep = table("task.py", [sym("routine_scope", "function", [10, 40], { depth: 0 })]);
      const { resolved } = resolveCalls("a.py", [call("main", "routine_scope")], own, [dep]);
      expect(resolved[0].target).toBe("function:task.py:routine_scope");
      expect(resolved[0].confidence).toBe("exact");
    });

    it("refuses when two imported files define the name", () => {
      const d1 = table("x.py", [sym("shared", "function", [1, 20], { depth: 0 })]);
      const d2 = table("y.py", [sym("shared", "function", [1, 20], { depth: 0 })]);
      const { resolved, unresolved } = resolveCalls("a.py", [call("main", "shared")], own, [d1, d2]);
      expect(resolved).toHaveLength(0);
      expect(unresolved[0].reason).toBe("ambiguous");
    });

    it("marks a stdlib/third-party name external", () => {
      const { unresolved } = resolveCalls("a.py", [call("main", "len")], own, []);
      expect(unresolved[0].reason).toBe("external");
    });

    it("does not bind a nested symbol from an imported file", () => {
      // Only depth-0 names are importable.
      const dep = table("x.py", [sym("outer.buried", "closure", [5, 9], { depth: 1 })]);
      const { unresolved } = resolveCalls("a.py", [call("main", "buried")], own, [dep]);
      expect(unresolved[0].reason).toBe("external");
    });
  });

  describe("Tier 4 — unique-name attribute", () => {
    const own = table("a.py", [sym("main", "function")]);

    it("binds an arbitrary receiver when the name is unique, at lower confidence", () => {
      const dep = table("conn.py", [sym("Connection", "class"), sym("Connection.dispatch", "method")]);
      const { resolved } = resolveCalls(
        "a.py",
        [call("main", "conn.dispatch", "dispatch", "conn")],
        own,
        [dep],
      );
      expect(resolved[0].target).toBe("function:conn.py:Connection.dispatch");
      expect(resolved[0].confidence).toBe("unique");
    });

    it("binds past a Protocol stub to the sole implementation", () => {
      // A stub cannot be a call target at runtime, so `LoadBalancerContextLike`
      // declaring `remove_worker` does not make the name ambiguous — the one
      // class that implements it is the only executable answer.
      const proto = table("base.py", [
        sym("LoadBalancerContextLike", "class"),
        sym("LoadBalancerContextLike.remove_worker", "method", [10, 11], { isStub: true }),
        sym("LoadBalancerContext", "class"),
        sym("LoadBalancerContext.remove_worker", "method", [40, 45]),
      ]);
      const { resolved } = resolveCalls(
        "a.py",
        [call("main", "ctx.remove_worker", "remove_worker", "ctx")],
        own,
        [proto],
      );
      expect(resolved[0].target).toBe("function:base.py:LoadBalancerContext.remove_worker");
    });

    it("still refuses when several real bodies share a name", () => {
      // The dunder case: `__exit__` on twenty classes is genuine ambiguity and a
      // guessed target would be actively wrong.
      const d1 = table("x.py", [sym("A", "class"), sym("A.__exit__", "method", [1, 9])]);
      const d2 = table("y.py", [sym("B", "class"), sym("B.__exit__", "method", [1, 9])]);
      const { resolved, unresolved } = resolveCalls(
        "a.py",
        [call("main", "obj.__exit__", "__exit__", "obj")],
        own,
        [d1, d2],
      );
      expect(resolved).toHaveLength(0);
      expect(unresolved[0].reason).toBe("receiver-unknown");
    });

    it("refuses when every candidate is a stub", () => {
      const d1 = table("x.py", [sym("A", "class"), sym("A.run", "method", [1, 2], { isStub: true })]);
      const d2 = table("y.py", [sym("B", "class"), sym("B.run", "method", [1, 2], { isStub: true })]);
      const { resolved } = resolveCalls(
        "a.py",
        [call("main", "obj.run", "run", "obj")],
        own,
        [d1, d2],
      );
      expect(resolved).toHaveLength(0);
    });

    it("refuses when the name is not unique", () => {
      const d1 = table("x.py", [sym("A", "class"), sym("A.dispatch", "method")]);
      const d2 = table("y.py", [sym("B", "class"), sym("B.dispatch", "method")]);
      const { resolved, unresolved } = resolveCalls(
        "a.py",
        [call("main", "conn.dispatch", "dispatch", "conn")],
        own,
        [d1, d2],
      );
      expect(resolved).toHaveLength(0);
      expect(unresolved[0].reason).toBe("receiver-unknown");
    });

    it("can be disabled, and then refuses even a unique name", () => {
      const dep = table("conn.py", [sym("Connection", "class"), sym("Connection.dispatch", "method")]);
      const { resolved, unresolved } = resolveCalls(
        "a.py",
        [call("main", "conn.dispatch", "dispatch", "conn")],
        own,
        [dep],
        { uniqueNameTier: false },
      );
      expect(resolved).toHaveLength(0);
      expect(unresolved[0].reason).toBe("receiver-unknown");
    });
  });

  describe("hygiene", () => {
    it("drops self-edges", () => {
      const own = table("a.py", [sym("recurse", "function")]);
      const { resolved } = resolveCalls("a.py", [call("recurse", "recurse")], own, []);
      expect(resolved).toHaveLength(0);
    });

    it("dedupes repeated calls and keeps the strongest confidence", () => {
      const own = table("a.py", [sym("main", "function"), sym("helper", "function")]);
      const dep = table("h.py", [sym("helper", "function", [1, 5], { depth: 0 })]);
      const { resolved } = resolveCalls(
        "a.py",
        [
          call("main", "obj.helper", "helper", "obj"), // Tier 4 -> unique
          call("main", "helper"), // Tier 1 -> exact, same target
        ],
        own,
        [dep],
      );
      const toHelper = resolved.filter((r) => r.target === "function:a.py:helper");
      expect(toHelper).toHaveLength(1);
      expect(toHelper[0].confidence).toBe("exact");
    });

    it("marks an uncallable callee expression dynamic", () => {
      const own = table("a.py", [sym("main", "function")]);
      const entry: CallGraphEntry = {
        caller: "main",
        callee: "handlers[key]",
        lineNumber: 3,
        calleeName: undefined,
      };
      const { unresolved } = resolveCalls("a.py", [entry], own, []);
      expect(unresolved[0].reason).toBe("dynamic");
    });

    it("gates cleanly when the extractor emitted no symbols", () => {
      const { resolved, unresolved } = resolveCalls(
        "a.go",
        [call("main", "helper")],
        table("a.go", []),
        [],
      );
      expect(resolved).toHaveLength(0);
      expect(unresolved[0].reason).toBe("no-symbols");
    });
  });
});

describe("selectGraphSymbols", () => {
  const endpoints = (...q: string[]) => new Set(q);

  it("admits a closure only when it participates in the call graph", () => {
    const own = table("a.py", [
      sym("outer", "function", [1, 30]),
      sym("outer.used", "closure", [5, 12]), // 8 lines, but is an endpoint
      sym("outer.unused", "closure", [15, 22]), // 8 lines, no calls
    ]);
    const { selected } = selectGraphSymbols(own, endpoints("outer.used"));
    const quals = selected.map((s) => s.qualname);
    expect(quals).toContain("outer.used");
    expect(quals).not.toContain("outer.unused");
  });

  it("admits a large closure even with no calls", () => {
    const own = table("a.py", [sym("outer", "function", [1, 60]), sym("outer.big", "closure", [5, 40])]);
    const { selected } = selectGraphSymbols(own, endpoints());
    expect(selected.map((s) => s.qualname)).toContain("outer.big");
  });

  it("uses a lower bar for methods than for module functions", () => {
    const own = table("a.py", [
      sym("C", "class", [1, 40]),
      sym("C.short", "method", [2, 8]), // 7 lines -> kept (>=5)
      sym("tiny", "function", [30, 33], { exported: undefined }), // 4 lines -> dropped
    ]);
    const quals = selectGraphSymbols(own, endpoints()).selected.map((s) => s.qualname);
    expect(quals).toContain("C.short");
    expect(quals).not.toContain("tiny");
  });

  it("always keeps classes", () => {
    const own = table("a.py", [sym("Tiny", "class", [1, 2])]);
    expect(selectGraphSymbols(own, endpoints()).selected).toHaveLength(1);
  });

  it("truncates deterministically and reports the count", () => {
    const many = Array.from({ length: 50 }, (_, i) => sym(`C${i}`, "class", [i + 1, i + 2]));
    const { selected, truncated } = selectGraphSymbols(table("a.py", many), endpoints(), {
      maxPerFile: 40,
    });
    expect(selected).toHaveLength(40);
    expect(truncated).toBe(10);
    expect(selected[0].qualname).toBe("C0"); // source order, not hash order
  });

  it("never truncates a call endpoint, even past the cap", () => {
    // A truncated endpoint dangles every edge naming it, and the merge script
    // drops such edges without error — the loss is invisible. Measured on wool:
    // a naive cap discarded 7 symbols from discovery/local.py and took 16 edge
    // endpoints with them.
    const many = Array.from({ length: 50 }, (_, i) => sym(`C${i}`, "class", [i + 1, i + 2]));
    const late = endpoints("C45", "C48"); // beyond a cap of 40 in source order
    const { selected, truncated } = selectGraphSymbols(table("a.py", many), late, {
      maxPerFile: 40,
    });
    const quals = selected.map((s) => s.qualname);
    expect(quals).toContain("C45");
    expect(quals).toContain("C48");
    expect(selected).toHaveLength(40); // still bounded
    expect(truncated).toBe(10);
    // Output stays in source order after the endpoint carve-out.
    const lines = selected.map((s) => s.lineRange[0]);
    expect([...lines].sort((a, b) => a - b)).toEqual(lines);
  });

  it("never truncates a class whose members are kept", () => {
    // Found on the first full pipeline run: the cap dropped `LocalDiscovery`
    // from a 58-symbol file while keeping its methods, which re-parents those
    // methods onto the file node and loses the class entirely. `contains`
    // chains to the nearest emitted ancestor, so an ancestor is not
    // discretionary.
    const many: SymbolInfo[] = [sym("Big", "class", [1, 400])];
    for (let i = 0; i < 60; i++) {
      many.push(sym(`Big.m${i}`, "method", [i * 5 + 2, i * 5 + 6]));
    }
    const { selected } = selectGraphSymbols(table("a.py", many), endpoints(), {
      maxPerFile: 40,
    });
    const quals = new Set(selected.map((s) => s.qualname));
    expect(quals.has("Big")).toBe(true);
    // Every kept method still has its class present.
    for (const s of selected) {
      if (s.qualname.includes(".")) {
        expect(quals.has(s.qualname.split(".")[0])).toBe(true);
      }
    }
  });

  it("keeps a chain of ancestors reachable for every selected symbol", () => {
    // The `contains` chain the agent builds walks to the nearest EMITTED
    // ancestor; this asserts such an ancestor always exists (or it is file-level).
    const own = table("a.py", [
      sym("C", "class", [1, 50]),
      sym("C.m", "method", [2, 40]),
      sym("C.m.inner", "closure", [5, 9]),
    ]);
    const { selected } = selectGraphSymbols(own, endpoints("C.m.inner"));
    const quals = new Set(selected.map((s) => s.qualname));
    for (const s of selected) {
      const parts = s.qualname.split(".");
      let ok = parts.length === 1; // file-level fallback
      for (let k = parts.length - 1; k >= 1 && !ok; k--) {
        if (quals.has(parts.slice(0, k).join("."))) ok = true;
      }
      expect(ok).toBe(true);
    }
  });
});
