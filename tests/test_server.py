"""The four tools, and the answers they are not allowed to give.

Written against mcp 2.2.0, where FastMCP is `MCPServer` and `call_tool` returns a
`CallToolResult` — checked against the installed SDK rather than remembered, since
the v1 name still appears in most examples on the web.

What these tests are really about is the same thing as the client's: an agent
reading this output will act on it, and three answers must stay apart.

* A repository nobody uploads to must not read as 0% coverage. exeradar has 417
  passing tests and no Codecov project; "0%" would be a lie that looks like a
  measurement, and an agent told that would go and write tests that already exist.
* A commit Codecov never received must say so. It happens on every push whose test
  job fails, because the upload step never runs.
* A failure to reach Codecov must not look like a clean report.

The owner is configuration, not something an agent should have to guess, so it
comes from CODECOV_OWNER and can be overridden per call.
"""

from __future__ import annotations

import json
import pathlib
import re

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from codecov_mcp import server as server_module

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
API = "https://api.codecov.io/api/v2/github/maksimtech"

TOOLS = {
    "repo_coverage",
    "commit_coverage",
    "pull_coverage",
    "files_that_lost_coverage",
}

OWNER_ENV = server_module.OWNER_ENV

HEAD = "9147b24792fa1cb897a486c3406c666e71d3d57b"
PARENT = "1e90d153404bd173b794a56cef254eb92ef5e4e6"
UNKNOWN = "774f17e68994bbef5d3bc3d36c260ac81d9740b5"


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["body"]


def mock(name: str, path: str, **params) -> None:
    route = respx.get(f"{API}{path}", params=params) if params else respx.get(f"{API}{path}")
    route.mock(return_value=httpx.Response(200, json=recorded(name)))


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv("CODECOV_OWNER", "maksimtech")
    monkeypatch.delenv("CODECOV_API_TOKEN", raising=False)


@pytest.fixture
def server():
    return server_module.build()


async def text_of(server, name: str, **arguments) -> str:
    outcome = await server.call_tool(name, arguments)
    return "\n".join(
        block.text for block in outcome.content if getattr(block, "text", None)
    )


# ── the surface ─────────────────────────────────────────────────────────────


async def test_the_four_tools_are_registered(server):
    assert {tool.name for tool in await server.list_tools()} == TOOLS


async def test_every_tool_says_what_it_does(server):
    for tool in await server.list_tools():
        assert tool.description and len(tool.description) > 30, tool.name


# ── a repository ────────────────────────────────────────────────────────────


@respx.mock
async def test_repo_coverage_reports_the_number(server):
    mock("repo_active", "/repos/apkradar/")

    answer = await text_of(server, "repo_coverage", repo="apkradar")

    assert "95.61" in answer
    assert "apkradar" in answer


@respx.mock
async def test_a_repository_without_uploads_does_not_read_as_zero(server):
    """No figure at all, and words that say why.

    Asserted as "no percentage anywhere" rather than as "the string 0% is
    absent": the explanation itself says the result is not 0%, and a substring
    check cannot tell a warning from the thing it warns about.
    """
    mock("repo_inactive", "/repos/exeradar/")

    answer = await text_of(server, "repo_coverage", repo="exeradar")

    assert re.search(r"\d+(\.\d+)?\s*%", answer) is None, answer
    assert "no coverage" in answer.lower() or "not activated" in answer.lower()
    assert "not a measurement of zero" in answer.lower()


@respx.mock
async def test_a_repository_whose_summary_lags_still_reports_its_number(server):
    """Where the defect was actually seen.

    On 2026-09-25 this tool answered "no coverage on Codecov ... This is the
    absence of a measurement, not a measurement of zero" for four repositories
    that had each just measured themselves, because /repos/<name>/ had not yet
    summarised the report its own branch was already carrying. That sentence is
    the most confident one this server owns, and it was wrong four times in a
    row.
    """
    mock("repo_totals_lagging", "/repos/exeradar/")
    mock("branch_measured", "/repos/exeradar/branches/main/")

    answer = await text_of(server, "repo_coverage", repo="exeradar")

    assert "86.07" in answer
    assert "no coverage" not in answer.lower()
    assert "absence of a measurement" not in answer.lower()


