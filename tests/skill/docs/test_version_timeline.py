"""Tests for version_timeline.py — dating doc units and judging the spread.

The script's output is a go/no-go signal for orienting contradictions by
recency. The verdict has to separate two conditions that look alike in a
histogram and call for opposite remedies: blame flattened into one release
series, and blame that returned nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

TAGS = [(100, "v0.1.0"), (200, "v0.2.0-rc1"), (300, "v0.2.0")]

# Commit times, one year apart, as git's `<unix> <tz>` date format.
T1, T2, T3 = 1577880000, 1609502400, 1641038400


# ── release_for ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [(50, "v0.1.0"), (100, "v0.1.0"), (101, "v0.2.0-rc1"), (300, "v0.2.0"),
     (301, None)],
    ids=["before-first", "at-a-tag", "between-tags", "at-last", "after-last"],
)
def test_release_for_should_return_the_first_tag_at_or_after_the_timestamp(
    version_timeline, timestamp, expected
):
    """Test release resolution at and around each tag.

    Given:
        Three tags in date order.
    When:
        release_for resolves a timestamp before, at, between, and after them.
    Then:
        It should return the first tag created at or after the timestamp, and
        None once every tag is older.
    """
    # Act & assert
    assert version_timeline.release_for(timestamp, TAGS) == expected


def test_release_for_should_return_none_when_there_are_no_tags(version_timeline):
    """Test release resolution in an untagged repository.

    Given:
        An empty tag list.
    When:
        release_for resolves any timestamp.
    Then:
        It should return None.
    """
    # Act & assert
    assert version_timeline.release_for(100, []) is None


# ── minor_series ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v0.12.0", "0.12"), ("0.12.3", "0.12"), ("v0.12.0-rc10", "0.12"),
     ("v10.2.33", "10.2"), ("release-candidate", None), ("v1.2", None),
     ("", None), (None, None)],
)
def test_minor_series_should_reduce_a_version_tag_to_major_and_minor(
    version_timeline, tag, expected
):
    """Test the comparison unit derived from a tag.

    Given:
        A version tag with or without a `v` prefix or pre-release suffix, a
        tag that is not a three-part version, or no tag.
    When:
        minor_series reduces it.
    Then:
        It should return `major.minor`, or None when there is no version.
    """
    # Act & assert
    assert version_timeline.minor_series(tag) == expected


# ── blame_file ────────────────────────────────────────────────────────────


def git(root: Path, *args: str, when: int | None = None) -> str:
    """Run git in `root` with a fixed identity and, optionally, a fixed date."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{when} +0000"
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        check=True, capture_output=True, text=True, env=env,
    ).stdout


def commit(root: Path, rel: str, text: str, when: int, tag: str | None = None) -> str:
    """Write one file, commit it at `when`, optionally tag it; return the sha."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"add {rel}", when=when)
    if tag:
        git(root, "tag", tag, when=when)
    return git(root, "rev-parse", "HEAD").strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """An empty git repository."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


def test_blame_file_should_map_each_line_to_the_commit_that_wrote_it(
    version_timeline, repo
):
    """Test the per-line blame map.

    Given:
        A file whose first two lines were committed at one time and whose
        third line was added by a later commit.
    When:
        blame_file blames the file.
    Then:
        It should map every line number to the sha and author time of the
        commit that wrote that line.
    """
    # Arrange
    first = commit(repo, "pkg/a.py", "one\ntwo\n", T1)
    second = commit(repo, "pkg/a.py", "one\ntwo\nthree\n", T2)

    # Act
    lines = version_timeline.blame_file(repo, "pkg/a.py")

    # Assert
    assert lines == {1: (first, T1), 2: (first, T1), 3: (second, T2)}


