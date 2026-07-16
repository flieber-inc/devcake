"""devcake-logs MCP server (stdio) — the ONLY module importing the `mcp` SDK
(core/datadog stay importable by app-test, which doesn't install it).

Registered per Dev Type via mcp_setup_commands (docs/08 §7), e.g.:
  claude mcp add devcake-logs -e DD_API_KEY=$DD_API_KEY \
      -e DD_APP_KEY=$DD_APP_KEY -e DD_SITE=datadoghq.com -- devcake-logs-mcp

Every tool builds the backend lazily and relays BackendError as tool output:
an unconfigured or failing platform yields an actionable message to the
agent, never a dead server mid-mission.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

from .core import BackendError, format_rows, make_backend

app = FastMCP("devcake-logs")


def _aggregate_text(query: str, from_time: str, to_time: str,
                    group_by: str, limit: int) -> str:
    pairs = make_backend().aggregate(query, from_time, to_time,
                                     group_by, limit)
    if not pairs:
        return "no logs matched — widen the time range or relax the query."
    return "\n".join(f"{k or '(none)'}: {n}" for k, n in pairs)


@app.tool()
def search_logs(query: str, from_time: str = "now-15m",
                to_time: str = "now", limit: int = 25,
                cursor: str | None = None) -> str:
    """Search the platform's logs. Query dialect follows the configured
    backend — Datadog: `service:payment-api status:error "card declined"`;
    CloudWatch: plain text = message filter, or a full Logs Insights query
    when it contains a pipe. from_time/to_time accept relative (`now-15m`,
    `now-2h`) or ISO8601 times. Returns one compact line per event, newest
    first; if the footer names a cursor, call again with it for the next
    page."""
    try:
        rows, cur = make_backend().search(query, from_time, to_time,
                                          limit, cursor=cursor)
        return format_rows(rows, cursor=cur)
    except BackendError as e:
        return f"error: {e}"


@app.tool()
def get_log_context(trace_id: str = "", timestamp: str = "", query: str = "",
                    window_seconds: int = 120, limit: int = 50) -> str:
    """Context around one event from a search_logs row: pass its trace= id
    for all trace-correlated logs (on CloudWatch this is a message-substring
    match — prefer timestamp mode there), or its timestamp for a
    ±window_seconds slice. query optionally narrows. Ascending time order —
    read top to bottom."""
    try:
        if not trace_id and not timestamp:
            return ("error: pass trace_id or timestamp — both come from "
                    "search_logs rows")
        backend = make_backend()
        q = " ".join(x for x in (query,
                                 backend.trace_filter(trace_id) if trace_id
                                 else "") if x) or "*"
        if timestamp:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            window = timedelta(seconds=window_seconds)
            frm, to = (ts - window).isoformat(), (ts + window).isoformat()
        else:
            frm, to = "now-1h", "now"
        rows, cur = backend.search(q, frm, to, limit, sort="timestamp")
        return format_rows(rows, cursor=cur)
    except ValueError:
        return f"error: timestamp {timestamp!r} is not ISO8601"
    except BackendError as e:
        return f"error: {e}"


@app.tool()
def aggregate_logs(query: str = "*", from_time: str = "now-1h",
                   to_time: str = "now", group_by: str = "service",
                   limit: int = 10) -> str:
    """Count matching logs grouped by a facet (`service`, `status`, `host`,
    or any `@attribute`), highest first — use to see which services or
    statuses dominate before drilling in with search_logs."""
    try:
        return _aggregate_text(query, from_time, to_time, group_by, limit)
    except BackendError as e:
        return f"error: {e}"


@app.tool()
def list_services(from_time: str = "now-1h") -> str:
    """Services emitting logs in the window, with event counts — start here
    to discover what exists before searching."""
    try:
        return _aggregate_text("*", from_time, "now", "service", 50)
    except BackendError as e:
        return f"error: {e}"


def _selftest() -> int:
    """No network: prove the SDK imports and all tools registered — CI runs
    `devcake-logs-mcp --selftest` against the baked image."""
    import asyncio
    tools = sorted(t.name for t in asyncio.run(app.list_tools()))
    expected = ["aggregate_logs", "get_log_context", "list_services",
                "search_logs"]
    if tools != expected:
        print(f"selftest FAILED: tools {tools} != {expected}",
              file=sys.stderr)
        return 1
    print("devcake-logs-mcp ok:", ", ".join(tools))
    return 0


def main() -> None:
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    app.run()   # stdio transport


if __name__ == "__main__":
    main()
