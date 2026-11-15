"""Put the prompt invariants under CI, and guard the region they cannot see.

`understand-anything-plugin/skills/understand-docs/check_prompts.py` holds the
invariants this taxonomy depends on — and nothing ran it. It sits outside
`testpaths`, defines no `test_*` functions, and `SKILL.md` documented it as a
manual command. Issue #7's acceptance criterion says the one vocabulary is
"asserted by check_prompts.py"; until this module existed, it was asserted by
whoever remembered to type the command.

The script was called `test_prompts.py`, which made a guide-conformant name for
this file impossible — `test_<module_name>.py` would have been
`test_test_prompts.py`. Renaming the script rather than the suite fixes it at
the root and removes the standing risk that pytest collects a hand-run script if
`testpaths` ever widens.

The second test covers that script's blind spot. Its withdrawn-verdict guard
reads only `_taxonomy.md`, and only the markdown-table form — so the `## Output`
sections of the two prompts, which sit outside the synced block and are
hand-maintained, went on offering a fourth verdict to agents after the fold. The
tie-break prompt is the pipeline's last stop; an adjudicator working from a
stale enum is the one whose answer nothing downstream re-checks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PROMPTS = (Path(__file__).resolve().parents[3]
           / "understand-anything-plugin" / "skills" / "understand-docs" / "prompts")
WITHDRAWN = "overstated"


def output_section(name: str) -> str:
    """The part of a prompt after its `## Output` heading."""
    return (PROMPTS / name).read_text().split("## Output", 1)[-1]


def test_main_should_return_zero_when_every_prompt_invariant_holds(
    prompt_invariants, monkeypatch, capsys
):
    """Test that the prompt guard script passes, from inside the suite.

    Given:
        The prompts and reconciler as committed.
    When:
        check_prompts.main() runs in check mode.
    Then:
        It should return 0, so every invariant it carries runs in CI rather
        than only when someone remembers the command.
    """
    # Arrange
    prompt_invariants.FAILURES.clear()
    monkeypatch.setattr(sys, "argv", ["check_prompts.py"])

    # Act
    code = prompt_invariants.main()

    # Assert
    assert code == 0, capsys.readouterr().out


@pytest.mark.parametrize("prompt", ["ground-claims.md", "tiebreak-verdicts.md"])
def test_output_section_should_not_offer_the_withdrawn_verdict(prompt):
    """Test the region the shared-block guard cannot see.

    Given:
        A grounding prompt's `## Output` section, which lies outside the synced
        taxonomy block and is maintained by hand.
    When:
        It is scanned for the withdrawn verdict.
    Then:
        It should not name it — an agent told a fourth verdict is legal will
        emit one, and the tie-break prompt has no downstream stage to catch it.
    """
    # Act
    section = output_section(prompt)

    # Assert
    assert WITHDRAWN not in section


def test_VERDICTS_should_match_the_taxonomy_verdict_table(reconcile_verdicts):
    """Test that the prompt's verdict table and the script agree exactly.

    Given:
        The verdict table in the shared taxonomy and VERDICTS in the reconciler.
    When:
        The table's row labels are parsed and compared to the declared tuple.
    Then:
        They should be equal as sets — a substring check passes while a stale
        row survives in a different markdown form, which is how the withdrawn
        verdict lingered.
    """
    # Arrange — the verdict table only; the taxonomy has others (quantifiers)
    # whose rows are also single backticked words.
    taxonomy = (PROMPTS / "_taxonomy.md").read_text()
    table = taxonomy.split("| verdict | minimal repair |", 1)[1].split("\n\n", 1)[0]

    # Act
    rows = set(re.findall(r"^\| `(\w+)` \|", table, re.M))

    # Assert
    assert rows == set(reconcile_verdicts.VERDICTS)


SKILL = PROMPTS.parent


EDGE_LINE = '    "tensions-with": "related",\n'
TYPES = "design-edges prompt offers exactly the EDGE_MAP types"
READS = "emitter reads only keys the design-edges example shows"
ACCEPTS = "emitter accepts every edge and principle of the design-edges example"
EDGES_LOOP = '    for e in payload.get("edges", []):\n'
MAPPING = '        mapped = EDGE_MAP.get(e.get("type"))\n'
READER = "def read_design_edges("
EXAMPLE_EDGE = '"type": "justifies",\n     "rationale": "<one sentence: why this edge holds>"}'
CLAIMS_LINE = "each with `claimText`, `docUnitId`, `roles`"


@pytest.mark.parametrize("path, old, new, failure", [
    ("emit_claim_graph.py", EDGE_LINE, EDGE_LINE + '    "supersedes": "related",\n', TYPES),
    ("emit_claim_graph.py", EDGE_LINE, EDGE_LINE + "    'supersedes': 'related',\n", TYPES),
    ("emit_claim_graph.py", EDGE_LINE, '    "tensions-with": "related", "is_a": "related",\n', TYPES),
    ("emit_claim_graph.py", "}\n\n# Uppercase for findings",
     '}\nEDGE_MAP["supersedes"] = "related"\n\n# Uppercase for findings', TYPES),
    ("emit_claim_graph.py", MAPPING,
     '        mapped = EDGE_MAP.get(e.get("type")) or ("related" if e.get("type") == "supersedes" else None)\n',
     ACCEPTS),
    ("emit_claim_graph.py", EDGES_LOOP, EDGES_LOOP + '        e.get("weight")\n', READS),
    ("emit_claim_graph.py", EDGES_LOOP, EDGES_LOOP + "        e.get('weight')\n", READS),
    ("emit_claim_graph.py", EDGES_LOOP,
     '    edge_list = payload.get("edges", [])\n    for e in edge_list:\n        e.get("weight")\n', READS),
    ("emit_claim_graph.py", EDGES_LOOP,
     '    [x["weight"] for x in payload.get("edges", [])]\n' + EDGES_LOOP, READS),
    ("emit_claim_graph.py", EDGES_LOOP, EDGES_LOOP + "        _field(e, 'weight')\n", READS),
    ("emit_claim_graph.py", EDGES_LOOP, EDGES_LOOP + '        key = "kind"\n        e.get(key)\n', READS),
    ("prompts/design-edges.md", EXAMPLE_EDGE, '"type": "justifies"}', READS),
    ("prompts/design-edges.md", '"type": "justifies",', '"type": "supersedes",',
     "design-edges example uses only the types the prompt offers"),
    ("prompts/design-edges.md", CLAIMS_LINE, "each with `claimText`, `roles`",
     "design-edges prompt lists exactly the claim fields design_lens bundles"),
    ("prompts/extract-claims.md", "# Task: extract atomic claims from documentation\n",
     "<!-- maintainers: re-measure -->\n# Task: extract atomic claims from documentation\n",
     "extract-claims.md carries no HTML comment"),
    ("reconcile_verdicts.py", "the step-7 grounding passes", "Phase E grounding passes",
     "reconcile_verdicts.py names no stage, phase, or plan document"),
    ("design_lens.py", "Step 12 of the `understand-docs` pipeline",
     "stage 3 of the `understand-docs` pipeline",
     "design_lens.py names no stage, phase, or plan document"),
    ("design_lens.py", "no embeddings.\n", "no embeddings (see CLAIM-GRAPH-PLAN.md §2).\n",
     "design_lens.py names no stage, phase, or plan document"),
    ("reconcile_verdicts.py", "the step-7 grounding passes", "phase E grounding passes",
     "reconcile_verdicts.py names no stage, phase, or plan document"),
    ("design_lens.py", "Step 12 of the `understand-docs` pipeline", "Phase\nC of the `understand-docs` pipeline",
     "design_lens.py names no stage, phase, or plan document"),
    ("design_lens.py", "no embeddings.\n", "no embeddings (see claim-graph-plan.md).\n",
     "design_lens.py names no stage, phase, or plan document"),
    ("design_lens.py", "no embeddings.\n", "no embeddings (see the plan, § 1.9).\n",
     "design_lens.py names no stage, phase, or plan document"),
], ids=["edge-type-in-emitter-only", "single-quoted-edge-type", "two-edge-types-on-one-line",
        "edge-type-assigned-after-the-literal", "edge-type-special-cased-in-the-reader",
        "field-the-prompt-omits", "single-quoted-field", "field-read-through-a-list-alias",
        "field-read-in-a-comprehension", "field-read-in-a-helper", "field-key-in-a-variable",
        "field-dropped-from-the-example", "example-uses-a-forbidden-type",
        "bundle-field-the-prompt-omits", "comment-in-prompt", "phase-in-a-script",
        "lowercase-stage-in-a-script", "plan-cited-in-a-script", "lowercase-phase-in-a-script",
        "phase-wrapped-across-lines", "lowercase-plan-in-a-script", "section-sign-in-a-script"])
def test_main_should_fail_when_a_prompt_and_its_consumer_drift(
    prompt_invariants, monkeypatch, tmp_path, capsys, path, old, new, failure
):
    """Test the step-13, maintainer-note, and step-vocabulary invariants against a drifted copy.

    Given:
        A copy of the skill's prompts and scripts in which the emitter accepts
        an edge type the prompt does not offer, however the key is written,
        assigned, or special-cased; the emitter reads a field the prompt's
        example does not show, however the read is spelled or routed; the
        prompt's example breaks its own table; the prompt's claim fields differ
        from the bundle's; a prompt carries an HTML comment; or a script names
        a stage, a phase, or the plan document.
    When:
        check_prompts.main() runs against the copy.
    Then:
        It should return 1 and name the invariant that broke.
    """
    # Arrange
    copy = tmp_path / "skill"
    (copy / "prompts").mkdir(parents=True)
    for name in ("emit_claim_graph.py", "reconcile_verdicts.py", "design_lens.py"):
        (copy / name).write_text((SKILL / name).read_text())
    for prompt in PROMPTS.glob("*.md"):
        (copy / "prompts" / prompt.name).write_text(prompt.read_text())
    target = copy / path
    text = target.read_text()
    assert text.count(old) == 1
    text = text.replace(old, new)
    if "_field(" in new:
        text = text.replace(READER, "def _field(e, key):\n    return e.get(key)\n\n\n" + READER)
    target.write_text(text)
    monkeypatch.setattr(prompt_invariants, "HERE", copy)
    monkeypatch.setattr(prompt_invariants, "PROMPTS", copy / "prompts")
    monkeypatch.setattr(sys, "argv", ["check_prompts.py"])
    prompt_invariants.FAILURES.clear()

    # Act
    code = prompt_invariants.main()
    capsys.readouterr()

    # Assert
    assert code == 1
    assert [f for f in prompt_invariants.FAILURES if f.startswith(failure)]
    prompt_invariants.FAILURES.clear()


def test_main_should_pass_a_script_that_uses_stage_and_phase_as_english(
    prompt_invariants, monkeypatch, tmp_path, capsys
):
    """Test that the step-vocabulary invariant does not flag ordinary English.

    Given:
        A copy of the skill in which a script's docstring says "Do not stage a
        file" and "each phase a model runs", with no number or capital letter
        naming a stage or phase.
    When:
        check_prompts.main() runs against the copy.
    Then:
        It should return 0.
    """
    # Arrange
    copy = tmp_path / "skill"
    (copy / "prompts").mkdir(parents=True)
    for name in ("emit_claim_graph.py", "reconcile_verdicts.py", "design_lens.py"):
        (copy / name).write_text((SKILL / name).read_text())
    for prompt in PROMPTS.glob("*.md"):
        (copy / "prompts" / prompt.name).write_text(prompt.read_text())
    target = copy / "design_lens.py"
    text = target.read_text()
    old = "no embeddings.\n"
    assert text.count(old) == 1
    target.write_text(text.replace(
        old, "no embeddings. Do not stage a file, and each phase a model runs\nis its own.\n"))
    monkeypatch.setattr(prompt_invariants, "HERE", copy)
    monkeypatch.setattr(prompt_invariants, "PROMPTS", copy / "prompts")
    monkeypatch.setattr(sys, "argv", ["check_prompts.py"])
    prompt_invariants.FAILURES.clear()

    # Act
    code = prompt_invariants.main()
    capsys.readouterr()

    # Assert
    assert code == 0, prompt_invariants.FAILURES
    prompt_invariants.FAILURES.clear()
