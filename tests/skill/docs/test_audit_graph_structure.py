"""Tests for audit_graph_structure.py — call-graph coverage of a knowledge graph.

The audit compares the graph's `calls` edges with call sites recovered from
the source. Its numbers are only worth reading if each call site lands in the
right bucket, and its per-symbol report exists to expose one failure: a real
caller the graph does not have.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

from tests.skill.docs.conftest import write_json

HELPER = "def helper():\n    return 1\n"


def function_id(rel: str, qualname: str) -> str:
    return f"function:{rel}:{qualname}"


def run_audit(module, monkeypatch, capsys, tmp_path: Path, sources: dict[str, str],
              nodes: list[str], calls: list[tuple[str, str]], extra=()) -> str:
    """Write a source tree and a graph, run main(), and return its stdout."""
    src = tmp_path / "src"
    for rel, text in sources.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    graph = write_json(tmp_path / "graph.json", {
        "nodes": [{"id": nid} for nid in nodes],
        "edges": [{"source": s, "target": t, "type": "calls"} for s, t in calls]
                 + [{"source": "x", "target": "y", "type": "imports"}],
    })
    monkeypatch.setattr(sys, "argv", [
        "audit_graph_structure.py", "--graph", str(graph), "--src-root", str(src),
        *extra])
    assert module.main() == 0
    return capsys.readouterr().out


CALLER = '''
    from pkg.b import helper

    def caller():
        return helper()
'''
CALLER_ID = function_id("pkg/a.py", "caller")
HELPER_ID = function_id("pkg/b.py", "helper")


# ── qualname_index / enclosing ────────────────────────────────────────────


def test_qualname_index_should_map_each_def_line_to_its_dotted_name(
    audit_graph_structure,
):
    """Test the line-to-qualname index.

    Given:
        A module with a function, a class, a method, and a function nested in
        the method.
    When:
        qualname_index indexes the tree.
    Then:
        It should map the line of each def to its dotted qualname.
    """
    # Arrange
    tree = ast.parse(
        "def top():\n    pass\n"
        "class Box:\n"
        "    def open(self):\n"
        "        def inner():\n            pass\n")

    # Act & assert
    assert audit_graph_structure.qualname_index(tree) == {
        1: "top", 3: "Box", 4: "Box.open", 5: "Box.open.inner"}


@pytest.mark.parametrize(
    ("line", "expected"),
    [(1, None), (3, "Box"), (5, "Box.open"), (7, "Box.open.inner"), (20, None)],
    ids=["before", "class-body", "method-body", "nested-body", "after"],
)
def test_enclosing_should_return_the_innermost_def_containing_the_line(
    audit_graph_structure, line, expected
):
    """Test attribution of a line to its innermost def.

    Given:
        Three nested def spans.
    When:
        enclosing looks up a line outside them, and inside each depth.
    Then:
        It should return the innermost def that contains the line, or None.
    """
    # Arrange
    defs = [(2, 10, "Box"), (4, 9, "Box.open"), (6, 8, "Box.open.inner")]

    # Act & assert
    assert audit_graph_structure.enclosing(line, defs) == expected


# ── main() — how each call site is counted ────────────────────────────────


def test_main_should_count_a_call_covered_when_the_graph_has_the_edge(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test the covered bucket.

    Given:
        A function calling an imported project function, and a graph with
        both nodes and the `calls` edge between them.
    When:
        main() audits the graph.
    Then:
        It should report one resolvable call site, covered.
    """
    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": CALLER, "pkg/b.py": HELPER},
                    [CALLER_ID, HELPER_ID], [(CALLER_ID, HELPER_ID)])

    # Assert
    assert "graph `calls` edges: 1" in out
    assert "resolvable call sites found in AST: 1" in out
    assert "covered by a graph edge: 1  (100%)" in out
    assert "MISSING from the graph:  0  (0%)" in out


def test_main_should_count_a_call_missing_when_the_graph_lacks_the_edge(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test the missing bucket.

    Given:
        The same call site, and a graph with both nodes and no `calls` edge.
    When:
        main() audits the graph.
    Then:
        It should report the call site missing and list its target among the
        most frequently missed.
    """
    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": CALLER, "pkg/b.py": HELPER},
                    [CALLER_ID, HELPER_ID], [])

    # Assert
    assert "MISSING from the graph:  1  (100%)" in out
    assert "  1  -> helper" in out


def test_main_should_count_a_call_covered_by_name_when_the_edge_targets_a_namesake(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test that coverage is by name and divergence is reported apart.

    Given:
        A call site whose callee this script binds to one project symbol, and
        a graph whose edge from that caller ends at another id of that name.
    When:
        main() audits the graph.
    Then:
        It should count the call covered and report one divergent target.
    """
    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": CALLER, "pkg/b.py": HELPER},
                    [CALLER_ID, HELPER_ID],
                    [(CALLER_ID, function_id("pkg/other.py", "helper"))])

    # Assert
    assert "covered by a graph edge: 1  (100%)" in out
    assert "divergent targets" in out and out.rstrip().endswith(": 1")


