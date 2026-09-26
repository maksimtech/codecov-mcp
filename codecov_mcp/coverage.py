"""
Reports, and what "this file lost coverage" is allowed to mean.

Codecov offers a `compare/` endpoint and it is not sufficient: it needs both
commits to be in Codecov, and one of them frequently is not — a push whose test
job failed never reaches the upload step, so the commit exists in git and not
here. Measured on apkradar on 2026-09-25, `compare/` answered

    404 {"detail":"Commit or branch '774f17e…' not found!"}

So a comparison is computed from two per-commit reports. The point of doing it
here rather than trusting a subtraction is that three things look like a loss and
are not:

* a **new file** has no previous coverage, so it cannot have lost any;
* a **deleted file** has not dropped to zero, it has gone;
* a file **measured in one report and missing from the other** is one of those two,
  never a drop.

Which leaves a loss as what it should be: a path present in both, covered less in
the second than in the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# In `line_coverage` each entry is [line number, status]. 0 is a hit and 1 a miss:
# on apkradar/publisher.py the fixture holds 140 entries, 130 zeros and 10 ones,
# against totals of 130 hits and 10 misses — and the ten lines marked 1 are
# 118, 177, 267, 288, 294, 307 and 321-324, exactly what coverage.py reported as
# missing in the same run. Partials appear as 2 where a project has branches.
LINE_MISSED = 1


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count(source: dict | None, key: str) -> int:
    try:
        return int((source or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class FileCoverage:
    path: str
    coverage: float
    lines: int = 0
    hits: int = 0
    misses: int = 0
    uncovered_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class Report:
    coverage: float | None
    lines: int = 0
    hits: int = 0
    misses: int = 0
    files: dict[str, FileCoverage] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> Report:
        """Read `/report/`, or a commit payload that carries one under `report`."""
        # A commit payload carries the report nested; /report/ is the report.
        # Bound to a name first so the narrowing is visible — as a conditional
        # expression the type still admitted None and mypy was right to say so.
        nested = payload.get("report")
        body: dict = nested if isinstance(nested, dict) else payload
        totals = body.get("totals") or {}

        files: dict[str, FileCoverage] = {}
        for entry in body.get("files") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            per_file = entry.get("totals") or {}
            coverage = _number(per_file.get("coverage"))
            if coverage is None:
                # A file with no percentage cannot take part in a comparison, and
                # treating it as 0 would report a loss nobody caused.
                continue
            files[name] = FileCoverage(
                path=name,
                coverage=coverage,
                lines=_count(per_file, "lines"),
                hits=_count(per_file, "hits"),
                misses=_count(per_file, "misses"),
                uncovered_lines=_uncovered(entry.get("line_coverage")),
            )

        return cls(
            coverage=_number(totals.get("coverage")),
            lines=_count(totals, "lines"),
            hits=_count(totals, "hits"),
            misses=_count(totals, "misses"),
            files=files,
        )


def _uncovered(line_coverage: Any) -> tuple[int, ...]:
    if not isinstance(line_coverage, list):
        return ()
    missed = []
    for pair in line_coverage:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            number, status = pair
            if status == LINE_MISSED and isinstance(number, int):
                missed.append(number)
    return tuple(sorted(missed))


@dataclass(frozen=True, order=True)
class Loss:
    """A file covered less than it was. Ordered worst-first by `delta`."""

    delta: float
    path: str = field(compare=False)
    before: float = field(compare=False, default=0.0)
    after: float = field(compare=False, default=0.0)
    newly_uncovered: int = field(compare=False, default=0)


@dataclass(frozen=True)
class Comparison:
    change: float
    losses: list[Loss]
    added: list[str]
    removed: list[str]

    @property
    def dropped_overall(self) -> bool:
        return self.change < 0


def compare(base: Report, head: Report) -> Comparison:
    """What changed between two reports, and nothing that did not."""
    losses = [
        Loss(
            delta=head.files[path].coverage - base.files[path].coverage,
            path=path,
            before=base.files[path].coverage,
            after=head.files[path].coverage,
            newly_uncovered=max(0, head.files[path].misses - base.files[path].misses),
        )
        for path in sorted(set(base.files) & set(head.files))
        if head.files[path].coverage < base.files[path].coverage
    ]

    overall = (head.coverage or 0.0) - (base.coverage or 0.0)
    return Comparison(
        change=overall,
        losses=sorted(losses),
        added=sorted(set(head.files) - set(base.files)),
        removed=sorted(set(base.files) - set(head.files)),
    )
