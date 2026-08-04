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
