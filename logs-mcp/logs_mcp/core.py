"""Backend-neutral core: the LogBackend seam, the normalized row shape.

Stdlib only — app-test imports this without the `mcp` SDK installed. New
platforms (Loki, CloudWatch, …) implement LogBackend and plug into
make_backend; the MCP tool surface in server.py never changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

_DEFAULT_MAX_MSG = 300    # chars per message; DEVCAKE_LOGS_MAX_MSG overrides


class BackendError(Exception):
    """Actionable failure, surfaced verbatim as MCP tool output (the server
    never crashes mid-mission over a backend problem)."""


@dataclass(frozen=True)
class LogRow:
    """One normalized log event — everything the agent gets, nothing else
    (the platform's noisy attribute blob is dropped at the backend)."""
    ts: str
    status: str
    service: str
    host: str
    message: str
    log_id: str
    trace_id: str


def format_rows(rows: list[LogRow], cursor: str | None = None,
                max_msg: int | None = None) -> str:
    """One compact plain-text line per event — the cheapest token shape for
    an agent. Empty fields collapse; messages hard-truncate (token efficiency
    is enforced server-side, never left to the agent); the footer teaches
    pagination. An empty result reads as a definite answer, not an error."""
    if not rows:
        return "no logs matched — widen the time range or relax the query."
    cap = (max_msg if max_msg is not None
           else int(os.environ.get("DEVCAKE_LOGS_MAX_MSG", _DEFAULT_MAX_MSG)))
    lines = []
    for r in rows:
        msg = r.message if len(r.message) <= cap else r.message[:cap] + "…"
        head = " ".join(x for x in (r.ts, r.status.upper(), r.service) if x)
        ids = " ".join(x for x in (f"id={r.log_id}" if r.log_id else "",
                                   f"trace={r.trace_id}" if r.trace_id else "")
                       if x)
        line = " ".join(x for x in (head, msg) if x)
        if ids:
            line += f" ({ids})"
        lines.append(line)
    footer = f"{len(rows)} events shown."
    if cursor:
        footer += f" more: pass cursor='{cursor}'"
    lines.append(footer)
    return "\n".join(lines)


class LogBackend(Protocol):
    def search(self, query: str, from_time: str, to_time: str, limit: int,
               cursor: str | None = None, sort: str = "-timestamp",
               ) -> tuple[list[LogRow], str | None]: ...

    def aggregate(self, query: str, from_time: str, to_time: str,
                  group_by: str, limit: int) -> list[tuple[str, int]]: ...

    def trace_filter(self, trace_id: str) -> str:
        """The backend-dialect query fragment selecting one trace's logs
        (trace correlation is dialect, so it lives below the seam)."""
        ...


def make_backend() -> LogBackend:
    """Env-configured factory (server.py builds per tool call): the seam
    where new platforms plug in — a loki/cloudwatch module implements
    LogBackend and gets an entry here; the MCP tool surface never changes.
    Lazy imports keep this module stdlib-only at import time."""
    kind = os.environ.get("DEVCAKE_LOGS_BACKEND", "datadog")
    if kind == "datadog":
        from .datadog import DatadogBackend
        return DatadogBackend(
            api_key=os.environ.get("DD_API_KEY", ""),
            app_key=os.environ.get("DD_APP_KEY", ""),
            site=os.environ.get("DD_SITE", "datadoghq.com"))
    if kind == "cloudwatch":
        from .cloudwatch import CloudWatchBackend
        groups = tuple(g.strip() for g in
                       os.environ.get("DEVCAKE_LOGS_GROUPS", "").split(",")
                       if g.strip())
        return CloudWatchBackend(
            access_key=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            secret_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            region=os.environ.get("AWS_REGION", ""),
            session_token=os.environ.get("AWS_SESSION_TOKEN", ""),
            log_groups=groups)
    raise BackendError(f"unknown log backend {kind!r} — supported: "
                       "datadog, cloudwatch")
