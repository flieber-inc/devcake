"""AWS CloudWatch Logs backend — Logs Insights over httpx with the local
SigV4 signer (no boto3: keeps the image lean and the MockTransport test
seam). Auth: static keys + optional STS session token via DevType.secret_env
(AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN / AWS_REGION).

Insights is an async API: search = StartQuery → poll GetQueryResults until
Complete. There is no pagination cursor — results are limit-bounded.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from .core import BackendError, LogRow
from .sigv4 import sign_headers

_MAX_LIMIT = 100          # server-side clamp, same rule as datadog.py
_TARGET_PREFIX = "Logs_20140328."
_FIELDS = "fields @timestamp, @message, @logStream, @log"
_RELATIVE = re.compile(r"^now(?:-(\d+)([smhd]))?$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _epoch(spec: str, now: datetime | None = None) -> int:
    """'now' / 'now-15m' / ISO8601 → epoch seconds (naive times = UTC)."""
    now = now if now is not None else datetime.now(timezone.utc)
    m = _RELATIVE.fullmatch(spec.strip())
    if m:
        back = int(m.group(1)) * _UNIT[m.group(2)] if m.group(1) else 0
        return int((now - timedelta(seconds=back)).timestamp())
    try:
        ts = datetime.fromisoformat(spec.replace("Z", "+00:00"))
    except ValueError:
        raise BackendError(
            f"time {spec!r} not understood — use now, now-15m/2h/1d, "
            "or ISO8601") from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())


def _insights_query(query: str, sort: str, limit: int) -> str:
    """Bare text wraps as a message filter; anything with a pipe is passed
    through as a raw Logs Insights query (power users)."""
    order = "asc" if sort == "timestamp" else "desc"
    tail = f" | sort @timestamp {order} | limit {limit}"
    q = query.strip()
    if "|" in q:
        # Deliberate escape hatch, not an oversight: the raw query goes to a
        # read-only API under the caller's own credentials — nothing to harden.
        return q
    if q in ("", "*"):
        return _FIELDS + tail
    escaped = q.replace('"', '\\"')
    return f'{_FIELDS} | filter @message like "{escaped}"{tail}'


class CloudWatchBackend:
    def __init__(self, access_key: str, secret_key: str, region: str,
                 session_token: str = "", log_groups: tuple[str, ...] = (),
                 transport: httpx.BaseTransport | None = None,
                 poll_interval: float = 0.4, poll_timeout: float = 25.0):
        if not access_key or not secret_key or not region:
            raise BackendError(
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_REGION not set "
                "— configure them as secret env vars on the Dev Type (admin "
                "Config page) and reference them from the MCP setup command")
        self._creds = (access_key, secret_key, session_token or None)
        self._region = region
        self._groups = tuple(log_groups)
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._url = f"https://logs.{region}.amazonaws.com/"
        self._transport = transport

    def _call(self, client: httpx.Client, action: str, body: dict) -> dict:
        payload = json.dumps(body).encode()
        access, secret, token = self._creds
        headers = sign_headers(
            method="POST", url=self._url, region=self._region, service="logs",
            headers={"Content-Type": "application/x-amz-json-1.1",
                     "X-Amz-Target": _TARGET_PREFIX + action},
            body=payload, access_key=access, secret_key=secret,
            session_token=token)
        try:
            resp = client.post(self._url, content=payload, headers=headers)
        except httpx.HTTPError as e:  # never leak httpx types upward
            raise BackendError(f"cloudwatch request failed: {e}") from e
        if resp.status_code >= 300:
            raise BackendError(
                f"cloudwatch {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _log_groups(self, client: httpx.Client) -> list[str]:
        if self._groups:
            return list(self._groups)
        data = self._call(client, "DescribeLogGroups", {"limit": 50})
        groups = [g.get("logGroupName", "")
                  for g in data.get("logGroups") or []]
        groups = [g for g in groups if g]
        if not groups:
            raise BackendError(
                "no CloudWatch log groups found — set DEVCAKE_LOGS_GROUPS "
                "(comma-separated) in the MCP setup command")
        return groups

    def _query(self, query_string: str, from_time: str, to_time: str,
               limit: int) -> list[list[dict]]:
        # One client per public call (discovery + StartQuery + polls share
        # the pool), closed on return: server.py builds a backend per tool
        # invocation, so a client held on self would leak one pool per call.
        with httpx.Client(timeout=30, transport=self._transport) as client:
            start = self._call(client, "StartQuery", {
                "queryString": query_string,
                "logGroupNames": self._log_groups(client),
                "startTime": _epoch(from_time),
                "endTime": _epoch(to_time),
                "limit": limit,
            })
            query_id = start.get("queryId", "")
            deadline = time.monotonic() + self._poll_timeout
            while True:
                data = self._call(client, "GetQueryResults",
                                  {"queryId": query_id})
                status = data.get("status", "Unknown")
                if status == "Complete":
                    return data.get("results") or []
                if status in ("Failed", "Cancelled", "Timeout"):
                    raise BackendError(f"cloudwatch query {status.lower()}")
                if time.monotonic() >= deadline:
                    raise BackendError(
                        "cloudwatch query still running after "
                        f"{self._poll_timeout:.0f}s — narrow the time range")
                time.sleep(self._poll_interval)

    def search(self, query: str, from_time: str, to_time: str, limit: int,
               cursor: str | None = None, sort: str = "-timestamp",
               ) -> tuple[list[LogRow], str | None]:
        limit = max(1, min(int(limit), _MAX_LIMIT))
        results = self._query(_insights_query(query, sort, limit),
                              from_time, to_time, limit)
        return [_row(fields) for fields in results], None

    def trace_filter(self, trace_id: str) -> str:
        """No trace facet in CloudWatch Logs — X-Ray ids commonly appear in
        the message text, so the id becomes a substring filter."""
        return trace_id

    def aggregate(self, query: str, from_time: str, to_time: str,
                  group_by: str, limit: int) -> list[tuple[str, int]]:
        facet = {"service": "@log", "host": "@logStream"}.get(group_by,
                                                              group_by)
        limit = max(1, min(int(limit), _MAX_LIMIT))
        q = _insights_query(query, "-timestamp", limit)
        # replace the row-shaped tail with a stats aggregation
        base = q.split(" | sort ")[0].replace(_FIELDS, "").lstrip(" |")
        stats = f"stats count() as devcake_count by {facet}"
        parts = [p for p in (base, stats) if p]
        results = self._query(" | ".join(parts) +
                              f" | sort devcake_count desc | limit {limit}",
                              from_time, to_time, limit)
        out: list[tuple[str, int]] = []
        for fields in results:
            kv = {f.get("field", ""): f.get("value", "") for f in fields}
            count = int(float(kv.pop("devcake_count", 0) or 0))
            key = next(iter(kv.values()), "")
            out.append((_strip_account(str(key)), count))
        return out


def _row(fields: list[dict]) -> LogRow:
    kv = {f.get("field", ""): f.get("value", "") for f in fields}
    return LogRow(ts=str(kv.get("@timestamp") or ""),
                  status="",                       # no status concept in CW
                  service=_strip_account(str(kv.get("@log") or "")),
                  host=str(kv.get("@logStream") or ""),
                  message=str(kv.get("@message") or ""),
                  log_id=str(kv.get("@ptr") or ""),
                  trace_id="")


def _strip_account(log_field: str) -> str:
    """@log values arrive as '123456789012:/group/name' — drop the account."""
    return log_field.split(":", 1)[1] if ":" in log_field else log_field
