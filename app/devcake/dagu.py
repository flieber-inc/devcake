"""ExecutorPort implementation: Dagu REST API (docs/13 §4 — endpoints verified live)."""

import json
import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("devcake.dagu")

DAGU_URL = os.environ.get("DAGU_URL", "http://dagu:8080")
DAG_NAME = "dev-run"
_AUTH = (os.environ.get("DAGU_USER", ""), os.environ.get("DAGU_PASSWORD", ""))


class DuplicateRun(Exception):
    """Dagu returned 409 already_exists for this dagRunId."""


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


class DaguExecutor:
    def __init__(self, base_url: str = DAGU_URL):
        self.base = base_url

    async def start(self, params: dict[str, str], dag_run_id: str) -> None:
        async with httpx.AsyncClient(timeout=10, auth=_AUTH) as client:
            resp = await client.post(
                f"{self.base}/api/v1/dags/{DAG_NAME}/start",
                json={"params": json.dumps(params), "dagRunId": dag_run_id},
            )
            if resp.status_code == 409:
                raise DuplicateRun(dag_run_id)
            resp.raise_for_status()

    async def stop(self, dag_run_id: str) -> bool:
        async with httpx.AsyncClient(timeout=10, auth=_AUTH) as client:
            resp = await client.post(
                f"{self.base}/api/v1/dag-runs/{DAG_NAME}/{dag_run_id}/stop"
            )
            return resp.status_code < 300

    async def status(self, dag_run_id: str) -> Optional[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10, auth=_AUTH) as client:
            resp = await client.get(f"{self.base}/api/v1/dag-runs/{DAG_NAME}/{dag_run_id}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def node_errors(self, dag_run_id: str) -> list[dict[str, str]]:
        """Step errors (with stderr tails) for one run; [] when none/unknown."""
        return extract_node_errors(await self.status(dag_run_id))

    async def stop_all(self) -> list[str]:
        """Stop every in-flight run of the dev-run DAG. Returns any error strings."""
        async with httpx.AsyncClient(timeout=30, auth=_AUTH) as client:
            resp = await client.post(f"{self.base}/api/v1/dags/{DAG_NAME}/stop-all")
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return list((resp.json() or {}).get("errors") or [])

    async def list_all_run_ids(self) -> list[str]:
        """Paginate through every historical dagRunId for the dev-run DAG."""
        ids: list[str] = []
        cursor: Optional[str] = None
        async with httpx.AsyncClient(timeout=30, auth=_AUTH) as client:
            while True:
                params: dict[str, Any] = {"limit": 500}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    f"{self.base}/api/v1/dag-runs/{DAG_NAME}", params=params
                )
                resp.raise_for_status()
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
        async with httpx.AsyncClient(timeout=15, auth=_AUTH) as client:
            resp = await client.delete(
                f"{self.base}/api/v1/dag-runs/{DAG_NAME}/{dag_run_id}"
            )
            if resp.status_code in (204, 404):
                return True
            if resp.status_code == 400:
                # still running / not deletable yet
                log.warning("dagu delete refused for %s: %s", dag_run_id, resp.text[:200])
                return False
            resp.raise_for_status()
            return True
