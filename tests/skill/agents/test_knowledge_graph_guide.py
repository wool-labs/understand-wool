"""Tests for agents/knowledge-graph-guide.md — its type tables against the schema.

An agent that enumerates node or edge types reads the guide's tables, not
`schema.ts`. The tables drifted once already: `claim` was added to the schema
and to a section of the same guide while the node table still said 16 types.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3] / "understand-anything-plugin"
GUIDE = PLUGIN / "agents" / "knowledge-graph-guide.md"
SCHEMA = PLUGIN / "packages" / "core" / "src" / "schema.ts"


def enum_after(text: str, anchor: str) -> list[str]:
    """The string members of the first `z.enum([...])` after `anchor`."""
    body = text.split(anchor, 1)[1]
    members = re.search(r"z\.enum\(\[(.*?)\]\)", body, re.S).group(1)
    return re.findall(r'"([^"]+)"', members)


def section(text: str, heading: str) -> tuple[str, str]:
    """The `### <heading> (...)` line's parenthesis, and the section body."""
    match = re.search(rf"^### {heading} \((.*?)\)\n(.*?)(?=^### |\Z)", text, re.M | re.S)
    return match.group(1), match.group(2)


def test_node_table_should_list_exactly_the_schema_node_types():
    """Test the guide's node-type table against GraphNodeSchema.

    Given:
        The guide's node-type table and the `type` enum of GraphNodeSchema.
    When:
        The first-column types and the heading's total are read from the guide.
    Then:
        It should list each schema node type once, and its total should equal
        the number of schema node types.
    """
    # Arrange
    schema = enum_after(SCHEMA.read_text(), "export const GraphNodeSchema")
    count, body = section(GUIDE.read_text(), "Node Types")

    # Act
    listed = re.findall(r"^\| `([^`]+)` \|", body, re.M)
    total = int(re.match(r"(\d+) total", count).group(1))

    # Assert
    assert sorted(listed) == sorted(schema)
    assert total == len(schema)


def test_edge_table_should_list_exactly_the_schema_edge_types():
    """Test the guide's edge-type table against EdgeTypeSchema.

    Given:
        The guide's edge-type table and EdgeTypeSchema.
    When:
        The backticked types in the table's rows and the heading's totals are
        read from the guide.
    Then:
        It should list each schema edge type once, its total should equal the
        number of schema edge types, and its category count should equal the
        number of rows.
    """
    # Arrange
    schema = enum_after(SCHEMA.read_text(), "export const EdgeTypeSchema")
    heading, body = section(GUIDE.read_text(), "Edge Types")
    rows = re.findall(r"^\| (?!Category|---)([^|]+) \| (.+) \|$", body, re.M)

    # Act
    listed = [t for _, types in rows for t in re.findall(r"`([^`]+)`", types)]
    total, categories = map(int, re.match(r"(\d+) total in (\d+) categories", heading).groups())

    # Assert
    assert sorted(listed) == sorted(schema)
    assert total == len(schema)
    assert categories == len(rows)
