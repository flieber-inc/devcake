"""Connection-test HTTP contract: ok:False always carries `error`.

The SPA Test connection buttons print `tr.error`. Probe DTOs use `detail`.
Independent expected values are the planted detail strings.
"""

from __future__ import annotations

import asyncio

from devcake.config import AppConfig, PMOInstance, RepoInstance
from devcake.ports.pmo import PMOHealth


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_forge_failed_probe_puts_detail_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    repo = RepoInstance(
        name="devcakerepo", forge="github",
        url="https://github.com/flieber-inc/devcake")
    secrets_store.write_connection_secret("repo", "devcakerepo", "token", "ghp_test")
    reason = (
        "repository access failed (HTTP 404); for a fine-grained PAT, "
        "select this repository and grant Contents and Pull requests read/write")

    class Runtime:
        def get(self, name):
            return object()

        async def refresh_health(self, name):
            return {
                "ok": False,
                "repository": "flieber-inc/devcake",
                "can_push": False,
                "detail": reason,
            }

    out = _run(cs.test_forge(
        "devcakerepo", config=AppConfig(repos=[repo]), forge_runtime=Runtime()))
    assert out["ok"] is False
    assert out["error"] == reason


def test_pmo_failed_probe_puts_detail_on_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    from devcake.api import connections_service as cs

    inst = PMOInstance(name="linear", system="linear", team_key="DEV")
    secrets_store.write_connection_secret("pmo", "linear", "api_key", "lin_test")
    reason = "team not found"

    class Adapter:
        async def health_probe(self, team_ref):
            return PMOHealth(ok=False, detail=reason)

        async def list_all(self, team_ref):
            return []

    class Mgr:
        pmo = Adapter()

    out = _run(cs.test_pmo(
        "linear", config=AppConfig(pmos=[inst]), managers={"linear": Mgr()}))
    assert out["ok"] is False
    assert out["error"] == reason
