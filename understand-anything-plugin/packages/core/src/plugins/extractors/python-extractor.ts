import type { StructuralAnalysis, CallGraphEntry, SymbolInfo } from "../../types.js";
import type { LanguageExtractor, TreeSitterNode } from "./types.js";
import { findChild, findChildren, hasChildOfType } from "./base-extractor.js";

/**
 * Extract parameter names from a Python `parameters` node.
 *
 * Handles: identifier (plain), typed_parameter, default_parameter,
 * typed_default_parameter, list_splat_pattern (*args),
 * dictionary_splat_pattern (**kwargs).
 */
function extractParams(paramsNode: TreeSitterNode | null): string[] {
  if (!paramsNode) return [];
  const params: string[] = [];

  for (let i = 0; i < paramsNode.childCount; i++) {
    const child = paramsNode.child(i);
    if (!child) continue;

    switch (child.type) {
      case "identifier":
        // Skip `self` and `cls` — they are implicit, not real parameters
        if (child.text !== "self" && child.text !== "cls") {
          params.push(child.text);
        }
        break;

      case "typed_parameter": {
        const ident = findChild(child, "identifier");
        if (ident && ident.text !== "self" && ident.text !== "cls") {
          params.push(ident.text);
        }
        break;
      }

      case "default_parameter": {
        const ident = findChild(child, "identifier");
        if (ident && ident.text !== "self" && ident.text !== "cls") {
          params.push(ident.text);
        }
        break;
      }

      case "typed_default_parameter": {
        const ident = findChild(child, "identifier");
        if (ident && ident.text !== "self" && ident.text !== "cls") {
          params.push(ident.text);
        }
        break;
      }

      case "list_splat_pattern": {
        const ident = findChild(child, "identifier");
        if (ident) params.push("*" + ident.text);
        break;
      }

      case "dictionary_splat_pattern": {
        const ident = findChild(child, "identifier");
        if (ident) params.push("**" + ident.text);
        break;
      }
    }
  }

  return params;
}

/**
 * Extract the return type annotation from a function_definition node.
 * Python AST has a `return_type` field (the `type` node after `->`) on function_definition.
 */
function extractReturnType(node: TreeSitterNode): string | undefined {
  const returnType = node.childForFieldName("return_type");
  if (returnType) {
    return returnType.text;
  }
  return undefined;
}

/**
 * True when a function body has no executable statements.
 *
 * Recognises the three shapes Python uses to declare-without-implementing:
 * `...` (Protocol members, `@overload` signatures), `pass`, and
 * `raise NotImplementedError`. A leading docstring is ignored.
 */
function isStubBody(node: TreeSitterNode): boolean {
  const body = node.childForFieldName("body");
  if (!body) return false;

  const statements: TreeSitterNode[] = [];
  for (let i = 0; i < body.childCount; i++) {
    const child = body.child(i);
    if (!child || child.type === "comment") continue;
    statements.push(child);
  }
  if (statements.length === 0) return true;

  // Drop a leading docstring.
  const meaningful = statements.filter((s, index) => {
    if (index !== 0 || s.type !== "expression_statement") return true;
    const inner = s.child(0);
    return !(inner && inner.type === "string");
  });
  if (meaningful.length === 0) return true;

  return meaningful.every((s) => {
    if (s.type === "pass_statement") return true;
    if (s.type === "expression_statement") {
      const inner = s.child(0);
      return Boolean(inner && inner.type === "ellipsis");
    }
    if (s.type === "raise_statement") return s.text.includes("NotImplementedError");
    return false;
  });
}

/**
 * Unwrap a `decorated_definition` to get the inner definition.
 * If the node is not a decorated_definition, returns the node itself.
 */
function unwrapDecorated(node: TreeSitterNode): TreeSitterNode {
  if (node.type === "decorated_definition") {
    const inner =
      findChild(node, "function_definition") ??
      findChild(node, "class_definition");
    if (inner) return inner;
  }
  return node;
}