@respx.mock
async def test_a_repository_that_does_not_exist_is_an_error_not_an_answer(server):
    """A mistake in the question, not a state of the data. mcp 2.2 passes the
    message through when a tool raises ToolError, and the protocol layer turns
    that into an error result for the client."""
    respx.get(f"{API}/repos/nope/").mock(
        return_value=httpx.Response(404, json=recorded("repo_missing"))
    )

    with pytest.raises(ToolError) as caught:
        await server.call_tool("repo_coverage", {"repo": "nope"})

    assert "no repository" in str(caught.value).lower()
    assert "%" not in str(caught.value)


# ── a commit ────────────────────────────────────────────────────────────────


@respx.mock
async def test_commit_coverage_reports_the_number(server):
    mock("commit_head", f"/repos/apkradar/commits/{HEAD}/")

    answer = await text_of(server, "commit_coverage", repo="apkradar", sha=HEAD)

    assert "95.39" in answer
    assert HEAD[:8] in answer


@respx.mock
async def test_a_commit_codecov_never_saw_is_not_a_zero(server):
    respx.get(f"{API}/repos/apkradar/commits/{UNKNOWN}/").mock(
        return_value=httpx.Response(404, json=recorded("commit_unknown"))
    )

    answer = await text_of(server, "commit_coverage", repo="apkradar", sha=UNKNOWN)

    assert "0%" not in answer
    assert "no report" in answer.lower() or "never" in answer.lower()
    assert "upload" in answer.lower()


# ── a pull request ──────────────────────────────────────────────────────────


@respx.mock
async def test_pull_coverage_lists_the_recent_ones(server):
    mock("pulls", "/repos/apkradar/pulls/")

    answer = await text_of(server, "pull_coverage", repo="apkradar")

    assert "#14" in answer


@respx.mock
async def test_a_pull_request_with_no_head_report_says_not_measured(server):
    """Its base_totals are good and its head_totals are null. The difference
    between the two is not a number."""
    mock("pulls", "/repos/apkradar/pulls/")

    answer = await text_of(server, "pull_coverage", repo="apkradar")

    assert "not measured" in answer.lower()


# ── what lost coverage ──────────────────────────────────────────────────────


@respx.mock
async def test_the_files_that_lost_coverage_are_named(server):
    mock("report_parent", "/repos/apkradar/report/", sha=PARENT)
    mock("report_head", "/repos/apkradar/report/", sha=HEAD)

    answer = await text_of(
        server, "files_that_lost_coverage", repo="apkradar", base=PARENT, head=HEAD
    )

    assert "95.61" in answer and "95.39" in answer
    assert ".py" in answer


@respx.mock
async def test_a_missing_base_report_is_not_reported_as_no_change(server):
    """The case Codecov's own compare/ endpoint hits: one commit is in git and not
    here. "No files lost coverage" would be a different and wrong answer."""
    respx.get(f"{API}/repos/apkradar/report/", params={"sha": UNKNOWN}).mock(
        return_value=httpx.Response(404, json={"detail": "Not found."})
    )
    mock("report_head", "/repos/apkradar/report/", sha=HEAD)

    answer = await text_of(
        server, "files_that_lost_coverage", repo="apkradar", base=UNKNOWN, head=HEAD
    )

    assert "no files" not in answer.lower()
    assert "no report" in answer.lower()


@respx.mock
async def test_a_comparison_with_nothing_lost_says_nothing_was_lost(server):
    mock("report_head", "/repos/apkradar/report/", sha=HEAD)

    answer = await text_of(
        server, "files_that_lost_coverage", repo="apkradar", base=HEAD, head=HEAD
    )

    assert "no file" in answer.lower()


# ── failures ────────────────────────────────────────────────────────────────


