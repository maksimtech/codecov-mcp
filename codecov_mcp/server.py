"""
CodecovMCP — the four tools, and the discipline of not rounding an absence to zero.

Built on mcp 2.2, where FastMCP is `MCPServer`. Every tool answers in prose: the
reader is a model that will act on the answer, and "exeradar is at 0%" would send
it to write tests for a repository that already has 417 of them. So the three
answers Codecov can give stay apart in the wording as well as in the types —
measured, never measured, could not ask.

Failures are reported through the protocol's own error flag as well as in the text,
because an agent that checks only the flag must not be told a call went fine.
"""

from __future__ import annotations

import functools
import logging
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from codecov_mcp.client import Codecov, CodecovError, NotMeasured
from codecov_mcp.coverage import Report, compare

OWNER_ENV = "CODECOV_OWNER"


def _reports_failures(tool):
    """Turn a failure to answer into the SDK's anticipated-failure path.

    `ToolError` is the one exception mcp 2.2 passes through with its message: any
    other becomes `UnexpectedToolError` and the reason is lost on the way to the
    caller. Which matters most exactly when something is broken.

    Only failures come through here. "No coverage was ever uploaded" and "Codecov
    holds no report for that commit" are answers, returned as text by the tools
    that can receive them — an agent should reason about those, not retry them.
    """
    @functools.wraps(tool)
    def answering(*args, **kwargs):
        try:
            return tool(*args, **kwargs)
        except CodecovError as exc:
            raise ToolError(str(exc)) from exc

    return answering


class MissingOwner(CodecovError):
    """Nobody said whose repositories to read."""


def _codecov(owner: str | None) -> Codecov:
    chosen = (owner or os.environ.get(OWNER_ENV) or "").strip()
    if not chosen:
        raise MissingOwner(
            f"no repository owner: pass owner=… or set {OWNER_ENV} "
            "(the GitHub user or organisation the repository belongs to)"
        )
    return Codecov(owner=chosen)


def _percent(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2f}%"