/**
 * Python extractor for tree-sitter structural analysis and call graph extraction.
 *
 * Handles functions, classes, imports, exports, and call graphs for Python code.
 * Python has no formal export syntax, so all top-level function and class
 * definitions are treated as exports.
 */
export class PythonExtractor implements LanguageExtractor {
  readonly languageIds = ["python"];

  extractStructure(rootNode: TreeSitterNode): StructuralAnalysis {
    const functions: StructuralAnalysis["functions"] = [];
    const classes: StructuralAnalysis["classes"] = [];
    const imports: StructuralAnalysis["imports"] = [];
    const exports: StructuralAnalysis["exports"] = [];

    for (let i = 0; i < rootNode.childCount; i++) {
      const node = rootNode.child(i);
      if (!node) continue;

      // Unwrap decorated definitions to get the inner node
      const inner = unwrapDecorated(node);

      switch (inner.type) {
        case "function_definition":
          this.extractFunction(inner, functions);
          // Top-level functions are exports in Python
          this.addExport(inner, node, exports);
          break;

        case "class_definition":
          this.extractClass(inner, classes);
          // Top-level classes are exports in Python
          this.addExport(inner, node, exports);
          break;

        case "import_statement":
          this.extractImport(inner, imports);
          break;

        case "import_from_statement":
          this.extractFromImport(inner, imports);
          break;
      }
    }

    const walked: SymbolInfo[] = [];
    this.walkSymbols(rootNode, walked, "", 0, false, undefined, false);

    // Collapse repeated qualnames, keeping the last.
    //
    // `@overload` declares the same name several times — stub signatures first,
    // the real implementation last — so a file can yield `do_dispatch` three
    // times and `WorkerProxy.__init__` four. Each becomes the same node id, and
    // a graph cannot hold duplicate ids. Last-wins is right for both shapes this
    // takes in Python: the implementation follows its overloads, and a
    // conditional redefinition supersedes the earlier binding.
    const byQualname = new Map<string, SymbolInfo>();
    for (const symbol of walked) byQualname.set(symbol.qualname, symbol);

    const symbols = [...byQualname.values()].sort(
      (a, b) => a.lineRange[0] - b.lineRange[0],
    );

    return { functions, classes, imports, exports, symbols };
  }

  extractCallGraph(rootNode: TreeSitterNode): CallGraphEntry[] {
    const entries: CallGraphEntry[] = [];
    // Parallel stacks: the qualname segments, and what each segment is. Classes
    // are pushed too — without them a method reports as `mount` rather than
    // `Chain.mount`, which matches no graph node id.
    const scopeStack: string[] = [];
    const kindStack: SymbolInfo["kind"][] = [];

    const walkForCalls = (node: TreeSitterNode) => {
      let pushed = false;

      if (node.type === "function_definition" || node.type === "class_definition") {
        const nameNode = node.childForFieldName("name");
        if (nameNode) {
          const isClass = node.type === "class_definition";
          const parentIsClass = kindStack[kindStack.length - 1] === "class";
          scopeStack.push(nameNode.text);
          kindStack.push(
            isClass
              ? "class"
              : parentIsClass
                ? "method"
                : kindStack.length > 0
                  ? "closure"
                  : "function",
          );
          pushed = true;
        }
      }

      if (node.type === "call") {
        // `function` is a real field on the Python `call` node. The previous
        // children-scan could pick the wrong node for a non-trivial callee.
        const fnNode = node.childForFieldName("function");
        if (fnNode && scopeStack.length > 0) {
          let calleeName: string | undefined;
          let calleeReceiver: string | undefined;

          if (fnNode.type === "identifier") {
            calleeName = fnNode.text;
          } else if (fnNode.type === "attribute") {
            calleeName = fnNode.childForFieldName("attribute")?.text;
            calleeReceiver = fnNode.childForFieldName("object")?.text;
          }
          // Anything else (subscript, chained call, parenthesized expression) is
          // emitted with calleeName undefined — inherently unresolvable, but it
          // still belongs in the denominator when measuring coverage.

          entries.push({
            caller: scopeStack.join("."),
            callee: fnNode.text,
            lineNumber: node.startPosition.row + 1,
            callerName: scopeStack[scopeStack.length - 1],
            callerKind: kindStack[kindStack.length - 1],
            calleeName,
            calleeReceiver,
          });
        }
      }

      for (let i = 0; i < node.childCount; i++) {
        const child = node.child(i);
        if (child) walkForCalls(child);
      }

      if (pushed) {
        scopeStack.pop();
        kindStack.pop();
      }
    };

    walkForCalls(rootNode);

    return entries;
  }

