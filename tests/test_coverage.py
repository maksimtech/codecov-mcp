"""Which files lost coverage, and what "lost" is allowed to mean.

Codecov has a `compare/` endpoint and it is not enough on its own: it needs both
commits to be in Codecov, and one of them often is not. On 2026-09-25 apkradar's
774f17e was in git and not in Codecov, because the test job failed on that push so
the upload step never ran, and `compare/` answered

    404 {"detail":"Commit or branch '774f17e…' not found!"}

So the diff is computed from two per-commit reports, which exist whenever each
commit was uploaded, and the missing-report case is reported as itself rather than
as "no change". Those two are opposite answers and the reader acts differently on
each.

The two reports here are real: 1e90d15 at 95.61% and 9147b24 at 95.39%, the second
being a commit of mine that moved coverage down. A file that appears in one report
and not in the other is not a drop either — it was added or deleted, and calling
that a loss of coverage would put a number on a file that has no previous one.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from codecov_mcp.coverage import FileCoverage, Report, compare

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def report(name: str) -> Report:
    body = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["body"]
    return Report.from_payload(body)


@pytest.fixture
def head() -> Report:
    return report("report_head")


@pytest.fixture
def parent() -> Report:
    return report("report_parent")


# ── reading a report ────────────────────────────────────────────────────────


def test_a_report_carries_its_total(head):
    assert head.coverage == pytest.approx(95.39)
    assert head.lines == 1586


def test_a_report_carries_one_entry_per_file(head):
    assert len(head.files) == 13
    assert "apkradar/publisher.py" in head.files


def test_each_file_knows_its_own_coverage(head):
    publisher = head.files["apkradar/publisher.py"]

    assert publisher.coverage == pytest.approx(92.85)
    assert publisher.lines == 140
    assert publisher.misses == 10


def test_a_report_with_line_numbers_keeps_them():
    """The per-file endpoint returns line_coverage, and a caller asking about one
    file wants the lines, not another percentage."""
    one = report("report_one_file")

    assert one.files["apkradar/publisher.py"].uncovered_lines


def test_the_uncovered_lines_are_the_ones_marked_missed():
    one = report("report_one_file")
    lines = one.files["apkradar/publisher.py"].uncovered_lines

    assert all(isinstance(number, int) for number in lines)
    # A tuple, not a list: a report is read, never edited.
    assert lines == tuple(sorted(lines))
    # The ten the CI's own coverage report named for this file in the same run.
    assert lines == (118, 177, 267, 288, 294, 307, 321, 322, 323, 324)


# ── comparing two reports ───────────────────────────────────────────────────


def test_the_total_change_is_the_difference(head, parent):
    outcome = compare(base=parent, head=head)

    assert outcome.change == pytest.approx(95.39 - 95.61, abs=1e-9)
    assert outcome.dropped_overall is True


def test_the_files_that_lost_coverage_are_listed(head, parent):
    outcome = compare(base=parent, head=head)

    for loss in outcome.losses:
        assert loss.before > loss.after
    assert outcome.losses == sorted(outcome.losses, key=lambda loss: loss.delta)


def test_a_file_that_did_not_change_is_not_a_loss(head, parent):
    outcome = compare(base=parent, head=head)
    names = {loss.path for loss in outcome.losses}

    unchanged = [
        path
        for path, file in head.files.items()
        if path in parent.files and file.coverage == parent.files[path].coverage
    ]
    assert unchanged, "the two real reports do share unchanged files"
    assert not names & set(unchanged)


def test_a_new_file_is_not_a_loss(head, parent):
    """publisher.py exists in the later report and not in the earlier one. It has
    no previous coverage, so it cannot have lost any."""
    added = set(head.files) - set(parent.files)

    outcome = compare(base=parent, head=head)

    assert added, "the real pair does add a file"
    assert not {loss.path for loss in outcome.losses} & added
    assert outcome.added == sorted(added)


def test_a_deleted_file_is_reported_as_deleted(parent):
    """A file that leaves the report has not dropped to zero."""
    fewer = Report(
        coverage=parent.coverage,
        lines=parent.lines,
        hits=parent.hits,
        misses=parent.misses,
        files=dict(list(parent.files.items())[:-1]),
    )
    gone = sorted(set(parent.files) - set(fewer.files))

    outcome = compare(base=parent, head=fewer)

    assert outcome.removed == gone
    assert not {loss.path for loss in outcome.losses} & set(gone)


def test_a_gain_is_not_a_loss():
    base = Report(coverage=50.0, lines=10, hits=5, misses=5,
                  files={"a.py": FileCoverage("a.py", 50.0, 10, 5, 5)})
    head = Report(coverage=90.0, lines=10, hits=9, misses=1,
                  files={"a.py": FileCoverage("a.py", 90.0, 10, 9, 1)})

    outcome = compare(base=base, head=head)

    assert outcome.losses == []
    assert outcome.dropped_overall is False
    assert outcome.change == pytest.approx(40.0)


def test_comparing_a_report_with_itself_finds_nothing(head):
    outcome = compare(base=head, head=head)

    assert outcome.losses == []
    assert outcome.change == pytest.approx(0.0)
    assert outcome.added == [] and outcome.removed == []


def test_the_loss_carries_enough_to_act_on(head, parent):
    """A path and a percentage; the lines are what a reader opens the file for."""
    outcome = compare(base=parent, head=head)
    if not outcome.losses:
        pytest.skip("the recorded pair happens to have no per-file loss")

    loss = outcome.losses[0]
    assert loss.path
    assert loss.delta < 0
    assert loss.newly_uncovered >= 0
