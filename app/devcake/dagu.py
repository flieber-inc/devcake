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