def test_blame_file_should_return_nothing_when_the_file_is_not_tracked(
    version_timeline, repo
):
    """Test blame on a path git does not know.

    Given:
        A repository and a path that was never committed.
    When:
        blame_file blames the path.
    Then:
        It should return an empty map without raising.
    """
    # Arrange
    commit(repo, "pkg/a.py", "one\n", T1)

    # Act & assert
    assert version_timeline.blame_file(repo, "pkg/missing.py") == {}


# ── viability ─────────────────────────────────────────────────────────────


UNRESOLVED = {
    # blame returned nothing
    "unknown": {"sha": None, "release": None, "minorSeries": None},
    # blamed, and written after the last version tag
    "late": {"sha": "a" * 40, "release": None, "minorSeries": None},
}


def entries(**counts: int) -> list[dict]:
    """Timeline entries with the given number of doc units per series.

    The keys of `UNRESOLVED` produce entries with no series, one kind per
    cause; any other key is a series, with `_` standing for the dot.
    """
    out = []
    for series, n in counts.items():
        if series in UNRESOLVED:
            entry = UNRESOLVED[series]
        else:
            value = series.replace("_", ".")
            entry = {"sha": "a" * 40, "release": f"v{value}.0", "minorSeries": value}
        out.extend(dict(entry) for _ in range(n))
    return out


def test_viability_should_not_name_unresolved_units_the_largest_series(
    version_timeline,
):
    """Test the case in which most doc units resolved to no series.

    Given:
        100 doc units with no series, 20 in 0.12, and 10 in 0.13.
    When:
        viability judges the timeline.
    Then:
        It should name 0.12 the largest series, count 10 units outside it,
        report the 100 as unresolved, and return the `undetermined` verdict.
    """
    # Act
    result = version_timeline.viability(
        entries(unknown=100, **{"0_12": 20, "0_13": 10}))

    # Assert
    assert result["largestSeries"] == "0.12"
    assert result["largestSeriesShare"] == round(20 / 30, 4)
    assert result["outsideLargestSeries"] == 10
    assert result["unresolved"] == 100
    assert result["unresolvedShare"] == round(100 / 130, 4)
    assert result["verdict"] == "undetermined"
    assert result["verdictReason"] == "blame failed"


@pytest.mark.parametrize(
    ("counts", "verdict"),
    [
        ({"0_9": 91, "0_8": 9}, "not viable"),
        ({"0_9": 90, "0_8": 10}, "marginal"),
        ({"0_9": 100}, "not viable"),
        ({"0_9": 60, "0_8": 40}, "marginal"),
        ({"0_9": 40, "0_8": 30, "0_7": 30}, "viable"),
        ({"0_9": 40, "0_8": 30, "0_7": 30, "unknown": 100}, "viable"),
        ({"0_9": 40, "0_8": 30, "0_7": 30, "unknown": 101}, "undetermined"),
        ({"unknown": 10}, "undetermined"),
        ({}, "undetermined"),
    ],
    ids=["just-over-flattened", "at-flattened", "one-series", "two-series",
         "spread", "half-unresolved", "majority-unresolved", "nothing-resolved",
         "no-doc-units"],
)
def test_viability_should_return_the_verdict_for_each_distribution(
    version_timeline, counts, verdict
):
    """Test every verdict and the boundary of the unresolved share.

    Given:
        A distribution of doc units across release series and unresolved.
    When:
        viability judges the timeline.
    Then:
        It should return the verdict for that distribution.
    """
    # Act
    result = version_timeline.viability(entries(**counts))

    # Assert
    assert result["verdict"] == verdict
    assert result["verdict"] in version_timeline.VERDICT_TEXT
    assert (result["verdictReason"] is not None) == (verdict == "undetermined")