def quieten_http_log() -> None:
    """Keep httpx's per-request INFO out of the client's log.

    An MCP server's stderr is read by whoever launched it, and httpx narrates
    every call: "HTTP Request: GET https://api.codecov.io/… 200 OK", nine lines
    for one answer. Raised to WARNING rather than disabled, so httpx still speaks
    when something is wrong, and scoped to its own logger so nothing else is
    silenced — apkradar had the same problem with androguard's parser log and the
    fix there had the same shape.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build() -> MCPServer:
    """The server, assembled. Separate from the module so tests get a fresh one."""
    quieten_http_log()
    server = MCPServer(
        name="codecov",
        instructions=(
            "Read coverage from Codecov. A repository with no uploads has no "
            "coverage rather than 0%, and a commit Codecov never received is not a "
            "commit measured at zero — the answers say which case they are."
        ),
    )

    @server.tool(
        description=(
            "Coverage of a repository on Codecov, on its default branch. Says so "
            "explicitly when the repository has never been uploaded to, which is not "
            "the same as 0% coverage, and reads the branch directly when the "
            "repository summary has not caught up with it yet."
        )
    )
    @_reports_failures
    def repo_coverage(repo: str, owner: str | None = None) -> str:
        codecov = _codecov(owner)
        found = codecov.repo(repo)
        if not found.measured:
            return (
                f"{codecov.owner}/{found.name}: no coverage on Codecov. {found.why}. "
                "This is the absence of a measurement, not a measurement of zero."
            )
        return (
            f"{codecov.owner}/{found.name} on {found.branch or 'the default branch'}: "
            f"{_percent(found.coverage)} — {found.hits} of {found.lines} lines covered, "
            f"{found.misses} missed. Last updated {found.updated or 'at an unknown time'}."
        )

    @server.tool(
        description=(
            "Coverage of one commit. A commit that exists in git and was never "
            "uploaded — which happens whenever the test job failed, since the upload "
            "step then does not run — is reported as having no report."
        )
    )
    @_reports_failures
    def commit_coverage(repo: str, sha: str, owner: str | None = None) -> str:
        codecov = _codecov(owner)
        try:
            commit = codecov.commit(repo, sha)
        except NotMeasured as exc:
            # An answer: the commit is real and Codecov holds nothing for it.
            return str(exc)
        ci = {True: "CI passed", False: "CI failed", None: "CI result unknown"}[commit.ci_passed]
        return (
            f"{commit.sha[:8]} on {commit.branch or 'an unknown branch'}: "
            f"{_percent(commit.coverage)} — {commit.hits} of {commit.lines} lines, "
            f"{commit.misses} missed. {ci}. "
            f"{commit.message.splitlines()[0] if commit.message else ''}"
        ).strip()

    @server.tool(
        description=(
            "Coverage of recent pull requests, base against head. A pull request whose "
            "head was never measured is reported as not measured rather than as a drop "
            "to zero."
        )
    )
    @_reports_failures
    def pull_coverage(repo: str, limit: int = 5, owner: str | None = None) -> str:
        codecov = _codecov(owner)
        pulls = codecov.pulls(repo, limit=limit)
        if not pulls:
            return f"{codecov.owner}/{repo}: Codecov lists no pull requests."

        lines = [f"{codecov.owner}/{repo} — {len(pulls)} most recent pull requests:"]
        for pull in pulls:
            if pull.measured:
                change = pull.change or 0.0
                movement = f"{change:+.2f} points ({_percent(pull.base_coverage)} → {_percent(pull.head_coverage)})"
            else:
                movement = (
                    f"not measured — base {_percent(pull.base_coverage)}, "
                    "head has no report"
                )
            lines.append(f"  #{pull.number} [{pull.state or 'unknown'}] {movement} · {pull.title}")
        return "\n".join(lines)

    @server.tool(
        description=(
            "Which files are covered less at one commit than at another, worst first. "
            "Computed from the two reports rather than from Codecov's compare endpoint, "
            "which needs both commits to be in Codecov; a missing report is reported as "
            "missing and never as 'nothing changed'. Files added or deleted between the "
            "two are listed as such, since neither has lost coverage."
        )
    )
    @_reports_failures
    def files_that_lost_coverage(
        repo: str, base: str, head: str, owner: str | None = None
    ) -> str:
        codecov = _codecov(owner)
        try:
            before = Report.from_payload(codecov.report_payload(repo, sha=base))
            after = Report.from_payload(codecov.report_payload(repo, sha=head))
        except NotMeasured as exc:
            # Not "nothing changed": one side of the comparison does not exist.
            return (
                f"{exc}\nNo comparison is possible, which is not the same as no "
                "file having lost coverage."
            )
        outcome = compare(base=before, head=after)

        headline = (
            f"{codecov.owner}/{repo}: {_percent(before.coverage)} at {base[:8]} → "
            f"{_percent(after.coverage)} at {head[:8]} ({outcome.change:+.2f} points)."
        )
        if not outcome.losses:
            body = ["No file is covered less than it was."]
        else:
            body = [f"{len(outcome.losses)} file(s) covered less than before:"]
            body += [
                f"  {loss.path}: {loss.before:.2f}% → {loss.after:.2f}% "
                f"({loss.delta:+.2f}, {loss.newly_uncovered} more uncovered line(s))"
                for loss in outcome.losses
            ]
        if outcome.added:
            body.append(f"Added, so with no earlier coverage: {', '.join(outcome.added)}")
        if outcome.removed:
            body.append(f"No longer in the report: {', '.join(outcome.removed)}")
        return "\n".join([headline, *body])

    return server


def main() -> None:
    """Entry point for `python -m codecov_mcp` and for the MCP client."""
    build().run(transport="stdio")


if __name__ == "__main__":
    main()
