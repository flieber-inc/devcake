"""ExecutorPort implementation: Dagu REST API (docs/13 §4 — endpoints verified live).

Wire failures surface as ports.executor.ExecutorError / DuplicateRun —
never raw httpx types (same contract as forge_request / ForgeError).
"""

import json
import logging
import os
from typing import Any, Optional

import httpx

from ...ports.executor import DuplicateRun, ExecutorError

log = logging.getLogger("devcake.dagu")

DAGU_URL = os.environ.get("DAGU_URL", "http://dagu:8080")
DAG_NAME = "dev-run"
_AUTH = (os.environ.get("DAGU_USER", ""), os.environ.get("DAGU_PASSWORD", ""))

# Re-export so `from adapters.dagu import DuplicateRun` keeps working for
# composition-root and API call sites that already import the adapter package.
__all__ = ["DAGU_URL", "DAG_NAME", "DaguExecutor", "DuplicateRun", "ExecutorError",
           "extract_node_errors"]


def extract_node_errors(status: Optional[dict]) -> list[dict[str, str]]:
    """Per-step failure details from a dag-run record. Dagu embeds the container's
    stderr tail in each failed node's `error` field, and the record outlives the
    container (keep_container: false) — this is the only post-mortem source."""
    detail = (status or {}).get("dagRunDetails") or {}
    out = []
    for node in detail.get("nodes") or []:
        err = node.get("error")
        if not err:
            continue
        out.append({"step": str((node.get("step") or {}).get("name", "")),
                    "status": str(node.get("statusLabel", node.get("status", ""))),
                    "error": str(err)})
    return out


def _raise_for_status(resp: httpx.Response, *, what: str) -> None:
    """Map non-2xx to ExecutorError without leaking HTTPStatusError."""
    if resp.status_code < 400:
        return
    raise ExecutorError(
        f"dagu {what} → {resp.status_code}: {resp.text[:200]}",
        status=resp.status_code,
    )


class DaguExecutor:
    def __init__(
        self,
        base_url: str = DAGU_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        auth: tuple[str, str] | None = None,
    ):
        self.base = base_url
        self._transport = transport        # tests inject MockTransport
        self._auth = _AUTH if auth is None else auth

    def _client(self, timeout: float = 10) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=timeout, auth=self._auth, transport=self._transport)

    async def _request(self, method: str, path: str, *, timeout: float = 10,
                       **kwargs) -> httpx.Response:
        """One Dagu call; network failures → ExecutorError(status=None)."""
        try:
            async with self._client(timeout) as client:
                return await client.request(method, f"{self.base}{path}", **kwargs)
        except httpx.HTTPError as e:
            raise ExecutorError(
                f"dagu {method} {path} → network: {e}", status=None) from e

    async def start(self, params: dict[str, str], dag_run_id: str) -> None:
        resp = await self._request(
            "POST", f"/api/v1/dags/{DAG_NAME}/start",
            json={"params": json.dumps(params), "dagRunId": dag_run_id},
        )
        if resp.status_code == 409:
            raise DuplicateRun(dag_run_id)
        _raise_for_status(resp, what=f"start {dag_run_id}")

    async def stop(self, dag_run_id: str) -> bool:
        # bool-success contract: network/HTTP failures report False so kill
        # paths can continue teardown without catching wire types.
        try:
            resp = await self._request(
                "POST", f"/api/v1/dag-runs/{DAG_NAME}/{dag_run_id}/stop")
        except ExecutorError:
            return False
        return resp.status_code < 300

    async def status(self, dag_run_id: str) -> Optional[dict[str, Any]]:
        resp = await self._request(
            "GET", f"/api/v1/dag-runs/{DAG_NAME}/{dag_run_id}")
        if resp.status_code == 404:
            return None
        _raise_for_status(resp, what=f"status {dag_run_id}")
        return resp.json()

    async def node_errors(self, dag_run_id: str) -> list[dict[str, str]]:
        """Step errors (with stderr tails) for one run; [] when none/unknown."""
        return extract_node_errors(await self.status(dag_run_id))

    async def stop_all(self) -> list[str]:
        """Stop every in-flight run of the dev-run DAG. Returns any error strings."""
        resp = await self._request(
            "POST", f"/api/v1/dags/{DAG_NAME}/stop-all", timeout=30)
        if resp.status_code == 404:
            return []
        _raise_for_status(resp, what="stop-all")
        return list((resp.json() or {}).get("errors") or [])

    async def list_all_run_ids(self) -> list[str]:
        """Paginate through every historical dagRunId for the dev-run DAG."""
        ids: list[str] = []
        cursor: Optional[str] = None
        while True:
            params: dict[str, Any] = {"limit": 500}
            if cursor:
                params["cursor"] = cursor
            resp = await self._request(
                "GET", f"/api/v1/dag-runs/{DAG_NAME}", timeout=30, params=params)
            _raise_for_status(resp, what="list dag-runs")
            body = resp.json() or {}
            for r in body.get("dagRuns") or []:
                rid = r.get("dagRunId")
                if rid:
                    ids.append(rid)
            cursor = body.get("nextCursor") or None
            if not cursor:
                break
        return ids

    async def delete(self, dag_run_id: str) -> bool:
        """Permanently remove one DAG-run record. True if deleted (or already gone)."""
        try:
            resp = await self._request(
                "DELETE", f"/api/v1/dag-runs/{DAG_NAME}/{dag_run_id}", timeout=15)
        except ExecutorError:
            return False
        if resp.status_code in (204, 404):
            return True
        if resp.status_code == 400:
            # still running / not deletable yet
            log.warning("dagu delete refused for %s: %s", dag_run_id, resp.text[:200])
            return False
        if resp.status_code >= 400:
            log.warning("dagu delete failed for %s: %s", dag_run_id, resp.text[:200])
            return False
        return True