@pytest.mark.parametrize(
    ("counts", "flags", "reason", "split"),
    [
        ({"0_9": 3, "unknown": 4}, {}, "blame failed", (4, 0)),
        ({"0_9": 3, "late": 4}, {}, "unreleased", (0, 4)),
        ({"0_9": 3, "unknown": 2, "late": 2}, {}, "unreleased", (2, 2)),
        ({"0_9": 3, "unknown": 3, "late": 2}, {}, "blame failed", (3, 2)),
        ({}, {}, "no doc units", (0, 0)),
        ({"late": 5}, {"tagged": False}, "no version tags", (0, 5)),
        ({"0_9": 4, "0_8": 3, "0_7": 3}, {"shallow": True}, "shallow clone", (0, 0)),
        ({"late": 5}, {"shallow": True, "tagged": False}, "shallow clone", (0, 5)),
    ],
    ids=["no-blame", "after-last-tag", "blame-is-half", "blame-is-majority",
         "empty", "untagged", "shallow", "shallow-and-untagged"],
)
def test_viability_should_name_the_reason_a_verdict_is_undetermined(
    version_timeline, counts, flags, reason, split
):
    """Test that a failed measurement is told apart from an unreleased corpus.

    Given:
        A timeline that cannot be judged: blame returned nothing, the doc
        units postdate the last version tag, a mix of the two, no doc units
        at all, a clone with no version tags, or a shallow clone.
    When:
        viability judges the timeline.
    Then:
        It should return `undetermined` with the reason for that cause, and
        the unresolved doc units split by cause.
    """
    # Act
    result = version_timeline.viability(entries(**counts), **flags)

    # Assert
    assert (result["verdict"], result["verdictReason"]) == ("undetermined", reason)
    assert result["verdictReason"] in version_timeline.REASON_TEXT
    assert tuple(result["unresolvedBy"].values()) == split


def test_viability_should_report_no_largest_series_when_nothing_resolved(
    version_timeline,
):
    """Test the figures when no doc unit has a series.

    Given:
        Ten doc units, none of which resolved to a series.
    When:
        viability judges the timeline.
    Then:
        It should report no largest series, no share, and zero units outside.
    """
    # Act
    result = version_timeline.viability(entries(unknown=10))

    # Assert
    assert result["largestSeries"] is None
    assert result["largestSeriesShare"] is None
    assert result["outsideLargestSeries"] == 0
    assert result["distinctMinorSeries"] == 0


def test_viability_should_break_a_tie_for_largest_by_series_name(version_timeline):
    """Test that the largest series does not depend on entry order.

    Given:
        Two series of equal size, presented in each order.
    When:
        viability judges each ordering.
    Then:
        It should name the same series largest both times.
    """
    # Arrange
    forward = entries(**{"0_8": 5, "0_9": 5})
    backward = list(reversed(forward))

    # Act & assert
    assert (version_timeline.viability(forward)["largestSeries"]
            == version_timeline.viability(backward)["largestSeries"] == "0.8")


@settings(max_examples=100, deadline=None)
@given(counts=st.dictionaries(
    st.sampled_from(["unknown", "late", "0_1", "0_2", "0_3", "1_0"]),
    st.integers(min_value=0, max_value=200)))
def test_viability_should_keep_every_figure_in_range(version_timeline, counts):
    """Test the invariants of the viability figures.

    Given:
        Any distribution of doc units across series and unresolved.
    When:
        viability judges the timeline.
    Then:
        The count outside the largest series should never be negative, the
        largest series should be a real series or absent, both shares should
        lie in [0, 1], the resolved and unresolved counts should sum to the
        total, and the per-cause counts should sum to the unresolved count.
    """
    # Arrange
    timeline = entries(**counts)

    # Act
    result = version_timeline.viability(timeline)

    # Assert
    resolved = sum(n for series, n in counts.items() if series not in UNRESOLVED)
    assert result["outsideLargestSeries"] >= 0
    assert result["largestSeries"] != "unknown"
    assert (result["largestSeries"] is None) == (resolved == 0)
    assert 0.0 <= result["unresolvedShare"] <= 1.0
    if result["largestSeriesShare"] is not None:
        assert 0.0 < result["largestSeriesShare"] <= 1.0
    assert result["unresolved"] + resolved == len(timeline)
    assert sum(result["unresolvedBy"].values()) == result["unresolved"]


