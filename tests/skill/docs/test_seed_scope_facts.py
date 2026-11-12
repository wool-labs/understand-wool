"""Tests for seed_scope_facts.py — subsystems and the vocabulary seed.

`scope-facts.json` tells later stages which claims are about the same thing.
Two derivations feed it: a partition of files into subsystems, and parameter
names documented with different meanings in more than one subsystem.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.skill.docs.conftest import write_json


def unit(source_file: str, *params: tuple[str, str]) -> dict:
    """A doc unit carrying one `:param:` field per (name, body) pair."""
    return {
        "id": f"docunit:{source_file}:{len(params)}",
        "sourceFile": source_file,
        "fields": [{"kind": "param", "argument": name, "body": body}
                   for name, body in params],
    }


# ── tokens / collapse ─────────────────────────────────────────────────────


def test_tokens_should_keep_content_words_only(seed_scope_facts):
    """Test the token set two field bodies are compared by.

    Given:
        A body mixing stopwords, one- and two-letter words, mixed case, and
        punctuation.
    When:
        tokens tokenises it.
    Then:
        It should return the lower-cased content words of three letters or
        more.
    """
    # Act
    found = seed_scope_facts.tokens("The Retry-Limit of a pool is 3, or `max_retries`.")

    # Assert
    assert found == {"retry", "limit", "pool", "max_retries"}


def test_collapse_should_reduce_whitespace_runs_to_single_spaces(seed_scope_facts):
    """Test body normalisation before comparison.

    Given:
        A body with leading space, a newline, and a run of spaces.
    When:
        collapse normalises it.
    Then:
        It should return the words separated by single spaces.
    """
    # Act & assert
    assert seed_scope_facts.collapse("  the retry\n   limit  ") == "the retry limit"


# ── subsystems_from_graph ─────────────────────────────────────────────────


def graph_with_layers(path: Path, layers: list[dict]) -> Path:
    """A graph with three file nodes and one node that has no file path."""
    return write_json(path, {
        "nodes": [
            {"id": "file:pkg/discovery/lan.py", "filePath": "pkg/discovery/lan.py"},
            {"id": "file:pkg/discovery/base.py", "filePath": "pkg/discovery/base.py"},
            {"id": "file:pkg/worker/pool.py", "filePath": "pkg/worker/pool.py"},
            {"id": "concept:retries"},
        ],
        "layers": layers,
    })


def test_subsystems_from_graph_should_return_one_subsystem_per_layer(
    seed_scope_facts, tmp_path
):
    """Test the preferred derivation.

    Given:
        A graph with two layers, each listing file nodes, one also listing a
        node with no file path and a node id the graph does not hold.
    When:
        subsystems_from_graph reads it.
    Then:
        It should return each layer as a subsystem with its name, description,
        and sorted file paths, ignoring the two ids that resolve to no file.
    """
    # Arrange
    graph = graph_with_layers(tmp_path / "graph.json", [
        {"id": "layer:discovery", "name": "Discovery", "description": "Finds workers.",
         "nodeIds": ["file:pkg/discovery/lan.py", "file:pkg/discovery/base.py",
                     "concept:retries", "file:gone.py"]},
        {"id": "layer:worker", "nodeIds": ["file:pkg/worker/pool.py"]},
    ])

    # Act
    subsystems = seed_scope_facts.subsystems_from_graph(graph, "")

    # Assert
    assert subsystems == {
        "layer:discovery": {
            "id": "layer:discovery", "name": "Discovery",
            "responsibility": "Finds workers.",
            "paths": ["pkg/discovery/base.py", "pkg/discovery/lan.py"],
            "source": "knowledge-graph.layers",
        },
        "layer:worker": {
            "id": "layer:worker", "name": "layer:worker", "responsibility": "",
            "paths": ["pkg/worker/pool.py"], "source": "knowledge-graph.layers",
        },
    }


@pytest.mark.parametrize("case", ["missing-file", "no-layers", "layers-without-files"])
def test_subsystems_from_graph_should_return_none_when_no_layer_is_usable(
    seed_scope_facts, tmp_path, case
):
    """Test each condition that sends the caller to the fallback.

    Given:
        No graph file, a graph with no layers, or a graph whose only layer
        lists no file node.
    When:
        subsystems_from_graph reads it.
    Then:
        It should return None.
    """
    # Arrange
    graph = tmp_path / "graph.json"
    if case == "no-layers":
        graph_with_layers(graph, [])
    elif case == "layers-without-files":
        graph_with_layers(graph, [{"id": "layer:x", "nodeIds": ["concept:retries"]}])

    # Act & assert
    assert seed_scope_facts.subsystems_from_graph(graph, "") is None


# ── subsystems_from_directories ───────────────────────────────────────────


def test_subsystems_from_directories_should_group_by_the_path_below_the_prefix(
    seed_scope_facts,
):
    """Test the directory fallback's grouping rule.

    Given:
        Files three levels deep, two levels deep, and at the top of the source
        prefix, and one file outside the prefix.
    When:
        subsystems_from_directories groups them.
    Then:
        It should key deep files by their first two segments, shallow files by
        their directory, top-level files as `root`, and leave the outside
        file's path unstripped.
    """
    # Arrange
    files = [
        "src/pkg/runtime/worker/pool.py",
        "src/pkg/runtime/worker/proxy.py",
        "src/pkg/runtime/context.py",
        "src/pkg/errors.py",
        "tools/gen.py",
    ]

    # Act
    subsystems = seed_scope_facts.subsystems_from_directories(files, "src/pkg/")

    # Assert
    assert {key: sub["paths"] for key, sub in subsystems.items()} == {
        "runtime/worker": ["src/pkg/runtime/worker/pool.py",
                           "src/pkg/runtime/worker/proxy.py"],
        "runtime": ["src/pkg/runtime/context.py"],
        "root": ["src/pkg/errors.py"],
        "tools": ["tools/gen.py"],
    }
    assert subsystems["runtime/worker"]["id"] == "runtime-worker"
    assert {sub["source"] for sub in subsystems.values()} == {"directory-fallback"}
    assert {sub["responsibility"] for sub in subsystems.values()} == {""}


# ── seed_vocabulary ───────────────────────────────────────────────────────

SUBSYSTEM_OF = {"a/x.py": "a", "a/y.py": "a", "b/x.py": "b"}


def test_seed_vocabulary_should_flag_a_name_documented_differently_across_subsystems(
    seed_scope_facts,
):
    """Test the vocabulary-clash derivation.

    Given:
        A parameter name documented in two subsystems with different bodies,
        once with a leading `*`.
    When:
        seed_vocabulary seeds the vocabulary.
    Then:
        It should emit one clash for the bare name, listing both subsystems,
        one meaning per subsystem, and marked for review.
    """
    # Arrange
    units = [
        unit("a/x.py", ("timeout", "seconds to wait\n    for a reply")),
        unit("b/x.py", ("*timeout", "retries before giving up")),
    ]

    # Act
    clashes, _ = seed_scope_facts.seed_vocabulary(units, SUBSYSTEM_OF)

    # Assert
    assert clashes == [{
        "term": "timeout", "uses": 2, "subsystems": ["a", "b"],
        "meanings": {"a": "seconds to wait for a reply",
                     "b": "retries before giving up"},
        "distinctBodies": 2, "classification": None, "needsReview": True,
    }]


@pytest.mark.parametrize(
    "units",
    [
        [unit("a/x.py", ("timeout", "seconds to wait")),
         unit("a/y.py", ("timeout", "retries before giving up"))],
        [unit("a/x.py", ("timeout", "Seconds to wait")),
         unit("b/x.py", ("timeout", "seconds  to wait"))],
        [unit("a/x.py", ("timeout", "seconds to wait")),
         unit("elsewhere.py", ("timeout", "retries before giving up"))],
    ],
    ids=["one-subsystem", "same-body", "file-without-subsystem"],
)
def test_seed_vocabulary_should_not_flag_a_name_that_does_not_clash(
    seed_scope_facts, units
):
    """Test each condition under which a repeated name is not a clash.

    Given:
        A parameter name documented twice within one subsystem, or in two
        subsystems with the same body up to case and spacing, or once in a
        file that belongs to no subsystem.
    When:
        seed_vocabulary seeds the vocabulary.
    Then:
        It should emit no clash.
    """
    # Act
    clashes, _ = seed_scope_facts.seed_vocabulary(units, SUBSYSTEM_OF)

    # Assert
    assert clashes == []


def test_seed_vocabulary_should_pair_names_whose_bodies_overlap(seed_scope_facts):
    """Test the parallel-name derivation.

    Given:
        Two differently named parameters whose bodies share most content
        words, and a third whose body shares none.
    When:
        seed_vocabulary seeds the vocabulary.
    Then:
        It should pair the first two with their Jaccard similarity and leave
        the third unpaired.
    """
    # Arrange
    units = [unit("a/x.py",
                  ("max_send", "maximum message length accepted when sending bytes"),
                  ("max_recv", "maximum message length accepted when receiving bytes"),
                  ("loop", "event loop that schedules every worker coroutine"))]

    # Act
    _, aliases = seed_scope_facts.seed_vocabulary(units, SUBSYSTEM_OF)

    # Assert
    assert aliases == [{"names": ["max_recv", "max_send"], "jaccard": 0.714,
                        "canonical": None, "needsReview": True}]


def test_seed_vocabulary_should_not_pair_names_whose_bodies_are_too_short(
    seed_scope_facts,
):
    """Test the minimum body size for comparison.

    Given:
        Two differently named parameters with identical three-word bodies.
    When:
        seed_vocabulary seeds the vocabulary.
    Then:
        It should pair nothing, since a short body matches too easily.
    """
    # Arrange
    units = [unit("a/x.py", ("fn", "callable being wrapped"),
                  ("func", "callable being wrapped"))]

    # Act
    _, aliases = seed_scope_facts.seed_vocabulary(units, SUBSYSTEM_OF)

    # Assert
    assert aliases == []


# ── main() ────────────────────────────────────────────────────────────────

UNITS = [
    unit("pkg/discovery/lan.py", ("timeout", "seconds to wait for a reply")),
    unit("pkg/worker/pool.py", ("timeout", "retries before giving up")),
    unit("pkg/discovery/base.py"),
]


def run_main(module, monkeypatch, tmp_path: Path, extra=()):
    """Invoke seed_scope_facts.main() on UNITS; return the written payload."""
    doc_units = write_json(tmp_path / "doc-units.json", {"docUnits": UNITS})
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "seed_scope_facts.py", str(tmp_path), "--doc-units", str(doc_units),
        "--out", str(out), *extra])
    assert module.main() == 0
    return json.loads((out / "scope-facts.json").read_text())


def test_main_should_take_subsystems_from_the_graph_when_it_has_layers(
    seed_scope_facts, monkeypatch, tmp_path, capsys
):
    """Test the run that has a knowledge graph to draw on.

    Given:
        Doc units from three files and a graph whose layers cover two of them.
    When:
        main() seeds the scope facts with --graph.
    Then:
        It should record the graph as the subsystem source, report the
        uncovered file as unassigned, emit the cross-layer clash, and count
        the undescribed layer among the review items.
    """
    # Arrange
    graph = graph_with_layers(tmp_path / "graph.json", [
        {"id": "layer:discovery", "description": "Finds workers.",
         "nodeIds": ["file:pkg/discovery/lan.py"]},
        {"id": "layer:worker", "nodeIds": ["file:pkg/worker/pool.py"]},
    ])

    # Act
    payload = run_main(seed_scope_facts, monkeypatch, tmp_path,
                       extra=("--graph", str(graph)))

    # Assert
    assert payload["subsystemSource"] == "knowledge-graph.layers"
    assert payload["unassignedFiles"] == ["pkg/discovery/base.py"]
    assert [c["term"] for c in payload["vocabulary"]] == ["timeout"]
    assert payload["vocabulary"][0]["subsystems"] == ["layer:discovery", "layer:worker"]
    assert payload["stats"] == {
        "subsystems": 2, "filesAssigned": 2, "filesUnassigned": 1,
        "vocabularyCandidates": 1, "parallelNameCandidates": 0, "reviewItems": 2,
    }
    assert "UNASSIGNED: 1" in capsys.readouterr().out


def test_main_should_fall_back_to_directories_when_there_is_no_graph(
    seed_scope_facts, monkeypatch, tmp_path, capsys
):
    """Test the run without a knowledge graph.

    Given:
        Doc units from three files and no graph at the default location.
    When:
        main() seeds the scope facts with a source prefix.
    Then:
        It should say on stderr that it fell back, group every file by
        directory, and leave none unassigned.
    """
    # Act
    payload = run_main(seed_scope_facts, monkeypatch, tmp_path,
                       extra=("--src-prefix", "pkg/"))

    # Assert
    assert "falling back to directory structure" in capsys.readouterr().err
    assert payload["subsystemSource"] == "directory-fallback"
    assert [s["name"] for s in payload["subsystems"]] == ["discovery", "worker"]
    assert payload["unassignedFiles"] == []
    assert payload["vocabulary"][0]["subsystems"] == ["discovery", "worker"]