@respx.mock
async def test_an_unreachable_codecov_is_reported_not_swallowed(server):
    respx.get(f"{API}/repos/apkradar/").mock(side_effect=httpx.ConnectError("no route"))

    with pytest.raises(ToolError) as caught:
        await server.call_tool("repo_coverage", {"repo": "apkradar"})

    assert "could not be reached" in str(caught.value).lower()
    assert "%" not in str(caught.value)


@respx.mock
async def test_rate_limiting_is_named(server):
    respx.get(f"{API}/repos/apkradar/").mock(return_value=httpx.Response(429))

    with pytest.raises(ToolError) as caught:
        await server.call_tool("repo_coverage", {"repo": "apkradar"})

    assert "rate" in str(caught.value).lower()


@respx.mock
async def test_a_failure_does_not_come_back_as_a_successful_call(server):
    """The requirement: an agent must not be handed a result that reads fine.

    I first wrote this against `CallToolResult.is_error`, which is not how this
    SDK works — a raising tool does not produce a result at this layer at all, and
    anything other than ToolError arrives as UnexpectedToolError with the reason
    stripped. ToolError is the documented path for an anticipated failure, keeps
    the message, and is what the session turns into an error for the client.
    """
    respx.get(f"{API}/repos/apkradar/").mock(return_value=httpx.Response(503))

    with pytest.raises(ToolError) as caught:
        await server.call_tool("repo_coverage", {"repo": "apkradar"})

    assert "503" in str(caught.value)
    assert type(caught.value).__name__ == "ToolError", "UnexpectedToolError loses the reason"


# ── configuration ───────────────────────────────────────────────────────────


@respx.mock
async def test_the_owner_can_be_given_per_call(monkeypatch):
    monkeypatch.delenv("CODECOV_OWNER", raising=False)
    route = respx.get("https://api.codecov.io/api/v2/github/someone/repos/thing/").mock(
        return_value=httpx.Response(200, json=recorded("repo_active"))
    )

    await text_of(server_module.build(), "repo_coverage", repo="thing", owner="someone")

    assert route.called


async def test_without_an_owner_it_asks_for_one(monkeypatch):
    monkeypatch.delenv("CODECOV_OWNER", raising=False)

    with pytest.raises(ToolError) as caught:
        await server_module.build().call_tool("repo_coverage", {"repo": "thing"})

    assert "owner" in str(caught.value).lower()
    assert OWNER_ENV in str(caught.value)


@respx.mock
async def test_no_token_is_needed_for_a_public_repository(server):
    route = respx.get(f"{API}/repos/apkradar/").mock(
        return_value=httpx.Response(200, json=recorded("repo_active"))
    )

    await text_of(server, "repo_coverage", repo="apkradar")

    assert "authorization" not in route.calls[0].request.headers


# ── the log belongs to the client, not to httpx ─────────────────────────────


@respx.mock
async def test_the_server_does_not_narrate_every_request(caplog, monkeypatch):
    """httpx logs each request at INFO, and an MCP server's stderr is read by the
    client: nine lines of "HTTP Request: GET …" for one answer, which is the defect
    apkradar had with androguard's parser log.

    The condition tested is the real one — something has turned INFO on globally,
    as the SDK does — because with the root logger at its default WARNING the
    records never appear and the test would pass without the fix. Note it does not
    raise httpx's own level to look: doing that is what defeats the fix.
    """
    import logging

    logging.getLogger().setLevel(logging.INFO)
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)

    mock("repo_active", "/repos/apkradar/")
    server = server_module.build()

    with caplog.at_level(logging.INFO):
        await text_of(server, "repo_coverage", repo="apkradar")

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert [r.getMessage() for r in caplog.records if r.name.startswith("http")] == []


async def test_the_silencing_is_not_a_blanket_mute(server, caplog):
    """Only httpx's request chatter. A warning from anywhere else still reaches
    the log, and so does httpx's own if something is actually wrong."""
    import logging

    with caplog.at_level(logging.WARNING):
        logging.getLogger("httpx").warning("something worth saying")
        logging.getLogger("codecov_mcp").warning("and this too")

    assert len(caplog.records) == 2