# ── main() ────────────────────────────────────────────────────────────────


def doc_unit(uid: str, rel: str, span: list[int] | None) -> dict:
    return {"id": uid, "sourceFile": rel, "docLineRange": span}


def run_main(module, monkeypatch, root: Path, units: list[dict], out: Path,
             extra=()):
    """Invoke version_timeline.main(); return the written payload."""
    doc_units = out.parent / "doc-units.json"
    doc_units.write_text(json.dumps({"docUnits": units}))
    monkeypatch.setattr(sys, "argv", [
        "version_timeline.py", str(root), "--doc-units", str(doc_units),
        "--out", str(out), *extra])
    assert module.main() == 0
    return json.loads((out / "version-timeline.json").read_text())


def test_main_should_date_a_doc_unit_by_its_earliest_line(
    version_timeline, monkeypatch, repo, tmp_path
):
    """Test that a later edit to one line does not redate the docstring.

    Given:
        A docstring committed before the first release, one line of which a
        commit after the second release rewrote.
    When:
        main() builds the timeline.
    Then:
        It should date the doc unit to the first commit and resolve it to the
        first release.
    """
    # Arrange
    first = commit(repo, "pkg/a.py", 'def f():\n    """One.\n\n    Two.\n    """\n',
                   T1, tag="v0.1.0")
    commit(repo, "pkg/a.py", 'def f():\n    """One.\n\n    Two, reworded.\n    """\n',
           T2, tag="v0.2.0")

    # Act
    payload = run_main(version_timeline, monkeypatch, repo,
                       [doc_unit("docunit:a", "pkg/a.py", [2, 5])], tmp_path / "out")

    # Assert
    (entry,) = payload["entries"]
    assert (entry["sha"], entry["authoredAt"]) == (first, T1)
    assert (entry["release"], entry["minorSeries"]) == ("v0.1.0", "0.1")


def test_main_should_leave_a_doc_unit_unreleased_when_it_postdates_every_tag(
    version_timeline, monkeypatch, repo, tmp_path
):
    """Test a doc unit written after the last release.

    Given:
        One file committed and tagged, and a second file committed afterwards
        with no later tag.
    When:
        main() builds the timeline.
    Then:
        It should date the second file's doc unit and give it no release and
        no series, and count it as unresolved without counting it as unblamed.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="v0.1.0")
    commit(repo, "pkg/b.py", 'def g():\n    """Two."""\n', T2)

    # Act
    payload = run_main(version_timeline, monkeypatch, repo, [
        doc_unit("docunit:a", "pkg/a.py", [2, 2]),
        doc_unit("docunit:b", "pkg/b.py", [2, 2]),
    ], tmp_path / "out")

    # Assert
    late = next(e for e in payload["entries"] if e["docUnitId"] == "docunit:b")
    assert late["authoredAt"] == T2
    assert (late["release"], late["minorSeries"]) == (None, None)
    assert payload["stats"]["unknown"] == 0
    assert payload["stats"]["unresolved"] == 1
    assert payload["stats"]["unresolvedBy"] == {"noBlame": 0, "afterLastTag": 1}
    assert payload["stats"]["byRelease"]["(none)"] == 1
    assert payload["stats"]["byMinorSeries"] == {"(none)": 1, "0.1": 1}


def test_main_should_report_undetermined_when_blame_returns_nothing(
    version_timeline, monkeypatch, repo, tmp_path, capsys
):
    """Test the report when the measurement itself failed.

    Given:
        A tagged repository, one doc unit in a tracked file, and five doc
        units whose files are not tracked.
    When:
        main() builds the timeline with --report.
    Then:
        It should print the `undetermined` verdict with the reason that blame
        failed, the unresolved count split by cause, and the standing rule;
        write the same verdict and reason to the output; name the one real
        series the largest; and not report blame as flattened.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="v0.1.0")
    units = [doc_unit("docunit:a", "pkg/a.py", [2, 2])] + [
        doc_unit(f"docunit:x{i}", f"pkg/untracked{i}.py", [1, 1]) for i in range(5)]

    # Act
    payload = run_main(version_timeline, monkeypatch, repo, units, tmp_path / "out",
                       extra=("--report",))

    # Assert
    err = capsys.readouterr().err
    assert "VERDICT: undetermined (blame failed)" in err
    assert ("5 of 6 doc units (83.3%) resolved to no release series: 5 no blame, "
            "0 after the last version tag") in err
    assert "Do not stage, commit, or stash anything." in err
    assert "flattened" not in err
    assert version_timeline.STANDING_RULE in err
    assert payload["stats"]["verdict"] == "undetermined"
    assert payload["stats"]["verdictReason"] == "blame failed"
    assert payload["stats"]["largestSeries"] == "0.1"
    assert payload["stats"]["unknown"] == 5
    assert payload["stats"]["outsideLargestSeries"] == 0