  /**
   * Recursively index every definition in the file, nested ones included.
   *
   * Deliberately independent of `extractStructure`'s top-level loop, which stays
   * byte-identical so `functions[]`/`classes[]` — and therefore every Python
   * fingerprint — are unchanged.
   *
   * Two rules carry the weight:
   *
   *  - **Pass-through nodes do not advance the prefix.** `if True:` / `try:` /
   *    `with:` blocks are not scopes in Python, so a `def` inside one is still
   *    module-scope and must keep a bare qualname. The old top-level-only loop
   *    missed such defs entirely.
   *  - **`parentIsClass` is checked before `insideFunction`.** A class nested
   *    inside a function still yields *methods*, not closures; testing
   *    `insideFunction` first would mislabel every one of them.
   */
  private walkSymbols(
    node: TreeSitterNode,
    out: SymbolInfo[],
    prefix: string,
    depth: number,
    insideFunction: boolean,
    parentQualname: string | undefined,
    parentIsClass: boolean,
  ): void {
    for (let i = 0; i < node.childCount; i++) {
      const child = node.child(i);
      if (!child) continue;

      const inner = unwrapDecorated(child);

      if (inner.type === "function_definition" || inner.type === "class_definition") {
        const nameNode = inner.childForFieldName("name");
        if (!nameNode) continue;

        const isClass = inner.type === "class_definition";
        const qualname = prefix + nameNode.text;
        const kind: SymbolInfo["kind"] = isClass
          ? "class"
          : parentIsClass
            ? "method"
            : insideFunction
              ? "closure"
              : "function";

        out.push({
          qualname,
          name: nameNode.text,
          kind,
          parentQualname,
          lineRange: [inner.startPosition.row + 1, inner.endPosition.row + 1],
          params: isClass ? [] : extractParams(inner.childForFieldName("parameters")),
          returnType: isClass ? undefined : extractReturnType(inner),
          depth,
          isAsync: isClass ? undefined : hasChildOfType(inner, "async") || undefined,
          exported: depth === 0 || undefined,
          isStub: isClass ? undefined : isStubBody(inner) || undefined,
        });

        this.walkSymbols(
          inner,
          out,
          qualname + ".",
          depth + 1,
          isClass ? insideFunction : true,
          qualname,
          isClass,
        );
      } else {
        // Not a scope: keep prefix, depth, and parentage exactly as they are.
        this.walkSymbols(child, out, prefix, depth, insideFunction, parentQualname, parentIsClass);
      }
    }
  }

  // ---- Private helpers ----

  private extractFunction(
    node: TreeSitterNode,
    functions: StructuralAnalysis["functions"],
  ): void {
    const nameNode = node.childForFieldName("name");
    if (!nameNode) return;

    const paramsNode = node.childForFieldName("parameters");
    const params = extractParams(paramsNode ?? null);
    const returnType = extractReturnType(node);

    functions.push({
      name: nameNode.text,
      lineRange: [
        node.startPosition.row + 1,
        node.endPosition.row + 1,
      ],
      params,
      returnType,
    });
  }

