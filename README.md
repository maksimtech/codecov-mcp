# CodecovMCP

Coverage from [Codecov](https://codecov.io) inside an MCP client, read-only, and
without a secret unless one is needed.

**Status: four tools, 60 tests.** There is no official Codecov MCP server; the
community ones hold an API token and the one on npm was last published in March
2025. This is small enough to own: one HTTP client, one comparison, four tools.

## Why it does not report 0%

Codecov distinguishes three things and they are easy to flatten into one number:

| What Codecov says | What it means | What this server answers |
|---|---|---|
| `totals.coverage: 95.61` | measured | the figure, with lines covered and missed |
| `activated: false`, no totals | nobody has ever uploaded | "no coverage on Codecov", no figure at all |
| `404` on a commit | Codecov never received it | "no report for that commit", and why |
| `head_totals: null` on a pull request | its head was never measured | "not measured", not a drop to zero |
| `activated: true`, no repo totals | the summary has not caught up | the figure, read from the default branch |
| unreachable, 429, 5xx, not JSON | the question could not be answered | an error to the client, never an empty result |

The distinction is not pedantry, and this server has already failed it once. On
2026-09-25 four repositories were activated on Codecov within minutes of each
other, and for a window afterwards `/repos/<name>/` answered `activated: true`
with no totals while each one's default branch already carried its figure. Reading
only the summary, `repo_coverage` reported "the absence of a measurement" four
times in a row for repositories that had just measured themselves — the most
confident sentence here, wrong. Hence the fallback in the table above.

The other two cases are the ones it was written for. `exeradar` was registered
with no project at all: told "0%", an agent would go and write tests that already
exist — there were 417 of them at the time. And a commit whose test job failed
never reaches the upload step, so it is absent from Codecov while present in git;
reporting that as zero coverage would blame the code for a failure in the
pipeline.

## Tools

| Tool | Answers |
|---|---|
| `repo_coverage(repo, owner?)` | coverage of a repository, or why there is none |
| `commit_coverage(repo, sha, owner?)` | coverage of one commit, with its CI result |
| `pull_coverage(repo, limit?, owner?)` | recent pull requests, base against head |
| `files_that_lost_coverage(repo, base, head, owner?)` | per-file drops, worst first, plus files added or deleted |

`files_that_lost_coverage` is computed from two per-commit reports rather than from
Codecov's `compare/` endpoint, which needs both commits to be in Codecov and
answers `404` naming the missing one. A missing side is reported as missing — "no
comparison is possible" is a different answer from "no file lost coverage".

Example, against this account on 2026-09-25:

```
maksimtech/apkradar: 95.61% at 1e90d153 → 95.39% at 9147b247 (-0.22 points).
2 file(s) covered less than before:
  apkradar/scanner.py: 96.55% → 94.73% (-1.82, 5 more uncovered line(s))
  apkradar/cli.py: 96.87% → 96.86% (-0.01, 3 more uncovered line(s))
Added, so with no earlier coverage: apkradar/publisher.py
```

## Install

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
```

Then add it to Claude Code, user scope:

```bash
claude mcp add --scope user codecov \
  --env CODECOV_OWNER=maksimtech \
  -- <path>/.venv/Scripts/codecov-mcp
```

or as JSON in `~/.claude.json`:

```json
{
  "mcpServers": {
    "codecov": {
      "type": "stdio",
      "command": "<path>/.venv/Scripts/codecov-mcp.exe",
      "env": { "CODECOV_OWNER": "maksimtech" }
    }
  }
}
```

## Configuration

| Variable | Needed | What it is |
|---|---|---|
| `CODECOV_OWNER` | yes, unless `owner` is passed per call | the GitHub user or organisation |
| `CODECOV_API_TOKEN` | only for private repositories | an **API** token from Settings → Access, not an upload token |

The v2 API answers for public repositories without authentication, so no token is
sent when there is none: a `Bearer` header with nothing after it is worse than no
header. A blank variable counts as absent.

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

Every fixture under `tests/fixtures/` is a response recorded from
`api.codecov.io/api/v2`, status code included, and the awkward ones are
deliberate: a repository with no uploads, a commit Codecov never received, a pull
request with no head report, a 404 from `compare/`.

One is not a recording, and says so in a `_derived` key of its own.
`repo_totals_lagging.json` is the real 2026-09-26 response for `exeradar` with
`totals` put back to null — the state seen the night before, whose window had
closed by the time there was a test to catch it in. Every other field is as the
service sent it, and the distinction is in the file because in this project
"recorded" and "constructed" are not the same word. `line_coverage` was
stripped from three of them to keep them small and the removal is noted inside
each — `report_one_file.json` keeps it, because the per-file path uses it.

The encoding of `line_coverage` was read from the data rather than from
documentation: `[line, status]` with 0 a hit and 1 a miss. On
`apkradar/publisher.py` the fixture holds 140 entries, 130 zeros and 10 ones,
against totals of 130 hits and 10 misses — and the ten lines marked 1 are exactly
the ones `coverage.py` reported missing in the same run.

## Licence

MIT.
