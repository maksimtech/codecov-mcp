"""The server has to speak the protocol from a real process, over real pipes.

Everything else in this suite calls the tools in-process, which proves the answers
and not the plumbing. Two things can only break out here:

* the console script, which is what a client launches;
* stdout, which *is* the protocol channel. Anything printed there corrupts the
  stream — a stray print, a library writing its log to the wrong stream — and the
  client's error for that is unhelpful, so the cheapest place to catch it is a
  handshake.

Skipped when the package has not been installed into the environment running the
tests, because then there is no console script to launch.
"""

from __future__ import annotations

import shutil
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {"repo_coverage", "commit_coverage", "pull_coverage", "files_that_lost_coverage"}


def entry_point() -> str:
    found = shutil.which("codecov-mcp") or shutil.which(
        "codecov-mcp", path=str(__import__("pathlib").Path(sys.executable).parent)
    )
    if not found:
        pytest.skip("codecov-mcp is not on PATH: pip install -e . first")
    return found


async def test_a_client_can_connect_and_list_the_tools():
    parameters = StdioServerParameters(
        command=entry_point(),
        env={"CODECOV_OWNER": "maksimtech"},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()

    assert {tool.name for tool in listed.tools} == TOOLS


async def test_the_server_describes_itself_to_the_client():
    """The instructions travel with the handshake, and they are where the
    "absence is not zero" rule is stated for whoever reads the tool list."""
    parameters = StdioServerParameters(
        command=entry_point(),
        env={"CODECOV_OWNER": "maksimtech"},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        outcome = await session.initialize()

    # server_info, not serverInfo: the wire uses camelCase, this SDK does not.
    assert outcome.server_info.name == "codecov"
    assert "0%" in (outcome.instructions or "") or "zero" in (outcome.instructions or "")
