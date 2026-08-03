import { describe, it, expect, beforeAll } from "vitest";
import { createRequire } from "node:module";
import { PythonExtractor } from "../python-extractor.js";

const require = createRequire(import.meta.url);

// Load tree-sitter + Python grammar once
let Parser: any;
let Language: any;
let pythonLang: any;

beforeAll(async () => {
  const mod = await import("web-tree-sitter");
  Parser = mod.Parser;
  Language = mod.Language;
  await Parser.init();
  const wasmPath = require.resolve(
    "tree-sitter-python/tree-sitter-python.wasm",
  );
  pythonLang = await Language.load(wasmPath);
});

function parse(code: string) {
  const parser = new Parser();
  parser.setLanguage(pythonLang);
  const tree = parser.parse(code);
  const root = tree.rootNode;
  return { tree, parser, root };
}

describe("PythonExtractor", () => {
  const extractor = new PythonExtractor();

  it("has correct languageIds", () => {
    expect(extractor.languageIds).toEqual(["python"]);
  });

  // ---- Functions ----

  describe("extractStructure - functions", () => {
    it("extracts simple functions with type annotations", () => {
      const { tree, parser, root } = parse(`
def hello(name: str) -> str:
    return f"Hello {name}"

def add(a: int, b: int) -> int:
    return a + b
`);
      const result = extractor.extractStructure(root);

      expect(result.functions).toHaveLength(2);

      expect(result.functions[0].name).toBe("hello");
      expect(result.functions[0].params).toEqual(["name"]);
      expect(result.functions[0].returnType).toBe("str");
      expect(result.functions[0].lineRange[0]).toBeGreaterThan(0);

      expect(result.functions[1].name).toBe("add");
      expect(result.functions[1].params).toEqual(["a", "b"]);
      expect(result.functions[1].returnType).toBe("int");

      tree.delete();
      parser.delete();
    });

    it("extracts functions without type annotations", () => {
      const { tree, parser, root } = parse(`
def greet(name):
    print(name)

def noop():
    pass
`);
      const result = extractor.extractStructure(root);

      expect(result.functions).toHaveLength(2);
      expect(result.functions[0].name).toBe("greet");
      expect(result.functions[0].params).toEqual(["name"]);
      expect(result.functions[0].returnType).toBeUndefined();

      expect(result.functions[1].name).toBe("noop");
      expect(result.functions[1].params).toEqual([]);

      tree.delete();
      parser.delete();
    });

    it("extracts functions with default parameters", () => {
      const { tree, parser, root } = parse(`
def connect(host: str, port: int = 8080, timeout: float = 30.0):
    pass
`);
      const result = extractor.extractStructure(root);

      expect(result.functions).toHaveLength(1);
      expect(result.functions[0].name).toBe("connect");
      expect(result.functions[0].params).toEqual(["host", "port", "timeout"]);

      tree.delete();
      parser.delete();
    });

    it("extracts functions with *args and **kwargs", () => {
      const { tree, parser, root } = parse(`
def flexible(*args, **kwargs):
    pass
`);
      const result = extractor.extractStructure(root);

      expect(result.functions).toHaveLength(1);
      expect(result.functions[0].params).toEqual(["*args", "**kwargs"]);

      tree.delete();
      parser.delete();
    });

    it("extracts decorated functions", () => {
      const { tree, parser, root } = parse(`
@decorator
def decorated_func():
    pass

@app.route("/api")
def api_handler():
    pass
`);
      const result = extractor.extractStructure(root);

      expect(result.functions).toHaveLength(2);
      expect(result.functions[0].name).toBe("decorated_func");
      expect(result.functions[1].name).toBe("api_handler");

      tree.delete();
      parser.delete();
    });

    it("reports correct line ranges", () => {
      const { tree, parser, root } = parse(`
def multiline(
    a: int,
    b: int,
) -> int:
    result = a + b
    return result
`);
      const result = extractor.extractStructure(root);

      expect(result.functions).toHaveLength(1);
      expect(result.functions[0].lineRange[0]).toBe(2);
      expect(result.functions[0].lineRange[1]).toBe(7);

      tree.delete();
      parser.delete();
    });
  });

  // ---- Classes ----

  describe("extractStructure - classes", () => {
    it("extracts classes with methods and properties", () => {
      const { tree, parser, root } = parse(`
class DataProcessor:
    name: str

    def __init__(self, name: str):
        self.name = name

    def process(self, data: list) -> dict:
        return transform(data)
`);
      const result = extractor.extractStructure(root);

      expect(result.classes).toHaveLength(1);
      expect(result.classes[0].name).toBe("DataProcessor");
      expect(result.classes[0].methods).toContain("__init__");
      expect(result.classes[0].methods).toContain("process");
      expect(result.classes[0].properties).toContain("name");

      tree.delete();
      parser.delete();
    });

    it("extracts dataclass-style annotated properties", () => {
      const { tree, parser, root } = parse(`
class Config:
    name: str
    value: int
    debug: bool
`);
      const result = extractor.extractStructure(root);

      expect(result.classes).toHaveLength(1);
      expect(result.classes[0].properties).toEqual(["name", "value", "debug"]);
      expect(result.classes[0].methods).toEqual([]);

      tree.delete();
      parser.delete();
    });

    it("extracts decorated classes", () => {
      const { tree, parser, root } = parse(`
@dataclass
class Config:
    name: str
    value: int = 0
`);
      const result = extractor.extractStructure(root);

      expect(result.classes).toHaveLength(1);
      expect(result.classes[0].name).toBe("Config");
      expect(result.classes[0].properties).toContain("name");
      expect(result.classes[0].properties).toContain("value");

      tree.delete();
      parser.delete();
    });

    it("extracts decorated methods within a class", () => {
      const { tree, parser, root } = parse(`
class MyClass:
    @staticmethod
    def static_method():
        pass

    @classmethod
    def class_method(cls):
        pass

    @property
    def prop(self):
        return self._prop
`);
      const result = extractor.extractStructure(root);

      expect(result.classes).toHaveLength(1);
      expect(result.classes[0].methods).toContain("static_method");
      expect(result.classes[0].methods).toContain("class_method");
      expect(result.classes[0].methods).toContain("prop");

      tree.delete();
      parser.delete();
    });

    it("filters self and cls from method params", () => {
      const { tree, parser, root } = parse(`
class Foo:
    def instance_method(self, x: int):
        pass

    @classmethod
    def class_method(cls, y: str):
        pass
`);
      const result = extractor.extractStructure(root);
      // Methods are on the class, but top-level functions should not include them
      expect(result.functions).toHaveLength(0);
      expect(result.classes[0].methods).toEqual(["instance_method", "class_method"]);

      tree.delete();
      parser.delete();
    });

    it("reports correct class line ranges", () => {
      const { tree, parser, root } = parse(`
class MyClass:
    def method_a(self):
        pass

    def method_b(self):
        pass
`);
      const result = extractor.extractStructure(root);

      expect(result.classes).toHaveLength(1);
      expect(result.classes[0].lineRange[0]).toBe(2);
      expect(result.classes[0].lineRange[1]).toBe(7);

      tree.delete();
      parser.delete();
    });
  });

  // ---- Imports ----

  describe("extractStructure - imports", () => {
    it("extracts simple import statements", () => {
      const { tree, parser, root } = parse(`
import os
import sys
`);
      const result = extractor.extractStructure(root);

      expect(result.imports).toHaveLength(2);
      expect(result.imports[0].source).toBe("os");
      expect(result.imports[0].specifiers).toEqual(["os"]);
      expect(result.imports[1].source).toBe("sys");
      expect(result.imports[1].specifiers).toEqual(["sys"]);

      tree.delete();
      parser.delete();
    });

    it("extracts from-import statements", () => {
      const { tree, parser, root } = parse(`
from pathlib import Path
from typing import Optional, List
`);
      const result = extractor.extractStructure(root);

      expect(result.imports).toHaveLength(2);
      expect(result.imports[0].source).toBe("pathlib");
      expect(result.imports[0].specifiers).toEqual(["Path"]);
      expect(result.imports[1].source).toBe("typing");
      expect(result.imports[1].specifiers).toEqual(["Optional", "List"]);

      tree.delete();
      parser.delete();
    });

    it("extracts aliased imports", () => {
      const { tree, parser, root } = parse(`
from foo import bar as baz
`);
      const result = extractor.extractStructure(root);

      expect(result.imports).toHaveLength(1);
      expect(result.imports[0].source).toBe("foo");
      expect(result.imports[0].specifiers).toEqual(["baz"]);

      tree.delete();
      parser.delete();
    });

    it("extracts dotted module imports", () => {
      const { tree, parser, root } = parse(`
import os.path
from os.path import join, exists
`);
      const result = extractor.extractStructure(root);

      expect(result.imports).toHaveLength(2);
      expect(result.imports[0].source).toBe("os.path");
      expect(result.imports[0].specifiers).toEqual(["os.path"]);
      expect(result.imports[1].source).toBe("os.path");
      expect(result.imports[1].specifiers).toEqual(["join", "exists"]);

      tree.delete();
      parser.delete();
    });

    it("extracts wildcard imports", () => {
      const { tree, parser, root } = parse(`
from os.path import *
`);
      const result = extractor.extractStructure(root);

      expect(result.imports).toHaveLength(1);
      expect(result.imports[0].source).toBe("os.path");
      expect(result.imports[0].specifiers).toEqual(["*"]);

      tree.delete();
      parser.delete();
    });

    it("handles all import types together", () => {
      const { tree, parser, root } = parse(`
import os
from pathlib import Path
from typing import Optional, List
`);
      const result = extractor.extractStructure(root);

      expect(result.imports.length).toBeGreaterThanOrEqual(3);

      tree.delete();
      parser.delete();
    });

    it("reports correct import line numbers", () => {
      const { tree, parser, root } = parse(`
import os
from pathlib import Path
`);
      const result = extractor.extractStructure(root);

      expect(result.imports[0].lineNumber).toBe(2);
      expect(result.imports[1].lineNumber).toBe(3);

      tree.delete();
      parser.delete();
    });
  });

  // ---- Exports ----

  describe("extractStructure - exports", () => {
    it("treats top-level functions as exports", () => {
      const { tree, parser, root } = parse(`
def public_func():
    pass

def another_func(x: int) -> str:
    return str(x)
`);
      const result = extractor.extractStructure(root);

      const exportNames = result.exports.map((e) => e.name);
      expect(exportNames).toContain("public_func");
      expect(exportNames).toContain("another_func");
      expect(result.exports).toHaveLength(2);

      tree.delete();
      parser.delete();
    });

    it("treats top-level classes as exports", () => {
      const { tree, parser, root } = parse(`
class MyService:
    pass

class MyModel:
    pass
`);
      const result = extractor.extractStructure(root);

      const exportNames = result.exports.map((e) => e.name);
      expect(exportNames).toContain("MyService");
      expect(exportNames).toContain("MyModel");
      expect(result.exports).toHaveLength(2);

      tree.delete();
      parser.delete();
    });

    it("treats decorated top-level definitions as exports", () => {
      const { tree, parser, root } = parse(`
@dataclass
class Config:
    name: str

@app.route("/")
def index():
    pass
`);
      const result = extractor.extractStructure(root);

      const exportNames = result.exports.map((e) => e.name);
      expect(exportNames).toContain("Config");
      expect(exportNames).toContain("index");

      tree.delete();
      parser.delete();
    });

    it("does not treat imports as exports", () => {
      const { tree, parser, root } = parse(`
import os
from pathlib import Path

def my_func():
    pass
`);
      const result = extractor.extractStructure(root);

      expect(result.exports).toHaveLength(1);
      expect(result.exports[0].name).toBe("my_func");

      tree.delete();
      parser.delete();
    });
  });

  // ---- Call Graph ----

  describe("extractCallGraph", () => {
    it("extracts simple function calls", () => {
      const { tree, parser, root } = parse(`
def process(data):
    result = transform(data)
    return format_output(result)

def main():
    process([1, 2, 3])
`);
      const result = extractor.extractCallGraph(root);

      expect(result.length).toBeGreaterThanOrEqual(2);

      const processCallers = result.filter((e) => e.caller === "process");
      expect(processCallers.some((e) => e.callee === "transform")).toBe(true);
      expect(processCallers.some((e) => e.callee === "format_output")).toBe(true);

      const mainCallers = result.filter((e) => e.caller === "main");
      expect(mainCallers.some((e) => e.callee === "process")).toBe(true);

      tree.delete();
      parser.delete();
    });

    it("extracts attribute-based calls (method calls)", () => {
      const { tree, parser, root } = parse(`
def process():
    self.method()
    os.path.join("a", "b")
    result.save()
`);
      const result = extractor.extractCallGraph(root);

      const callees = result.map((e) => e.callee);
      expect(callees).toContain("self.method");
      expect(callees).toContain("os.path.join");
      expect(callees).toContain("result.save");

      tree.delete();
      parser.delete();
    });

    it("tracks correct caller context for nested calls", () => {
      const { tree, parser, root } = parse(`
def outer():
    helper()
    def inner():
        deep_call()
    another()
`);
      const result = extractor.extractCallGraph(root);

      const outerCalls = result.filter((e) => e.caller === "outer");
      expect(outerCalls.some((e) => e.callee === "helper")).toBe(true);
      expect(outerCalls.some((e) => e.callee === "another")).toBe(true);

      // `caller` is the dotted qualname rooted at file scope, not the bare
      // innermost name — a bare name matches no graph node id.
      const innerCalls = result.filter((e) => e.caller === "outer.inner");
      expect(innerCalls.some((e) => e.callee === "deep_call")).toBe(true);
      expect(innerCalls[0]?.callerName).toBe("inner");
      expect(innerCalls[0]?.callerKind).toBe("closure");

      tree.delete();
      parser.delete();
    });

    it("reports correct line numbers for calls", () => {
      const { tree, parser, root } = parse(`
def main():
    foo()
    bar()
`);
      const result = extractor.extractCallGraph(root);

      expect(result).toHaveLength(2);
      expect(result[0].lineNumber).toBe(3);
      expect(result[1].lineNumber).toBe(4);

      tree.delete();
      parser.delete();
    });

    it("ignores top-level calls (no caller)", () => {
      const { tree, parser, root } = parse(`
print("hello")
main()
`);
      const result = extractor.extractCallGraph(root);

      // Top-level calls have no enclosing function, so they are skipped
      expect(result).toHaveLength(0);

      tree.delete();
      parser.delete();
    });

    it("handles calls inside class methods", () => {
      const { tree, parser, root } = parse(`
class Service:
    def start(self):
        self.setup()
        run_server()
`);
      const result = extractor.extractCallGraph(root);

      // Class names are part of the caller qualname: `Service.start`, not `start`.
      const startCalls = result.filter((e) => e.caller === "Service.start");
      expect(startCalls.some((e) => e.callee === "self.setup")).toBe(true);
      expect(startCalls.some((e) => e.callee === "run_server")).toBe(true);
      expect(startCalls[0]?.callerKind).toBe("method");

      // The callee is decomposed so a resolver can tell `foo()` from `x.foo()`,
      // which resolve by completely different rules.
      const setup = startCalls.find((e) => e.callee === "self.setup");
      expect(setup?.calleeName).toBe("setup");
      expect(setup?.calleeReceiver).toBe("self");
      const server = startCalls.find((e) => e.callee === "run_server");
      expect(server?.calleeName).toBe("run_server");
      expect(server?.calleeReceiver).toBeUndefined();

      tree.delete();
      parser.delete();
    });
  });

  // ---- Comprehensive ----

  describe("comprehensive Python file", () => {
    it("handles a realistic Python module", () => {
      const { tree, parser, root } = parse(`
import os
from pathlib import Path
from typing import Optional, List

class FileProcessor:
    name: str
    verbose: bool

    def __init__(self, name: str, verbose: bool = False):
        self.name = name
        self.verbose = verbose

    def process(self, paths: List[str]) -> dict:
        results = {}
        for p in paths:
            results[p] = self._read_file(p)
        return results

    def _read_file(self, path: str) -> Optional[str]:
        full = Path(path)
        if full.exists():
            return full.read_text()
        return None

def create_processor(name: str) -> FileProcessor:
    return FileProcessor(name)

@staticmethod
def utility_func(*args, **kwargs) -> None:
    print(args, kwargs)
`);
      const result = extractor.extractStructure(root);

      // Imports
      expect(result.imports.length).toBeGreaterThanOrEqual(3);

      // Class
      expect(result.classes).toHaveLength(1);
      expect(result.classes[0].name).toBe("FileProcessor");
      expect(result.classes[0].methods).toContain("__init__");
      expect(result.classes[0].methods).toContain("process");
      expect(result.classes[0].methods).toContain("_read_file");
      expect(result.classes[0].properties).toContain("name");
      expect(result.classes[0].properties).toContain("verbose");

      // Top-level functions
      expect(result.functions.some((f) => f.name === "create_processor")).toBe(
        true,
      );
      expect(result.functions.some((f) => f.name === "utility_func")).toBe(
        true,
      );

      // Exports (top-level defs)
      const exportNames = result.exports.map((e) => e.name);
      expect(exportNames).toContain("FileProcessor");
      expect(exportNames).toContain("create_processor");
      expect(exportNames).toContain("utility_func");

      // Call graph
      const calls = extractor.extractCallGraph(root);
      expect(calls.length).toBeGreaterThan(0);

      tree.delete();
      parser.delete();
    });
  });

  // ---- Nested definition index ----

  describe("symbols", () => {
    const symbolsOf = (src: string) => {
      const { tree, parser, root } = parse(src);
      const result = extractor.extractStructure(root);
      tree.delete();
      parser.delete();
      return result.symbols ?? [];
    };

    it("qualifies methods, closures, and closures inside closures", () => {
      const symbols = symbolsOf(`
class DispatchSession:
    def _schedule_worker(self):
        def _start():
            def _run():
                routine_scope()
            return _run
        return _start
`);
      const byQual = Object.fromEntries(symbols.map((s) => [s.qualname, s]));

      expect(byQual["DispatchSession"]?.kind).toBe("class");
      expect(byQual["DispatchSession._schedule_worker"]?.kind).toBe("method");
      expect(byQual["DispatchSession._schedule_worker._start"]?.kind).toBe("closure");

      // The exact shape that broke the wool call graph.
      const run = byQual["DispatchSession._schedule_worker._start._run"];
      expect(run?.kind).toBe("closure");
      expect(run?.name).toBe("_run");
      expect(run?.depth).toBe(3);
      expect(run?.parentQualname).toBe("DispatchSession._schedule_worker._start");
    });

    it("keeps a def inside `if:` at module scope with a bare qualname", () => {
      // `module -> if_statement -> block -> function_definition`. An `if` is not
      // a scope in Python, so the prefix must not advance — and the old
      // top-level-only loop missed such defs entirely.
      const symbols = symbolsOf(`
if True:
    def conditional():
        pass
`);
      const cond = symbols.find((s) => s.name === "conditional");
      expect(cond?.qualname).toBe("conditional");
      expect(cond?.depth).toBe(0);
      expect(cond?.kind).toBe("function");
    });

    it("treats methods of a class nested in a function as methods, not closures", () => {
      const symbols = symbolsOf(`
def factory():
    class Inner:
        def method(self):
            pass
    return Inner
`);
      const byQual = Object.fromEntries(symbols.map((s) => [s.qualname, s]));
      expect(byQual["factory.Inner"]?.kind).toBe("class");
      // parentIsClass must win over insideFunction, or every such method is
      // mislabelled a closure.
      expect(byQual["factory.Inner.method"]?.kind).toBe("method");
    });

    it("records async and params, and unwraps decorators without duplicating", () => {
      const symbols = symbolsOf(`
import functools

@functools.cache
async def fetch(url, timeout=5):
    pass
`);
      const fetch = symbols.filter((s) => s.name === "fetch");
      expect(fetch).toHaveLength(1);
      expect(fetch[0].isAsync).toBe(true);
      expect(fetch[0].params).toEqual(["url", "timeout"]);
      expect(fetch[0].exported).toBe(true);
    });

    it("leaves functions[] and classes[] untouched", () => {
      // The no-churn guarantee: fingerprint.ts maps functions[] directly, so
      // growing it would churn every Python fingerprint.
      const { tree, parser, root } = parse(`
class Only:
    def a(self):
        pass
    def b(self):
        pass
`);
      const result = extractor.extractStructure(root);
      expect(result.functions).toHaveLength(0);
      expect(result.classes).toHaveLength(1);
      expect((result.symbols ?? []).length).toBe(3);
      tree.delete();
      parser.delete();
    });

    it("collapses @overload stubs to the implementation", () => {
      // Each occurrence would otherwise become the same node id, and a graph
      // cannot hold duplicate ids. Found by a file-analyzer agent that had to
      // work around `do_dispatch` x3 and `WorkerProxy.__init__` x4 in wool.
      const symbols = symbolsOf(`
from typing import overload

@overload
def do_dispatch() -> bool: ...
@overload
def do_dispatch(value: bool) -> object: ...
def do_dispatch(value=None):
    if value is None:
        return _flag.get()
    return _scope(value)
`);
      const hits = symbols.filter((s) => s.name === "do_dispatch");
      expect(hits).toHaveLength(1);
      // The implementation, not a stub — it is the one with a real body.
      expect(hits[0].lineRange[1] - hits[0].lineRange[0]).toBeGreaterThan(1);
    });

    it("collapses overloaded methods inside a class", () => {
      const symbols = symbolsOf(`
from typing import overload

class WorkerProxy:
    @overload
    def __init__(self, *, discovery) -> None: ...
    @overload
    def __init__(self, *, workers) -> None: ...
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._started = False
`);
      const inits = symbols.filter((s) => s.qualname === "WorkerProxy.__init__");
      expect(inits).toHaveLength(1);
      expect(inits[0].kind).toBe("method");
    });

    it("emits symbols in source order", () => {
      const symbols = symbolsOf(`
def first():
    pass

class Second:
    def third(self):
        pass

def fourth():
    pass
`);
      const lines = symbols.map((s) => s.lineRange[0]);
      expect([...lines].sort((a, b) => a - b)).toEqual(lines);
    });
  });
});
