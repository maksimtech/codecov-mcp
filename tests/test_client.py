"""Reading Codecov without inventing answers.

The whole point of this client is the distinction the radars had to learn the hard
way: "bad", "nothing there" and "could not tell" are three answers, and a tool
that collapses them into one number is worse than no tool. Codecov gives all three
and they are easy to flatten by accident:

* a repository nobody ever uploaded to answers HTTP 200 with `activated: false`
  and no totals — that is not 0% coverage, it is no measurement;
* a pull request whose head has not been measured answers with `head_totals: null`
  beside a perfectly good `base_totals`;
* a commit git knows and Codecov never received answers 404 — as
  774f17e68994bbef5d3bc3d36c260ac81d9740b5 does, because the test job failed on
  that push so the upload step never ran;
* a repository that does not exist answers 404 too, with a different message.

Every fixture here is a real response recorded on 2026-09-25 from
api.codecov.io/api/v2, including all four cases above. The token is optional: the
v2 API answers for public repositories without one, which is why this server can
be useful without holding a secret at all.
"""

from __future__ import annotations

import json
import pathlib

import httpx
import pytest
import respx

from codecov_mcp.client import (
    Codecov,
    CodecovError,
    NotFound,
    NotMeasured,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
API = "https://api.codecov.io/api/v2/github/maksimtech"


def recorded(name: str) -> tuple[int, dict]:
    """A response as the service actually sent it."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data["status"], data["body"]


def mock(name: str, path: str) -> None:
    status, body = recorded(name)
    respx.get(f"{API}{path}").mock(return_value=httpx.Response(status, json=body))


@pytest.fixture
def codecov() -> Codecov:
    return Codecov(owner="maksimtech", service="github")


# ── the repository ──────────────────────────────────────────────────────────


@respx.mock
def test_an_active_repository_reports_its_coverage(codecov):
    mock("repo_active", "/repos/apkradar/")

    repo = codecov.repo("apkradar")

    assert repo.coverage == pytest.approx(95.61)
    assert repo.lines == 1300
    assert repo.hits == 1243
    assert repo.branch == "main"
    assert repo.measured is True


@respx.mock
def test_a_repository_nobody_uploads_to_has_no_coverage(codecov):
    """exeradar is registered and has never received a report. `activated: false`
    with no totals is not zero coverage — reporting 0% would say the tests cover
    nothing, and in that repository 417 tests pass."""
    mock("repo_inactive", "/repos/exeradar/")

    repo = codecov.repo("exeradar")

    assert repo.measured is False
    assert repo.coverage is None
    assert "not activated" in repo.why.lower()


@respx.mock
def test_a_repository_that_does_not_exist_is_an_error_not_an_absence(codecov):
    mock("repo_missing", "/repos/nonesuch-xyz/")

    with pytest.raises(NotFound) as caught:
        codecov.repo("nonesuch-xyz")

    assert "nonesuch-xyz" in str(caught.value)


@respx.mock
def test_a_repository_whose_summary_lags_is_read_from_its_branch(codecov):
    """The defect this fallback exists for.

    On 2026-09-25 four repositories were activated within minutes of each other.
    For a window after the first report landed, /repos/<name>/ answered
    `activated: true` with `totals: null` while /repos/<name>/branches/main/
    already carried the figure. Asking only the first one, this client said the
    coverage was not there — absence of measurement — four times in a row, for
    four repositories that had just measured themselves.

    "Could not tell yet" is not "nothing there". Collapsing the two is the whole
    thing this server was written not to do, and it did it anyway.
    """
    mock("repo_totals_lagging", "/repos/exeradar/")
    mock("branch_measured", "/repos/exeradar/branches/main/")

    repo = codecov.repo("exeradar")

    assert repo.measured is True
    assert repo.coverage == pytest.approx(86.07)
    assert repo.lines == 1257
    assert repo.hits == 1082
    assert repo.misses == 175


@respx.mock
def test_the_branch_it_falls_back_to_is_the_one_the_repository_names(codecov):
    """Not a hardcoded "main". A repository on `develop` would otherwise be
    reported as unmeasured while its coverage sat one request away."""
    status, body = recorded("repo_totals_lagging")
    body = {**body, "branch": "develop"}
    respx.get(f"{API}/repos/exeradar/").mock(return_value=httpx.Response(status, json=body))
    mock("branch_measured", "/repos/exeradar/branches/develop/")

    repo = codecov.repo("exeradar")

    assert repo.coverage == pytest.approx(86.07)
    assert repo.branch == "develop"


@respx.mock
def test_the_figure_is_dated_by_the_commit_it_came_from(codecov):
    """Taking the number from the branch and the timestamp from the repository
    summary would date a fresh measurement with a stale clock — the two are out
    of step by definition while the summary is catching up."""
    mock("repo_totals_lagging", "/repos/exeradar/")
    mock("branch_measured", "/repos/exeradar/branches/main/")

    repo = codecov.repo("exeradar")

    assert repo.updated == "2026-09-25T23:53:46Z"


@respx.mock
def test_a_repository_with_no_branch_record_still_has_no_coverage(codecov):
    """The fallback must not turn a real absence into an error. A 404 on the
    branch means Codecov has no record of it, which is an answer."""
    mock("repo_totals_lagging", "/repos/exeradar/")
    mock("branch_missing", "/repos/exeradar/branches/main/")

    repo = codecov.repo("exeradar")

    assert repo.measured is False
    assert repo.coverage is None
    assert "main" in repo.why


@respx.mock
def test_it_does_not_ask_about_a_branch_when_nobody_has_uploaded(codecov):
    """`activated: false` is already a complete answer. Asking the branch would
    spend a request to be told the same thing, and would make the no-upload case
    depend on a second endpoint answering."""
    mock("repo_inactive", "/repos/exeradar/")
    branch = respx.get(f"{API}/repos/exeradar/branches/main/").mock(
        return_value=httpx.Response(200, json={})
    )

    repo = codecov.repo("exeradar")

    assert repo.measured is False
    assert "not activated" in repo.why.lower()
    assert not branch.called


@respx.mock
def test_a_summary_that_has_caught_up_asks_nothing_further(codecov):
    """The fallback is for the window, not for every call. apkradar has had
    repo-level totals throughout."""
    mock("repo_active", "/repos/apkradar/")
    branch = respx.get(f"{API}/repos/apkradar/branches/main/").mock(
        return_value=httpx.Response(200, json={})
    )

    repo = codecov.repo("apkradar")

    assert repo.coverage == pytest.approx(95.61)
    assert not branch.called


@respx.mock
def test_a_branch_lookup_that_fails_is_not_read_as_an_absence(codecov):
    """5xx on the fallback means the question could not be answered. Reporting
    "no coverage" there would be the original defect with an extra step."""
    mock("repo_totals_lagging", "/repos/exeradar/")
    respx.get(f"{API}/repos/exeradar/branches/main/").mock(
        return_value=httpx.Response(503, json={"detail": "unavailable"})
    )

    with pytest.raises(CodecovError):
        codecov.repo("exeradar")


# ── commits ─────────────────────────────────────────────────────────────────


@respx.mock
def test_a_commit_reports_the_coverage_of_its_report(codecov):
    sha = "9147b24792fa1cb897a486c3406c666e71d3d57b"
    mock("commit_head", f"/repos/apkradar/commits/{sha}/")

    commit = codecov.commit("apkradar", sha)

    assert commit.coverage == pytest.approx(95.39)
    assert commit.branch == "main"
    assert commit.ci_passed is True
    assert commit.message.startswith("fix(docs)")


@respx.mock
def test_a_commit_codecov_never_received_says_so(codecov):
    """It exists in git. The upload step did not run because the tests failed on
    that push, so Codecov has nothing — which is not the same as a commit that
    was measured at zero."""
    sha = "774f17e68994bbef5d3bc3d36c260ac81d9740b5"
    mock("commit_unknown", f"/repos/apkradar/commits/{sha}/")

    with pytest.raises(NotMeasured) as caught:
        codecov.commit("apkradar", sha)

    assert sha[:8] in str(caught.value)


@respx.mock
def test_the_commit_list_comes_back_newest_first(codecov):
    mock("commits", "/repos/apkradar/commits/")

    commits = codecov.commits("apkradar", limit=3)

    assert [c.sha[:7] for c in commits][0] == "9147b24"
    assert all(c.coverage is not None for c in commits)


# ── pull requests ───────────────────────────────────────────────────────────


@respx.mock
def test_a_pull_request_with_no_head_report_is_not_a_drop_to_zero(codecov):
    """`head_totals: null` beside a good `base_totals` is the shape of a PR whose
    head was never measured. Subtracting one from the other would report a
    catastrophic loss of coverage that never happened."""
    mock("pulls", "/repos/apkradar/pulls/")

    pulls = codecov.pulls("apkradar", limit=3)
    unmeasured = [p for p in pulls if p.head_coverage is None]

    assert unmeasured, "the recorded page contains such a pull request"
    for pull in unmeasured:
        assert pull.change is None
        assert pull.measured is False


@respx.mock
def test_a_pull_request_carries_its_identity(codecov):
    mock("pulls", "/repos/apkradar/pulls/")

    first = codecov.pulls("apkradar", limit=3)[0]

    assert first.number == 14
    assert first.state == "merged"
    assert first.title


# ── failures that must not read as results ──────────────────────────────────


@respx.mock
def test_a_server_error_is_raised_not_swallowed(codecov):
    respx.get(f"{API}/repos/apkradar/").mock(return_value=httpx.Response(503))

    with pytest.raises(CodecovError) as caught:
        codecov.repo("apkradar")

    assert "503" in str(caught.value)


@respx.mock
def test_a_network_failure_is_raised(codecov):
    respx.get(f"{API}/repos/apkradar/").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(CodecovError):
        codecov.repo("apkradar")


@respx.mock
def test_a_body_that_is_not_json_is_raised(codecov):
    respx.get(f"{API}/repos/apkradar/").mock(
        return_value=httpx.Response(200, text="<html>proxy</html>")
    )

    with pytest.raises(CodecovError):
        codecov.repo("apkradar")


@respx.mock
def test_rate_limiting_says_what_it_is(codecov):
    respx.get(f"{API}/repos/apkradar/").mock(return_value=httpx.Response(429))

    with pytest.raises(CodecovError) as caught:
        codecov.repo("apkradar")

    assert "rate" in str(caught.value).lower()


# ── the token is optional ───────────────────────────────────────────────────


@respx.mock
def test_no_token_means_no_authorization_header(codecov):
    route = respx.get(f"{API}/repos/apkradar/").mock(
        return_value=httpx.Response(200, json=recorded("repo_active")[1])
    )

    codecov.repo("apkradar")

    assert "authorization" not in route.calls[0].request.headers


@respx.mock
def test_a_token_is_sent_as_a_bearer_when_there_is_one():
    route = respx.get(f"{API}/repos/apkradar/").mock(
        return_value=httpx.Response(200, json=recorded("repo_active")[1])
    )

    Codecov(owner="maksimtech", token="secret-token").repo("apkradar")

    assert route.calls[0].request.headers["authorization"] == "Bearer secret-token"


def test_the_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("CODECOV_API_TOKEN", "  from-env  ")

    assert Codecov(owner="maksimtech").token == "from-env"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_token_is_no_token(monkeypatch, value):
    monkeypatch.setenv("CODECOV_API_TOKEN", value)

    assert Codecov(owner="maksimtech").token is None
