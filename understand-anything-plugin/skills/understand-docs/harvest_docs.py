#!/usr/bin/env python3
"""harvest_docs.py — extract documentation units and run deterministic field checks.

Stage 0 of the documentation claim graph (see CLAIM-GRAPH-PLAN.md). No LLM, no
embeddings, no network, no third-party dependencies. Two jobs:

  1. HARVEST — walk tracked Python sources, emit every docstring as a `DocUnit`
     with a content-addressed ID, parsing reStructuredText info field lists.
  2. E0 — check each field bidirectionally against the AST:
       incorrectness  the docs say something the code contradicts
       omission       the code does something the docs never mention

E0 emits *candidates*, never verdicts. Its checks are field-level; a real claim
may be a fraction of a field (`:raises ValueError: if data is not 16 bytes or is
NULL` is one field and two assertions), so a flagged field marks every claim
later derived from it for review rather than deciding anything.

Suppression rules exist because a naive implementation is mostly false
positives. Measured on wool: 55 raw flags, of which 16 were Protocol stubs and
~30 were transitively-raised exceptions. See `--explain` for per-suppression
counts.

Usage:
    python3 harvest_docs.py <repo-root> [--src PATH]... [--out DIR] [--explain]

Output (deterministic; re-running on unchanged input is byte-identical):
    <out>/doc-units.json
    <out>/e0-candidates.json
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

SCHEMA_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Repo conventions
# ---------------------------------------------------------------------------


def resolve_ua_dir(root: Path) -> Path:
    """Return the project data directory, honouring the legacy name if present."""
    legacy = root / ".understand-anything"
    return legacy if legacy.is_dir() else root / ".ua"


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    """Canonical form for identity hashing.

    NFC, casefold, collapse internal whitespace, strip surrounding punctuation.
    Deliberately conservative: too aggressive and distinct claims collide onto
    one ID and one is silently lost.
    """
    text = unicodedata.normalize("NFC", text).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t.,;:!?-—–")


def git_tracked(root: Path, prefix: str) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", prefix],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(root / line for line in out.split("\n") if line.endswith(".py"))


# ---------------------------------------------------------------------------
# reStructuredText info field lists
# ---------------------------------------------------------------------------

# `:name [argument]: body`. Sphinx calls this an info field list. Distinct from
# a directive (`.. name:: args`), which is a different construct entirely.
FIELD_RE = re.compile(
    r"^(?P<indent>[ \t]*):(?P<kind>param|parameter|arg|argument|key|keyword"
    r"|returns?|rtype|raises?|raise|except|exception|yields?|ivar|cvar|vartype|type)"
    r"(?P<arg>\s+[^:]*?)?:(?P<rest>.*)$"
)

KIND_ALIASES = {
    "parameter": "param",
    "arg": "param",
    "argument": "param",
    "key": "param",
    "keyword": "param",
    "return": "returns",
    "raise": "raises",
    "except": "raises",
    "exception": "raises",
    "yield": "yields",
}

DIRECTIVE_RE = re.compile(r"^[ \t]*\.\.[ \t]+(?P<name>[a-z][a-z0-9-]*)::(?P<rest>.*)$")


class Field:
    """One entry in an info field list. May yield more than one claim."""

    __slots__ = ("kind", "argument", "body", "line_offset", "index")

    def __init__(
        self, kind: str, argument: Optional[str], body: str, line_offset: int, index: int
    ) -> None:
        self.kind = kind
        self.argument = argument
        self.body = body
        self.line_offset = line_offset
        self.index = index

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "argument": self.argument,
            "body": self.body,
            "lineOffset": self.line_offset,
            "fieldIndex": self.index,
        }


def parse_fields(docstring: str) -> tuple[list[Field], list[dict[str, Any]]]:
    """Split a docstring into info fields and directives.

    Field bodies may span paragraphs. A body ends at the next field marker at
    the same-or-lesser indent, or at a non-blank line at lesser-or-equal indent
    that is not itself a field. Stopping at the first blank line is wrong and
    silently discards the longest bodies in a corpus.
    """
    lines = docstring.split("\n")
    fields: list[Field] = []
    directives: list[dict[str, Any]] = []
    i = 0
    index = 0

    while i < len(lines):
        directive = DIRECTIVE_RE.match(lines[i])
        if directive:
            directives.append(
                {
                    "name": directive.group("name"),
                    "argument": directive.group("rest").strip() or None,
                    "lineOffset": i,
                }
            )
            i += 1
            continue

        match = FIELD_RE.match(lines[i])
        if not match:
            i += 1
            continue

        indent = len(match.group("indent").expandtabs(4))
        kind = match.group("kind")
        kind = KIND_ALIASES.get(kind, kind)
        argument = (match.group("arg") or "").strip() or None
        start = i
        body_parts = [match.group("rest").strip()]
        i += 1

        while i < len(lines):
            line = lines[i]
            if not line.strip():
                body_parts.append("")
                i += 1
                continue
            line_indent = len(line) - len(line.lstrip().rjust(len(line.expandtabs(4))))
            line_indent = len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())
            if line_indent <= indent:
                break
            body_parts.append(line.strip())
            i += 1

        body = re.sub(r"\s*\n\s*\n\s*", "\n\n", "\n".join(body_parts)).strip()
        body = re.sub(r"(?<!\n)\n(?!\n)", " ", body)
        fields.append(Field(kind, argument, body, start, index))
        index += 1

    return fields, directives


# ---------------------------------------------------------------------------
# Doc-unit splitting
# ---------------------------------------------------------------------------

# Above this, a docstring is split at reST structure boundaries. Measured on wool
# (CLAIM-GRAPH-PLAN §1.8): doc units under ~2000 chars reconcile at 78% agreement
# across 5 independent extraction passes, units above it at 52%. A 7KB docstring
# yields ~160 claim clusters and five agents will never segment 160 assertions the
# same way — that is a decomposition problem, not a matching problem, so no
# similarity threshold fixes it. 24 of wool's 429 doc units (5.6%) exceed this and
# carry 32% of all documentation text.
SPLIT_THRESHOLD = 1800
# Never emit a sliver; fold it into the previous part instead.
MIN_PART_CHARS = 300


def segment_blocks(lines: list[str]) -> list[tuple[str, int, int]]:
    """Atomic blocks of a docstring as (kind, start_line, end_line_exclusive).

    A block is never split. Fields and directives own their continuation lines;
    everything else is a blank-line-separated paragraph.
    """
    blocks: list[tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue

        directive = DIRECTIVE_RE.match(lines[i])
        field = FIELD_RE.match(lines[i])
        if directive or field:
            indent = len(lines[i].expandtabs(4)) - len(lines[i].expandtabs(4).lstrip())
            start = i
            i += 1
            while i < len(lines):
                if not lines[i].strip():
                    i += 1
                    continue
                cur = len(lines[i].expandtabs(4)) - len(lines[i].expandtabs(4).lstrip())
                if cur <= indent and (FIELD_RE.match(lines[i]) or DIRECTIVE_RE.match(lines[i])
                                      or cur < indent):
                    break
                if cur <= indent:
                    break
                i += 1
            blocks.append(("field" if field else "directive", start, i))
            continue

        start = i
        while i < len(lines) and lines[i].strip() \
                and not FIELD_RE.match(lines[i]) and not DIRECTIVE_RE.match(lines[i]):
            i += 1
        blocks.append(("prose", start, i))
    return blocks


def split_docstring(text: str) -> list[tuple[int, int]]:
    """Line ranges (start, end_exclusive) for each part of a docstring.

    Returns a single range when the docstring is under threshold. Packs atomic
    blocks greedily; a block larger than the cap on its own becomes its own part
    rather than being cut mid-sentence.
    """
    if len(text) <= SPLIT_THRESHOLD:
        return [(0, len(text.split("\n")))]

    lines = text.split("\n")
    blocks = segment_blocks(lines)
    if len(blocks) <= 1:
        return [(0, len(lines))]

    def block_chars(b: tuple[str, int, int]) -> int:
        return sum(len(lines[i]) + 1 for i in range(b[1], b[2]))

    parts: list[list[tuple[str, int, int]]] = [[]]
    acc = 0
    for block in blocks:
        size = block_chars(block)
        if acc and acc + size > SPLIT_THRESHOLD:
            parts.append([])
            acc = 0
        parts[-1].append(block)
        acc += size

    # Fold a trailing sliver back into its predecessor.
    if len(parts) > 1 and sum(block_chars(b) for b in parts[-1]) < MIN_PART_CHARS:
        parts[-2].extend(parts.pop())

    return [(p[0][1], p[-1][2]) for p in parts if p]


# ---------------------------------------------------------------------------
# AST helpers — the suppression rules
# ---------------------------------------------------------------------------

FuncDef = (ast.FunctionDef, ast.AsyncFunctionDef)


def decorator_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def is_stub(node: ast.AST) -> bool:
    """A body of `...`, `pass`, or `raise NotImplementedError` promises nothing."""
    body = [
        stmt
        for stmt in getattr(node, "body", [])
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    ]
    if not body:
        return True
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
            continue
        if isinstance(stmt, ast.Raise):
            exc = stmt.exc
            name = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            if name == "NotImplementedError":
                continue
        return False
    return True


def protocol_classes(tree: ast.Module) -> set[str]:
    """Names of classes in this module that are Protocols, transitively.

    Structural conformance is a type checker's job and is not attempted. Base
    detection alone suppresses every Protocol false positive measured on wool.
    """
    direct: set[str] = set()
    bases: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        names = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                names.append(base.id)
            elif isinstance(base, ast.Attribute):
                names.append(base.attr)
        bases[node.name] = names
        if "Protocol" in names:
            direct.add(node.name)

    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name in direct:
                continue
            if any(parent in direct for parent in parents):
                direct.add(name)
                changed = True
    return direct


def overloaded_names(parent: ast.AST) -> set[str]:
    """Function names in this scope that carry `@overload` declarations.

    The *implementation* carries no decorator — the overloads are preceding
    sibling declarations of the same name. A checker that inspects only the
    flagged function's own decorators will miss this.
    """
    names: set[str] = set()
    for stmt in getattr(parent, "body", []):
        if isinstance(stmt, FuncDef) and "overload" in decorator_names(stmt):
            names.add(stmt.name)
    return names


def signature_params(node: ast.AST) -> list[str]:
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        names.append(args.vararg.arg)
    if args.kwarg:
        names.append(args.kwarg.arg)
    return [n for n in names if n not in ("self", "cls")]


def direct_raises(node: ast.AST) -> set[str]:
    """Exception *class* names raised syntactically in this body.

    `raise some_local_variable` re-raises are excluded — a naive checker counts
    them as types and inflates the flag count.
    """
    found: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Raise) or child.exc is None:
            continue
        exc = child.exc
        name = None
        if isinstance(exc, ast.Call):
            target = exc.func
            name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
        elif isinstance(exc, ast.Name):
            name = exc.id
        elif isinstance(exc, ast.Attribute):
            name = exc.attr
        if name and name[:1].isupper():
            found.add(name)
    return found


def called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def returns_value(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, FuncDef) and child is not node:
            continue
        if isinstance(child, ast.Return) and child.value is not None:
            if not (isinstance(child.value, ast.Constant) and child.value.value is None):
                return True
    return False


def is_generator(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, FuncDef) and child is not node:
            continue
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
    return False


def visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "dunder"
    if name.startswith("_"):
        return "private"
    return "public"


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


class Scope:
    """A documented module, class, or function."""

    __slots__ = ("kind", "name", "qualname", "node", "parent", "docstring", "lineno", "doc_range")

    def __init__(self, kind, name, qualname, node, parent, docstring, lineno, doc_range=None):
        self.kind = kind
        self.name = name
        self.qualname = qualname
        self.node = node
        self.parent = parent
        self.docstring = docstring
        self.lineno = lineno
        self.doc_range = doc_range


def docstring_range(node: ast.AST) -> Optional[tuple[int, int]]:
    """Line span of the docstring literal itself, for `git blame`."""
    body = getattr(node, "body", [])
    if not body:
        return None
    first = body[0]
    if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)):
        return (first.lineno, first.end_lineno or first.lineno)
    return None


def walk_scopes(tree: ast.Module, module_name: str) -> Iterator[Scope]:
    def descend(node: ast.AST, prefix: str, parent: Optional[ast.AST]) -> Iterator[Scope]:
        for stmt in getattr(node, "body", []):
            if isinstance(stmt, (ast.ClassDef, *FuncDef)):
                kind = "class" if isinstance(stmt, ast.ClassDef) else "function"
                qual = f"{prefix}.{stmt.name}" if prefix else stmt.name
                doc = ast.get_docstring(stmt, clean=True)
                if doc:
                    yield Scope(kind, stmt.name, qual, stmt, node, doc, stmt.lineno,
                                docstring_range(stmt))
                yield from descend(stmt, qual, node)

    module_doc = ast.get_docstring(tree, clean=True)
    if module_doc:
        yield Scope("module", module_name, module_name, tree, None, module_doc, 1,
                    docstring_range(tree))
    yield from descend(tree, "", None)


def harvest_file(path: Path, root: Path) -> tuple[list[dict[str, Any]], list[Scope], ast.Module]:
    rel = path.relative_to(root).as_posix()
    source = path.read_text(errors="ignore")
    tree = ast.parse(source)
    module_name = rel.replace("/", ".").removesuffix(".py")

    units: list[dict[str, Any]] = []
    scopes: list[Scope] = []
    for scope in walk_scopes(tree, module_name):
        fields, directives = parse_fields(scope.docstring)
        lines = scope.docstring.split("\n")
        parts = split_docstring(scope.docstring)
        # The parent ID is the whole docstring, so it stays stable whether or not
        # the split threshold moves. Part IDs hash their own text, so a part is a
        # first-class content-addressed unit rather than a positional slice.
        # `attachedTo` is part of the identity, not just metadata: two distinct
        # symbols in one file can carry byte-identical docstrings (wool has
        # `WorkerLike.metadata` and `Worker.metadata`), and hashing text alone
        # collides them into one unit. It is stable under reformatting, so it
        # costs nothing against the content-addressing rationale.
        attached = f"{scope.kind}:{rel}:{scope.qualname}"
        parent_id = "docunit:" + content_hash(
            f"{attached}:docstring:{normalize(scope.docstring)}"
        )
        summary = next((ln.strip() for ln in lines if ln.strip()), "")

        for index, (start, end) in enumerate(parts):
            text = "\n".join(lines[start:end]).strip("\n")
            if not text.strip():
                continue
            part_fields = [f for f in fields if start <= f.line_offset < end]
            part_directives = [d for d in directives if start <= d["lineOffset"] < end]
            single = len(parts) == 1
            unit_id = parent_id if single else (
                "docunit:" + content_hash(f"{attached}:docstring:{index}:{normalize(text)}")
            )
            doc_range = None
            if scope.doc_range:
                base = scope.doc_range[0]
                doc_range = [base + start, min(base + end - 1, scope.doc_range[1])]

            units.append(
                {
                    "id": unit_id,
                    "kind": "docstring",
                    "sourceFile": rel,
                    "sourceLine": scope.lineno,
                    "docLineRange": doc_range,
                    "attachedTo": attached,
                    "attachedKind": scope.kind,
                    "visibility": visibility(scope.name),
                    "text": text,
                    # Later parts lose the opening summary line, so carry it as
                    # orientation — a claim extracted from part 4 still needs to
                    # know what the docstring is about.
                    "context": None if (single or index == 0) else summary,
                    "parentDocUnitId": None if single else parent_id,
                    "partIndex": None if single else index,
                    "partCount": None if single else len(parts),
                    # fieldIndex stays numbered against the whole docstring so
                    # ClaimMeta.fieldRef still resolves to the original field list.
                    "fields": [f.to_json() for f in part_fields],
                    "directives": part_directives,
                }
            )
            scopes.append(scope)
    return units, scopes, tree


# ---------------------------------------------------------------------------
# E0 — bidirectional field checks
# ---------------------------------------------------------------------------


class Candidate:
    __slots__ = ("check", "direction", "doc_unit", "file", "line", "symbol", "detail", "field_index")

    def __init__(self, check, direction, doc_unit, file, line, symbol, detail, field_index=None):
        self.check = check
        self.direction = direction
        self.doc_unit = doc_unit
        self.file = file
        self.line = line
        self.symbol = symbol
        self.detail = detail
        self.field_index = field_index

    def sort_key(self):
        return (self.file, self.line, self.check, self.direction, self.detail)

    def to_json(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "direction": self.direction,
            "docUnitId": self.doc_unit,
            "sourceFile": self.file,
            "sourceLine": self.line,
            "symbol": self.symbol,
            "fieldIndex": self.field_index,
            "detail": self.detail,
            "status": "needs_review",
        }


def check_file(
    path: Path,
    root: Path,
    units: list[dict[str, Any]],
    scopes: list[Scope],
    tree: ast.Module,
    stats: dict[str, int],
) -> list[Candidate]:
    rel = path.relative_to(root).as_posix()
    protocols = protocol_classes(tree)

    # Module-local function bodies, for depth-1 transitive `:raises` resolution.
    local_raises: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, FuncDef):
            local_raises.setdefault(node.name, set()).update(direct_raises(node))

    enclosing: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                enclosing[id(stmt)] = node.name

    out: list[Candidate] = []

    # A split docstring contributes several doc units for one function. E0 is a
    # per-function check against the AST, so it must run once per scope with the
    # *whole* docstring's field list — checking a part in isolation would report
    # a `:param:` that lives in another part as omitted.
    merged: dict[int, dict[str, Any]] = {}
    for unit, scope in zip(units, scopes):
        entry = merged.setdefault(
            id(scope.node), {"scope": scope, "unit": unit, "fields": []})
        entry["fields"].extend(unit["fields"])
        if unit.get("partIndex") in (None, 0):
            entry["unit"] = unit

    for entry in merged.values():
        scope, unit = entry["scope"], entry["unit"]
        if scope.kind != "function":
            continue

        # Omission checks run on every documented function. Only the
        # incorrectness checks need a field to be wrong about — gating the
        # whole block on `fields` would skip the prose-only docstrings, which
        # is exactly where undocumented returns and raises live.
        fields = [Field(**{
            "kind": f["kind"], "argument": f["argument"], "body": f["body"],
            "line_offset": f["lineOffset"], "index": f["fieldIndex"],
        }) for f in sorted(entry["fields"], key=lambda x: x["fieldIndex"])]

        node = scope.node
        symbol = scope.qualname
        line = scope.lineno
        owner = enclosing.get(id(node))

        # --- suppression gates -------------------------------------------------
        if owner and owner in protocols:
            stats["suppressed_protocol"] += 1
            continue
        if is_stub(node):
            stats["suppressed_stub"] += 1
            continue
        is_overloaded = scope.name in overloaded_names(scope.parent) if scope.parent else False
        if is_overloaded:
            stats["suppressed_overload"] += 1

        params = signature_params(node)
        documented_params = {
            f.argument.lstrip("*") for f in fields if f.kind == "param" and f.argument
        }
        kinds = {f.kind for f in fields}

        # --- incorrectness -----------------------------------------------------
        if not is_overloaded:
            for field in fields:
                if field.kind != "param" or not field.argument:
                    continue
                name = field.argument.lstrip("*")
                if name not in params:
                    out.append(Candidate(
                        "param", "incorrect", unit["id"], rel, line, symbol,
                        f"documented parameter {name!r} is not in the signature "
                        f"(signature: {', '.join(params) or 'none'})",
                        field.index))

        if "returns" in kinds and not returns_value(node) and not is_generator(node):
            out.append(Candidate(
                "returns", "incorrect", unit["id"], rel, line, symbol,
                "documents :returns: but the body returns no value"))

        if "yields" in kinds and not is_generator(node):
            out.append(Candidate(
                "yields", "incorrect", unit["id"], rel, line, symbol,
                "documents :yields: but the body is not a generator"))

        raised = direct_raises(node)
        for field in fields:
            if field.kind != "raises" or not field.argument:
                continue
            exc = field.argument.strip().split(".")[-1]
            if exc in raised:
                continue
            # depth-1, same-module resolution
            reachable = set()
            unresolved = False
            for callee in called_names(node):
                if callee in local_raises:
                    reachable |= local_raises[callee]
                else:
                    unresolved = True
            if exc in reachable:
                stats["suppressed_transitive"] += 1
                continue
            if unresolved:
                # A callee we cannot resolve may raise it. Suppress rather than
                # accuse: precision matters more than recall for refutations.
                stats["suppressed_unresolved_callee"] += 1
                continue
            out.append(Candidate(
                "raises", "incorrect", unit["id"], rel, line, symbol,
                f"documents :raises {exc}: but it is not raised here or by any "
                f"resolvable callee", field.index))

        # --- omission ----------------------------------------------------------
        if documented_params and not is_overloaded:
            for name in params:
                if name not in documented_params:
                    out.append(Candidate(
                        "param", "omitted", unit["id"], rel, line, symbol,
                        f"parameter {name!r} is in the signature but undocumented"))

        if "returns" not in kinds and returns_value(node) and not is_generator(node):
            vis = visibility(scope.name)
            if vis == "public":
                out.append(Candidate(
                    "returns", "omitted", unit["id"], rel, line, symbol,
                    "returns a value but has no :returns: field"))
            else:
                stats[f"suppressed_visibility_{vis}"] += 1

        if "yields" not in kinds and is_generator(node) and visibility(scope.name) == "public":
            out.append(Candidate(
                "yields", "omitted", unit["id"], rel, line, symbol,
                "is a generator but has no :yields: field"))

        documented_raises = {
            f.argument.strip().split(".")[-1] for f in fields if f.kind == "raises" and f.argument
        }
        for exc in sorted(raised - documented_raises):
            out.append(Candidate(
                "raises", "omitted", unit["id"], rel, line, symbol,
                f"raises {exc} but has no :raises {exc}: field"))

    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--src", action="append", default=None,
                        help="tracked path prefix to scan (repeatable)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--explain", action="store_true",
                        help="print suppression counts to stderr")
    args = parser.parse_args()

    root = args.root.resolve()
    prefixes = args.src or ["."]
    out_dir = args.out or (resolve_ua_dir(root) / "docs")
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for prefix in prefixes:
        paths.extend(git_tracked(root, prefix))
    paths = sorted({p for p in paths if "_pb2" not in p.name})

    stats = {
        "suppressed_protocol": 0,
        "suppressed_stub": 0,
        "suppressed_overload": 0,
        "suppressed_transitive": 0,
        "suppressed_unresolved_callee": 0,
        "suppressed_visibility_private": 0,
        "suppressed_visibility_dunder": 0,
        "parse_errors": 0,
    }

    all_units: list[dict[str, Any]] = []
    all_candidates: list[Candidate] = []

    for path in paths:
        try:
            units, scopes, tree = harvest_file(path, root)
        except SyntaxError:
            stats["parse_errors"] += 1
            continue
        all_units.extend(units)
        all_candidates.extend(check_file(path, root, units, scopes, tree, stats))

    all_units.sort(key=lambda u: (u["sourceFile"], u["sourceLine"], u["id"]))
    all_candidates.sort(key=lambda c: c.sort_key())

    field_count = sum(len(u["fields"]) for u in all_units)
    directive_count = sum(len(u["directives"]) for u in all_units)

    write_json(out_dir / "doc-units.json", {
        "schemaVersion": SCHEMA_VERSION,
        "root": str(root),
        "sources": prefixes,
        "stats": {
            "files": len(paths),
            "docUnits": len(all_units),
            "fields": field_count,
            "directives": directive_count,
        },
        "docUnits": all_units,
    })

    by_check: dict[str, int] = {}
    for cand in all_candidates:
        key = f"{cand.check}:{cand.direction}"
        by_check[key] = by_check.get(key, 0) + 1

    write_json(out_dir / "e0-candidates.json", {
        "schemaVersion": SCHEMA_VERSION,
        "root": str(root),
        "note": "candidates for review, not verdicts; field-level checks, claim-level adjudication",
        "stats": {"total": len(all_candidates), "byCheck": dict(sorted(by_check.items()))},
        "suppressions": {k: v for k, v in sorted(stats.items()) if v},
        "candidates": [c.to_json() for c in all_candidates],
    })

    print(f"{len(paths)} files → {len(all_units)} doc units, "
          f"{field_count} fields, {directive_count} directives")
    print(f"{len(all_candidates)} E0 candidates")
    for key, count in sorted(by_check.items()):
        print(f"    {key:22} {count}")
    if args.explain:
        print("suppressions:", file=sys.stderr)
        for key, count in sorted(stats.items()):
            if count:
                print(f"    {key:34} {count}", file=sys.stderr)
    print(f"→ {out_dir}")
    return 0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Canonical serialization: sorted keys, fixed separators, trailing newline."""
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
