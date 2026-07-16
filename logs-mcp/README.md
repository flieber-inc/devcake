# devcake-logs-mcp

A standalone, read-only **stdio MCP server** that gives coding agents access
to a log platform. Responses are trimmed server-side for token economy: one
compact plain-text line per event, messages hard-truncated, limits clamped —
never left to the agent's discretion.

Built for [devcake](../README.md) Dev containers (every Dev image pip-installs
it; see `docs/07-dev-runtime.md` §6a), but it has no devcake dependency — any
MCP client can run it.

## Tools

| Tool | Purpose |
|---|---|
| `list_services` | services emitting logs in a window, with counts — discovery |
| `search_logs` | search events; paginated via a response-footer cursor |
| `get_log_context` | trace-correlated logs, or a ±window slice around a timestamp |
| `aggregate_logs` | counts grouped by a facet (service/status/host/attribute) |

## Backends

Selected by `DEVCAKE_LOGS_BACKEND` (default `datadog`); everything is
configured through environment variables — headless-friendly, no OAuth.

**Datadog** — `DD_API_KEY`, `DD_APP_KEY`, optional `DD_SITE`
(default `datadoghq.com`; EU orgs: `datadoghq.eu`).

**AWS CloudWatch Logs** (`DEVCAKE_LOGS_BACKEND=cloudwatch`) — Logs Insights
via a local stdlib SigV4 signer (no boto3). `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, optional `AWS_SESSION_TOKEN`;
`DEVCAKE_LOGS_GROUPS` (csv) targets specific log groups, otherwise up to 50
are auto-discovered. Needs `logs:StartQuery`, `logs:GetQueryResults`,
`logs:DescribeLogGroups`.

Other knobs: `DEVCAKE_LOGS_MAX_MSG` (message truncation, default 300 chars).

## Install & run

```sh
pip install .
devcake-logs-mcp --selftest        # no network: SDK imports + tools registered
```

Register with an MCP client, e.g. Claude Code:

```sh
claude mcp add devcake-logs \
  -e DD_API_KEY=$DD_API_KEY -e DD_APP_KEY=$DD_APP_KEY \
  -e DD_SITE=datadoghq.com -- devcake-logs-mcp
```

## Layout (keep it)

`core.py` is stdlib-only (the `LogBackend` seam, row shape, formatting);
`datadog.py`/`cloudwatch.py`/`sigv4.py` add only httpx; **`server.py` is the
only module importing the `mcp` SDK**. The test suite exercises everything
below `server.py` and therefore runs without `mcp` installed (devcake CI runs
it in the app-test image, which has pytest+httpx only); `server.py` itself is
covered by the `--selftest` smoke against the baked image.

## Tests

```sh
python -m pytest tests/ -q          # needs httpx + pytest on the path
```