  private extractClass(
    node: TreeSitterNode,
    classes: StructuralAnalysis["classes"],
  ): void {
    const nameNode = node.childForFieldName("name");
    if (!nameNode) return;

    const methods: string[] = [];
    const properties: string[] = [];

    const body = node.childForFieldName("body");
    if (body) {
      for (let i = 0; i < body.childCount; i++) {
        const member = body.child(i);
        if (!member) continue;

        // Methods: function_definition or decorated_definition wrapping a function_definition
        const innerMember = unwrapDecorated(member);
        if (innerMember.type === "function_definition") {
          const methodName = innerMember.childForFieldName("name");
          if (methodName) methods.push(methodName.text);
        }

        // Properties: type-annotated assignments at class body level
        // e.g., `name: str` or `value: int = 0`
        if (member.type === "expression_statement") {
          const assignment = findChild(member, "assignment");
          if (assignment) {
            // Check if this is a type-annotated class-level assignment (has `:` child = type annotation)
            const typeNode = findChild(assignment, "type");
            const nameIdent = findChild(assignment, "identifier");
            if (typeNode && nameIdent) {
              properties.push(nameIdent.text);
            }
          }
        }
      }
    }

    classes.push({
      name: nameNode.text,
      lineRange: [
        node.startPosition.row + 1,
        node.endPosition.row + 1,
      ],
      methods,
      properties,
    });
  }

  private extractImport(
    node: TreeSitterNode,
    imports: StructuralAnalysis["imports"],
  ): void {
    // `import os` or `import os.path`
    // Can have multiple: `import os, sys`
    const dottedNames = findChildren(node, "dotted_name");
    const aliasedImports = findChildren(node, "aliased_import");

    for (const dn of dottedNames) {
      imports.push({
        source: dn.text,
        specifiers: [dn.text],
        lineNumber: node.startPosition.row + 1,
      });
    }

    for (const ai of aliasedImports) {
      const dottedName = findChild(ai, "dotted_name");
      const alias = ai.children.find(
        (c) => c.type === "identifier",
      );
      if (dottedName) {
        imports.push({
          source: dottedName.text,
          specifiers: [alias ? alias.text : dottedName.text],
          lineNumber: node.startPosition.row + 1,
        });
      }
    }
  }

  private extractFromImport(
    node: TreeSitterNode,
    imports: StructuralAnalysis["imports"],
  ): void {
    // `from pathlib import Path` or `from typing import Optional, List`
    const moduleNode = node.childForFieldName("module_name");
    const source = moduleNode ? moduleNode.text : "";
    const moduleNodeId = moduleNode?.id;

    const specifiers: string[] = [];

    // Collect dotted_name specifiers (non-aliased)
    // Skip the module_name dotted_name (compare by node id, not reference)
    const allDottedNames = findChildren(node, "dotted_name");
    for (const dn of allDottedNames) {
      if (dn.id === moduleNodeId) continue;
      specifiers.push(dn.text);
    }

    // Collect aliased imports: `from foo import bar as baz`
    const aliasedImports = findChildren(node, "aliased_import");
    for (const ai of aliasedImports) {
      // The alias identifier follows the `as` keyword
      const alias = ai.children.find(
        (c) => c.type === "identifier",
      );
      if (alias) {
        specifiers.push(alias.text);
      }
    }

    // Handle wildcard imports: `from os import *`
    if (findChild(node, "wildcard_import")) {
      specifiers.push("*");
    }

    imports.push({
      source,
      specifiers,
      lineNumber: node.startPosition.row + 1,
    });
  }

  private addExport(
    inner: TreeSitterNode,
    outer: TreeSitterNode,
    exports: StructuralAnalysis["exports"],
  ): void {
    const nameNode = inner.childForFieldName("name");
    if (nameNode) {
      exports.push({
        name: nameNode.text,
        lineNumber: outer.startPosition.row + 1,
      });
    }
  }
}
