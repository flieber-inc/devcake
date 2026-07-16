"""Datadog LogBackend — httpx only, transport= injectable (the same test
seam as the app repo's HTTP adapters). Auth is header-based API+APP keys:
non-interactive, fits the headless ephemeral Dev container."""

from __future__ import annotations

import httpx

from .core import BackendError, LogRow

_MAX_LIMIT = 100      # server-side clamp: token efficiency is enforced here


class DatadogBackend:
    def __init__(self, api_key: str, app_key: str, site: str = "datadoghq.com",
                 transport: httpx.BaseTransport | None = None):
        if not api_key or not app_key:
            raise BackendError(
                "DD_API_KEY/DD_APP_KEY not set — configure them as secret "
                "env vars on the Dev Type (admin Config page) and reference "
                "them from the MCP setup command")
        self._base_url = f"https://api.{site}"
        self._headers = {"DD-API-KEY": api_key,
                         "DD-APPLICATION-KEY": app_key}
        self._transport = transport

    def _post(self, path: str, body: dict) -> dict:
        # Per-call client: server.py builds a backend per tool invocation,
        # so a client held on self would leak one pool per tool call.
        try:
            with httpx.Client(base_url=self._base_url, headers=self._headers,
                              timeout=30, transport=self._transport) as client:
                resp = client.post(path, json=body)
        except httpx.HTTPError as e:      # never leak httpx types upward
            raise BackendError(f"datadog request failed: {e}") from e
        if resp.status_code >= 300:
            raise BackendError(f"datadog {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def search(self, query: str, from_time: str, to_time: str, limit: int,
               cursor: str | None = None, sort: str = "-timestamp",
               ) -> tuple[list[LogRow], str | None]:
        page: dict = {"limit": max(1, min(int(limit), _MAX_LIMIT))}
        if cursor:
            page["cursor"] = cursor
        data = self._post("/api/v2/logs/events/search", {
            "filter": {"query": query, "from": from_time, "to": to_time},
            "page": page,
            "sort": sort,
        })
        rows = [_row(ev) for ev in data.get("data") or []]
        next_cursor = ((data.get("meta") or {}).get("page") or {}).get("after")
        return rows, next_cursor

    def trace_filter(self, trace_id: str) -> str:
        return f"@dd.trace_id:{trace_id}"    # the indexed APM facet

    def aggregate(self, query: str, from_time: str, to_time: str,
                  group_by: str, limit: int) -> list[tuple[str, int]]:
        data = self._post("/api/v2/logs/analytics/aggregate", {
            "filter": {"query": query, "from": from_time, "to": to_time},
            "compute": [{"aggregation": "count"}],
            "group_by": [{"facet": group_by,
                          "limit": max(1, min(int(limit), _MAX_LIMIT)),
                          "sort": {"aggregation": "count", "order": "desc"}}],
        })
        buckets = ((data.get("data") or {}).get("buckets")) or []
        return [(str((b.get("by") or {}).get(group_by, "")),
                 int((b.get("computes") or {}).get("c0") or 0))
                for b in buckets]


def _row(ev: dict) -> LogRow:
    """Normalize one API event; tolerate sparse events (empty strings, never
    a crash) and drop everything not in LogRow — the custom-attribute blob
    never reaches the agent."""
    a = ev.get("attributes") or {}
    custom = a.get("attributes") or {}
    dd = custom.get("dd") or {}
    trace = dd.get("trace_id") or custom.get("dd.trace_id") or ""
    return LogRow(ts=str(a.get("timestamp") or ""),
                  status=str(a.get("status") or ""),
                  service=str(a.get("service") or ""),
                  host=str(a.get("host") or ""),
                  message=str(a.get("message") or ""),
                  log_id=str(ev.get("id") or ""),
                  trace_id=str(trace))