def test_main_should_report_not_viable_when_one_series_holds_the_doc_units(
    version_timeline, monkeypatch, repo, tmp_path, capsys
):
    """Test the report when blame is flattened.

    Given:
        A repository whose every doc unit was committed before one release.
    When:
        main() builds the timeline with --report.
    Then:
        It should print the `not viable` verdict and name that series.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n\ndef g():\n    """Two."""\n',
           T1, tag="v0.9.0")

    # Act
    payload = run_main(version_timeline, monkeypatch, repo, [
        doc_unit("docunit:a", "pkg/a.py", [2, 2]),
        doc_unit("docunit:b", "pkg/a.py", [5, 5]),
    ], tmp_path / "out", extra=("--report",))

    # Assert
    err = capsys.readouterr().err
    assert "VERDICT: not viable" in err
    assert "largest series '0.9' holds 100.0% of the doc units that did" in err
    assert payload["stats"]["verdict"] == "not viable"


def test_main_should_say_so_when_the_repository_has_no_tags(
    version_timeline, monkeypatch, repo, tmp_path, capsys
):
    """Test an untagged repository.

    Given:
        A repository with a commit and no tag.
    When:
        main() builds the timeline.
    Then:
        It should say on stderr that there are no version tags, leave the doc
        unit dated with no release, and give that as the reason the verdict is
        undetermined.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1)

    # Act
    payload = run_main(version_timeline, monkeypatch, repo,
                       [doc_unit("docunit:a", "pkg/a.py", [2, 2])], tmp_path / "out")

    # Assert
    assert "no version tags" in capsys.readouterr().err
    assert payload["tagCount"] == 0
    assert payload["entries"][0]["authoredAt"] == T1
    assert payload["entries"][0]["release"] is None
    assert payload["stats"]["verdict"] == "undetermined"
    assert payload["stats"]["verdictReason"] == "no version tags"


