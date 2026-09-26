"""
CodecovMCP — reading Codecov's v2 API without inventing answers.

Three failure modes are kept apart here, because Codecov reports all three and a
client written on optimism turns them into one number:

* **not measured** — the repository exists and nobody ever uploaded to it
  (`activated: false`, no totals), or Codecov never received the commit. That is
  the absence of a measurement, not a measurement of zero.
* **not found** — the repository or the resource does not exist at all.
* **failed** — Codecov could not be reached, refused the request, or answered
  with something that is not JSON. Never an empty result: an empty result is what
  a healthy repository with no findings looks like.

The token is optional. The v2 API answers for public repositories without one, so
this server can be useful while holding no secret at all; `CODECOV_API_TOKEN` is
read from the environment when a private repository needs it, and it is an API
token from Settings → Access, not an upload token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

API_ROOT = "https://api.codecov.io/api/v2"
TOKEN_ENV = "CODECOV_API_TOKEN"
DEFAULT_TIMEOUT = 20.0


class CodecovError(Exception):
    """Codecov could not answer. Never raised for "nothing to report"."""


class NotFound(CodecovError):
    """The repository or resource does not exist."""


class NotMeasured(CodecovError):
    """It exists, and Codecov holds no coverage for it.

    Distinct from NotFound because the answer to the user is different: one is
    "check the name", the other is "the upload never happened".
    """


def _number(value: Any) -> float | None:
    """Codecov sends coverage as a string sometimes, and as null when unknown."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count(totals: dict | None, key: str) -> int:
    try:
        return int((totals or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class RepoCoverage:
    """A repository's latest coverage, or the reason there is none."""

    name: str
    branch: str | None = None
    coverage: float | None = None
    lines: int = 0
    hits: int = 0
    misses: int = 0
    private: bool | None = None
    updated: str | None = None
    why: str = ""

    @property
    def measured(self) -> bool:
        return self.coverage is not None


@dataclass(frozen=True)
class CommitCoverage:
    sha: str
    coverage: float | None = None
    lines: int = 0
    hits: int = 0
    misses: int = 0
    message: str = ""
    timestamp: str | None = None
    branch: str | None = None
    ci_passed: bool | None = None
    state: str | None = None
    parent: str | None = None


@dataclass(frozen=True)
class PullCoverage:
    """A pull request, whose head is often not measured yet.

    `head_totals: null` beside a good `base_totals` is an ordinary shape — a PR
    that never ran, or whose run failed before the upload. Subtracting one from
    the other would report a collapse in coverage that never happened, so
    `change` stays None and `measured` says why.
    """

    number: int
    title: str = ""
    state: str | None = None
    base_coverage: float | None = None
    head_coverage: float | None = None
    ci_passed: bool | None = None
    author: str | None = None

    @property
    def measured(self) -> bool:
        return self.base_coverage is not None and self.head_coverage is not None

    @property
    def change(self) -> float | None:
        if not self.measured:
            return None
        return self.head_coverage - self.base_coverage  # type: ignore[operator]


@dataclass
class Codecov:
    """A read-only client for one owner's repositories."""

    owner: str
    service: str = "github"
    token: str | None = None
    api_root: str = API_ROOT
    timeout: float = DEFAULT_TIMEOUT
    _client: httpx.Client | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.token is None:
            self.token = (os.environ.get(TOKEN_ENV) or "").strip() or None
        elif isinstance(self.token, str):
            self.token = self.token.strip() or None

    # ── the one place that talks to the network ─────────────────────────────

    @property
    def base(self) -> str:
        return f"{self.api_root}/{self.service}/{self.owner}"

    def _headers(self) -> dict[str, str]:
        # No token, no header: sending "Bearer " with nothing after it would be
        # worse than sending nothing, and public repositories need neither.
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.base}{path}"
        try:
            client = self._client or httpx.Client(timeout=self.timeout)
            try:
                response = client.get(url, params=params, headers=self._headers())
            finally:
                if self._client is None:
                    client.close()
        except httpx.HTTPError as exc:
            raise CodecovError(f"Codecov could not be reached: {type(exc).__name__}") from exc

        if response.status_code == 404:
            raise NotFound(self._detail(response) or f"not found: {url}")
        if response.status_code == 429:
            raise CodecovError("Codecov is rate limiting this client (HTTP 429)")
        if response.status_code >= 400:
            raise CodecovError(
                f"Codecov answered HTTP {response.status_code}"
                + (f": {self._detail(response)}" if self._detail(response) else "")
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CodecovError(
                f"Codecov answered HTTP {response.status_code} with a body that is not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise CodecovError(f"Codecov answered with {type(payload).__name__}, expected an object")
        return payload

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        return str(body.get("detail", "")) if isinstance(body, dict) else ""

    # ── repositories ───────────────────────────────────────────────────────

    def repo(self, repo: str) -> RepoCoverage:
        try:
            payload = self._get(f"/repos/{repo}/")
        except NotFound as exc:
            raise NotFound(f"no repository {self.owner}/{repo} on Codecov: {exc}") from exc

        branch = payload.get("branch")
        totals = payload.get("totals")
        coverage = _number((totals or {}).get("coverage"))
        updated = payload.get("updatestamp")
        why = ""

        if coverage is None and not payload.get("activated"):
            why = (
                # No numeral, not even to deny one: a reader skimming for a
                # figure must not find one here.
                "the repository is on Codecov but not activated: no report has ever "
                "been uploaded, so there is no coverage to report — which is not the "
                "same as zero coverage"
            )
        elif coverage is None:
            # This summary lags behind the reports it summarises. On 2026-09-25
            # four repositories were activated within minutes of each other, and
            # for a window afterwards this endpoint answered `activated: true`
            # with `totals: null` while their default branch already carried the
            # figure — 86.07 for exeradar, read from the branch while the summary
            # still said nothing. By the next morning all five had caught up.
            #
            # Asking only here turned "not summarised yet" into "not measured",
            # which is the single mistake this server exists to avoid, and it
            # made it four times in a row. So ask the branch before concluding.
            head = self._branch_head(repo, branch)
            totals = (head or {}).get("totals")
            coverage = _number((totals or {}).get("coverage"))
            if coverage is not None:
                # Dated by the commit the figure came from. The summary's own
                # timestamp belongs to whatever it last managed to summarise,
                # which during the window is something older by definition.
                updated = (head or {}).get("timestamp") or updated
            else:
                why = (
                    "the repository is activated and neither its summary nor the head "
                    f"of {branch or 'its default branch'} carries a total"
                )

        return RepoCoverage(
            name=payload.get("name") or repo,
            branch=branch,
            coverage=coverage,
            lines=_count(totals, "lines"),
            hits=_count(totals, "hits"),
            misses=_count(totals, "misses"),
            private=payload.get("private"),
            updated=updated,
            why=why,
        )

    def _branch_head(self, repo: str, branch: str | None) -> dict | None:
        """The head commit of a branch, or None when Codecov has no record of it.

        A 404 is an answer: there is no such branch, or nothing was ever pushed
        to it. Everything else is left to propagate — a timeout, a 429 or a 5xx
        means the question could not be answered, and answering it anyway with
        "no coverage" would be the defect this method was added to fix, wearing
        one more request as a disguise.
        """
        if not branch:
            return None
        try:
            payload = self._get(f"/repos/{repo}/branches/{branch}/")
        except NotFound:
            return None
        head = payload.get("head_commit")
        return head if isinstance(head, dict) else None

    # ── commits ────────────────────────────────────────────────────────────

    def commit(self, repo: str, sha: str) -> CommitCoverage:
        try:
            payload = self._get(f"/repos/{repo}/commits/{sha}/")
        except NotFound as exc:
            raise NotMeasured(
                f"Codecov holds no report for commit {sha[:8]} of {self.owner}/{repo}: "
                "the commit may exist in git and never have been uploaded — a failed "
                f"test job skips the upload step ({exc})"
            ) from exc
        return self._commit(payload)

    def commits(self, repo: str, limit: int = 10) -> list[CommitCoverage]:
        payload = self._get(f"/repos/{repo}/commits/", {"page_size": max(1, limit)})
        results = payload.get("results")
        if not isinstance(results, list):
            raise CodecovError("Codecov answered the commit list without results")
        return [self._commit(item) for item in results[:limit] if isinstance(item, dict)]

    @staticmethod
    def _commit(payload: dict) -> CommitCoverage:
        totals = payload.get("totals")
        return CommitCoverage(
            sha=str(payload.get("commitid") or ""),
            coverage=_number((totals or {}).get("coverage")),
            lines=_count(totals, "lines"),
            hits=_count(totals, "hits"),
            misses=_count(totals, "misses"),
            message=str(payload.get("message") or ""),
            timestamp=payload.get("timestamp"),
            branch=payload.get("branch"),
            ci_passed=payload.get("ci_passed"),
            state=payload.get("state"),
            parent=payload.get("parent"),
        )

    # ── pull requests ──────────────────────────────────────────────────────

    def pulls(self, repo: str, limit: int = 10) -> list[PullCoverage]:
        payload = self._get(f"/repos/{repo}/pulls/", {"page_size": max(1, limit)})
        results = payload.get("results")
        if not isinstance(results, list):
            raise CodecovError("Codecov answered the pull request list without results")

        pulls = []
        for item in results[:limit]:
            if not isinstance(item, dict):
                continue
            author = item.get("author") or {}
            pulls.append(
                PullCoverage(
                    number=int(item.get("pullid") or 0),
                    title=str(item.get("title") or ""),
                    state=item.get("state"),
                    base_coverage=_number((item.get("base_totals") or {}).get("coverage")),
                    head_coverage=_number((item.get("head_totals") or {}).get("coverage")),
                    ci_passed=item.get("ci_passed"),
                    author=author.get("username") if isinstance(author, dict) else None,
                )
            )
        return pulls

    # ── reports, which the comparison is built from ────────────────────────

    def report_payload(
        self,
        repo: str,
        sha: str | None = None,
        branch: str | None = None,
        path: str | None = None,
    ) -> dict:
        """The raw per-file report. `coverage.Report` gives it a shape.

        Asked per commit rather than through Codecov's `compare/` endpoint: that
        one needs both commits to be in Codecov and answers 404 naming the missing
        one, which is an answer worth keeping but not a diff.
        """
        params: dict[str, Any] = {}
        if sha:
            params["sha"] = sha
        if branch:
            params["branch"] = branch
        if path:
            params["path"] = path
        try:
            return self._get(f"/repos/{repo}/report/", params or None)
        except NotFound as exc:
            what = sha[:8] if sha else (branch or "the default branch")
            raise NotMeasured(
                f"Codecov holds no report for {what} of {self.owner}/{repo}: {exc}"
            ) from exc