def test_main_should_set_aside_a_bare_name_that_is_neither_local_nor_imported(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test the unresolved bucket.

    Given:
        A function calling a bare name that some project module defines and
        this module neither defines nor imports.
    When:
        main() audits the graph.
    Then:
        It should count the call unresolved and keep it out of the total.
    """
    # Arrange
    caller = "def caller():\n    return helper()\n"

    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": caller, "pkg/b.py": HELPER},
                    [CALLER_ID, HELPER_ID], [])

    # Assert
    assert "resolvable call sites found in AST: 0" in out
    assert "unresolved (not counted):  1" in out


def test_main_should_attribute_a_closures_call_to_the_enclosing_graph_node(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test attribution through nested closures.

    Given:
        A call made from a closure two levels inside a method, and a graph
        that has a node for the method only, with a `calls` edge from it.
    When:
        main() audits the graph.
    Then:
        It should count the call covered and report it as originating inside
        a nested closure.
    """
    # Arrange
    source = '''
        from pkg.b import helper

        class Session:
            def schedule(self):
                def start():
                    def run():
                        return helper()
                    return run
                return start
    '''
    method = function_id("pkg/a.py", "Session.schedule")

    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": source, "pkg/b.py": HELPER},
                    [method, HELPER_ID], [(method, HELPER_ID)])

    # Assert
    assert "covered by a graph edge: 1  (100%)" in out
    assert "1 originate inside nested closures" in out


def test_main_should_count_a_call_missing_when_its_caller_has_no_node(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test a caller the graph does not model at all.

    Given:
        A call site whose calling function has no node in the graph.
    When:
        main() audits the graph.
    Then:
        It should count the call missing and say the caller has no node.
    """
    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": CALLER, "pkg/b.py": HELPER}, [HELPER_ID], [])

    # Assert
    assert "MISSING from the graph:  1  (100%)" in out
    assert "(of the missing, 1 have no node for the caller at all)" in out


def test_main_should_ignore_calls_outside_any_def_and_files_that_do_not_parse(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test what the audit leaves out of the count.

    Given:
        A module-level call to a project function, a call to a name no
        project module defines, and a second file with a syntax error.
    When:
        main() audits the graph.
    Then:
        It should find no resolvable call site and not raise.
    """
    # Arrange
    source = '''
        from pkg.b import helper

        VALUE = helper()

        def caller():
            return print("x")
    '''

    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": source, "pkg/b.py": HELPER, "pkg/bad.py": "def (:\n"},
                    [CALLER_ID, HELPER_ID], [])

    # Assert
    assert "resolvable call sites found in AST: 0" in out
    assert "unresolved (not counted):  0" in out


def test_main_should_apply_the_path_prefix_to_node_ids(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test a graph whose node ids carry a path prefix.

    Given:
        A graph whose ids are prefixed with a directory above the source
        root, and that prefix passed as --path-prefix.
    When:
        main() audits the graph.
    Then:
        It should match the caller to its node and count the call covered.
    """
    # Arrange
    caller = function_id("lib/src/pkg/a.py", "caller")
    helper = function_id("lib/src/pkg/b.py", "helper")

    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": CALLER, "pkg/b.py": HELPER},
                    [caller, helper], [(caller, helper)],
                    extra=("--path-prefix", "lib/src/"))

    # Assert
    assert "covered by a graph edge: 1  (100%)" in out


# ── main() --symbol ───────────────────────────────────────────────────────

TWO_CALLERS = '''
    from pkg.b import helper

    def first():
        return helper()

    def second():
        return helper()
'''


def test_main_should_list_each_caller_of_the_symbol_with_its_graph_status(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test the per-symbol report.

    Given:
        Two functions calling one symbol, and a graph with an edge from the
        first only.
    When:
        main() audits the graph with --symbol.
    Then:
        It should list the first caller as in the graph and the second as
        missing, each with its line.
    """
    # Arrange
    first = function_id("pkg/a.py", "first")
    second = function_id("pkg/a.py", "second")

    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": TWO_CALLERS, "pkg/b.py": HELPER},
                    [first, second, HELPER_ID], [(first, HELPER_ID)],
                    extra=("--symbol", "helper"))

    # Assert
    report = out.split("=== callers of `helper` ===")[1]
    assert "[in graph] pkg/a.py:first  (line 5)" in report
    assert "[MISSING ] pkg/a.py:second  (line 8)" in report


def test_main_should_list_a_caller_of_the_symbol_that_has_no_graph_node(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test the per-symbol report on the failure it exists to expose.

    Given:
        Two functions calling one symbol, and a graph that has a node and an
        edge for the first and no node at all for the second.
    When:
        main() audits the graph with --symbol.
    Then:
        It should list the second caller as missing, not omit it.
    """
    # Arrange
    first = function_id("pkg/a.py", "first")

    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/a.py": TWO_CALLERS, "pkg/b.py": HELPER},
                    [first, HELPER_ID], [(first, HELPER_ID)],
                    extra=("--symbol", "helper"))

    # Assert
    report = out.split("=== callers of `helper` ===")[1]
    assert "[in graph] pkg/a.py:first  (line 5)" in report
    assert "[MISSING ] pkg/a.py:second  (line 8)" in report


def test_main_should_say_so_when_the_symbol_has_no_caller(
    audit_graph_structure, monkeypatch, capsys, tmp_path
):
    """Test the per-symbol report for a symbol nothing calls.

    Given:
        A source tree in which no function calls the requested symbol.
    When:
        main() audits the graph with --symbol.
    Then:
        It should report that none were found in the AST.
    """
    # Act
    out = run_audit(audit_graph_structure, monkeypatch, capsys, tmp_path,
                    {"pkg/b.py": HELPER}, [HELPER_ID], [],
                    extra=("--symbol", "helper"))

    # Assert
    assert "=== callers of `helper` ===\n  none found in the AST" in out