def test_main_should_report_unreleased_when_most_doc_units_postdate_the_last_tag(
    version_timeline, monkeypatch, repo, tmp_path, capsys
):
    """Test the report on a repository with much work since its last release.

    Given:
        One doc unit committed before the only tag and three committed after
        it, all blamed successfully.
    When:
        main() builds the timeline with --report.
    Then:
        It should print `undetermined` with the reason `unreleased`, say that
        running again will not change it, and not tell the operator to check
        the files.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="v0.1.0")
    commit(repo, "pkg/b.py",
           'def g():\n    """Two."""\n\ndef h():\n    """Three."""\n\n'
           'def i():\n    """Four."""\n', T2)

    # Act
    payload = run_main(version_timeline, monkeypatch, repo, [
        doc_unit("docunit:a", "pkg/a.py", [2, 2]),
        doc_unit("docunit:b", "pkg/b.py", [2, 2]),
        doc_unit("docunit:c", "pkg/b.py", [5, 5]),
        doc_unit("docunit:d", "pkg/b.py", [8, 8]),
    ], tmp_path / "out", extra=("--report",))

    # Assert
    err = capsys.readouterr().err
    assert "VERDICT: undetermined (unreleased)" in err
    assert "running again will not change it" in err
    assert "3 after the last version tag" in err
    assert "committed" not in err
    assert payload["stats"]["verdictReason"] == "unreleased"


def test_main_should_report_a_shallow_clone_as_undetermined(
    version_timeline, monkeypatch, repo, tmp_path, capsys
):
    """Test that truncated history is not read as flattened blame.

    Given:
        A repository with doc units in three release series, cloned at depth
        one so that a single commit and a single tag remain.
    When:
        main() builds the timeline on the clone with --report.
    Then:
        It should print `undetermined` with the reason `shallow clone` and
        not report the corpus as not viable.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="v0.1.0")
    commit(repo, "pkg/b.py", 'def g():\n    """Two."""\n', T2, tag="v0.2.0")
    commit(repo, "pkg/c.py", 'def h():\n    """Three."""\n', T3, tag="v0.3.0")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--depth", "1", repo.as_uri(), str(clone))

    # Act
    payload = run_main(version_timeline, monkeypatch, clone, [
        doc_unit("docunit:a", "pkg/a.py", [2, 2]),
        doc_unit("docunit:b", "pkg/b.py", [2, 2]),
        doc_unit("docunit:c", "pkg/c.py", [2, 2]),
    ], tmp_path / "out", extra=("--report",))

    # Assert
    err = capsys.readouterr().err
    assert "VERDICT: undetermined (shallow clone)" in err
    assert "not viable" not in err
    assert payload["stats"]["shallowClone"] is True
    assert payload["stats"]["verdictReason"] == "shallow clone"


def test_main_should_resolve_past_a_tag_that_is_not_a_version(
    version_timeline, monkeypatch, repo, tmp_path
):
    """Test that a non-version tag does not stand in for the next release.

    Given:
        A doc unit committed before a `nightly` tag, with a version tag
        created after that.
    When:
        main() builds the timeline.
    Then:
        It should resolve the doc unit to the version tag and count one tag.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="nightly-2020")
    commit(repo, "pkg/b.py", "x = 1\n", T2, tag="v0.2.0")

    # Act
    payload = run_main(version_timeline, monkeypatch, repo,
                       [doc_unit("docunit:a", "pkg/a.py", [2, 2])], tmp_path / "out")

    # Assert
    (entry,) = payload["entries"]
    assert (entry["release"], entry["minorSeries"]) == ("v0.2.0", "0.2")
    assert payload["tagCount"] == 1


def test_main_should_count_uncommitted_docstrings_as_unblamed(
    version_timeline, monkeypatch, repo, tmp_path, capsys
):
    """Test that a line with no commit is not dated to the moment of the run.

    Given:
        One committed and tagged doc unit, and three doc units in a file that
        is staged and has never been committed.
    When:
        main() builds the timeline with --report.
    Then:
        It should give the three no sha and no date, count them as unblamed,
        and report that blame failed, not that the corpus is unreleased.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="v0.1.0")
    (repo / "pkg" / "new.py").write_text(
        'def g():\n    """Two."""\n\ndef h():\n    """Three."""\n\n'
        'def i():\n    """Four."""\n')
    git(repo, "add", "-A")

    # Act
    payload = run_main(version_timeline, monkeypatch, repo, [
        doc_unit("docunit:a", "pkg/a.py", [2, 2]),
        doc_unit("docunit:b", "pkg/new.py", [2, 2]),
        doc_unit("docunit:c", "pkg/new.py", [5, 5]),
        doc_unit("docunit:d", "pkg/new.py", [8, 8]),
    ], tmp_path / "out", extra=("--report",))

    # Assert
    staged = [e for e in payload["entries"] if e["sourceFile"] == "pkg/new.py"]
    assert {(e["sha"], e["authoredAt"]) for e in staged} == {(None, None)}
    assert payload["stats"]["unresolvedBy"] == {"noBlame": 3, "afterLastTag": 0}
    assert "VERDICT: undetermined (blame failed)" in capsys.readouterr().err


def test_main_should_date_an_edited_docstring_by_its_committed_lines(
    version_timeline, monkeypatch, repo, tmp_path
):
    """Test a docstring with one uncommitted line among committed ones.

    Given:
        A committed, tagged docstring, one line of which has since been edited
        in the working tree without a commit.
    When:
        main() builds the timeline.
    Then:
        It should date the doc unit by its committed lines.
    """
    # Arrange
    first = commit(repo, "pkg/a.py", 'def f():\n    """One.\n\n    Two.\n    """\n',
                   T1, tag="v0.1.0")
    (repo / "pkg" / "a.py").write_text(
        'def f():\n    """One.\n\n    Two, edited.\n    """\n')

    # Act
    payload = run_main(version_timeline, monkeypatch, repo,
                       [doc_unit("docunit:a", "pkg/a.py", [2, 5])], tmp_path / "out")

    # Assert
    (entry,) = payload["entries"]
    assert (entry["sha"], entry["authoredAt"], entry["release"]) == (first, T1, "v0.1.0")


@pytest.mark.parametrize("root", ["plain", "missing"])
def test_main_should_fail_with_one_line_when_the_root_is_not_a_repository(
    version_timeline, monkeypatch, tmp_path, capsys, root
):
    """Test a repository root that git does not recognize.

    Given:
        A root that is an ordinary directory, or does not exist, outside any
        git repository.
    When:
        main() is run on it with a valid doc-units file.
    Then:
        It should exit 1 with one NOT A REPOSITORY line naming the root, write
        no timeline, and not create the root.
    """
    # Arrange
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    target = tmp_path / root
    if root == "plain":
        target.mkdir()
    doc_units = tmp_path / "doc-units.json"
    doc_units.write_text(json.dumps({"docUnits": [doc_unit("docunit:a", "a.py", [1, 1])]}))
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "version_timeline.py", str(target), "--doc-units", str(doc_units), "--out", str(out)])

    # Act
    code = version_timeline.main()

    # Assert
    err = capsys.readouterr().err
    assert code == 1
    assert err.count("\n") == 1
    assert err.startswith("NOT A REPOSITORY")
    assert str(target.resolve()) in err
    assert not out.exists()
    assert target.exists() is (root == "plain")


@pytest.mark.parametrize("content", [None, "", "{not json", "[]", "{}"],
                         ids=["missing", "empty", "malformed", "list", "no-key"])
def test_main_should_fail_with_one_line_when_the_doc_units_file_is_unreadable(
    version_timeline, monkeypatch, repo, tmp_path, capsys, content
):
    """Test a doc-units argument that is not a doc-units file.

    Given:
        A repository and a doc-units path that is missing, empty, not JSON, or
        JSON without a `docUnits` key.
    When:
        main() is run with it.
    Then:
        It should exit 1 with one BAD DOC UNITS line naming the file and write
        no timeline.
    """
    # Arrange
    commit(repo, "pkg/a.py", 'def f():\n    """One."""\n', T1, tag="v0.1.0")
    doc_units = tmp_path / "doc-units.json"
    if content is not None:
        doc_units.write_text(content)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", [
        "version_timeline.py", str(repo), "--doc-units", str(doc_units), "--out", str(out)])

    # Act
    code = version_timeline.main()

    # Assert
    err = capsys.readouterr().err
    assert code == 1
    assert err.count("\n") == 1
    assert err.startswith("BAD DOC UNITS")
    assert str(doc_units) in err
    assert not out.exists()
