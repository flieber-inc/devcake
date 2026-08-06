"""/api/v1/health payload additions for the boot-sweep rework (incident
2026-08-01): `forge_probe` distinguishes "initial sweep pending" from "done"
so the SPA/operator can tell an empty forge map from an unprobed one, and
_branch_protection must probe bounded-parallel — it walks every work repo
and, sequential, stalled the first rich /health call the same way the boot
sweep stalled lifespan."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.api import health as health_mod
from devcake.config import AppConfig


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _forge_runtime(*, last_full_probe_at=None, health=None, forges=None):
    return SimpleNamespace(
        health=health if health is not None else {},
        breakers={},
        forges=forges if forges is not None else {},
        last_full_probe_at=last_full_probe_at,
        instance=lambda name: None,
    )


def _payload(fr, monkeypatch, repo_cache=None, workspaces=None):
    async def _true(*a, **k):
        return True

    async def _ingest():
        return {"ok": True, "detail": ""}

    monkeypatch.setattr(health_mod, "_check_redis", _true)
    monkeypatch.setattr(health_mod, "_check_http", _true)
    monkeypatch.setattr(health_mod, "_oo_ingest_check", _ingest)
    health_mod.reset_protection_cache()
    return run_coro(health_mod.build_health_payload(
        config=AppConfig(), dev_types={}, managers={}, stewards={},
        forge_runtime=fr, shared_breakers={},
        store=SimpleNamespace(active=lambda: []),
        internal_forge=None,
        poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={}),
        repo_cache=repo_cache, workspaces=workspaces))


def test_forge_probe_pending_then_complete(monkeypatch):
    fr = _forge_runtime(forges={"a": object(), "b": object()},
                        health={"a": {"ok": True}})
    got = _payload(fr, monkeypatch)["forge_probe"]
    assert got == {"complete": False, "completed_at": None,
                   "probed": 1, "configured": 2}

    done = datetime.now(timezone.utc)
    fr.last_full_probe_at = done
    fr.health["b"] = {"ok": True}
    got = _payload(fr, monkeypatch)["forge_probe"]
    assert got == {"complete": True, "completed_at": done.isoformat(),
                   "probed": 2, "configured": 2}


def test_unused_repo_names_and_payload_block(monkeypatch):
    from devcake.config import PMOInstance, RepoInstance

    cfg = AppConfig(
        repos=[RepoInstance(name=n, url=f"https://github.com/o/{n}")
               for n in ("work1", "work2", "refdocs", "orphan1", "orphan2")],
        pmos=[
            PMOInstance(name="alpha", team_key="A",
                        repos=["work1"], reference_repos=["refdocs"]),
            PMOInstance(name="beta", team_key="B", repos=["work2"]),
        ])
    assert health_mod.unused_repo_names(cfg) == ["orphan1", "orphan2"]

    # zero-PMO config: every adapter is unused
    lonely = AppConfig(repos=[RepoInstance(name="solo",
                                           url="https://github.com/o/solo")])
    assert health_mod.unused_repo_names(lonely) == ["solo"]
    assert health_mod.unused_repo_names(AppConfig()) == []

    async def _true(*a, **k):
        return True

    async def _ingest():
        return {"ok": True, "detail": ""}

    monkeypatch.setattr(health_mod, "_check_redis", _true)
    monkeypatch.setattr(health_mod, "_check_http", _true)
    monkeypatch.setattr(health_mod, "_oo_ingest_check", _ingest)
    health_mod.reset_protection_cache()
    payload = run_coro(health_mod.build_health_payload(
        config=cfg, dev_types={}, managers={}, stewards={},
        forge_runtime=_forge_runtime(), shared_breakers={},
        store=SimpleNamespace(active=lambda: []),
        internal_forge=None,
        poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={}),
        repo_cache=None))
    assert payload["unused_repos"] == {
        "count": 2, "names": ["orphan1", "orphan2"], "configured": 5}


def test_branch_protection_probes_concurrently(monkeypatch):
    started = {"n": 0}
    all_started = asyncio.Event()

    class _Prot:
        def model_dump(self):
            return {"rendezvous": True}

    class _Forge:
        async def default_branch_protection(self, branch):
            started["n"] += 1
            if started["n"] == 3:
                all_started.set()
            # sequential execution times out here → the except path stores
            # None; only genuine parallelism lets all three return _Prot
            await asyncio.wait_for(all_started.wait(), timeout=2)
            return _Prot()

    inst = SimpleNamespace(reference_only=False, default_branch="main")
    fr = SimpleNamespace(forges={f"r{i}": _Forge() for i in range(3)},
                         instance=lambda name: inst)
    health_mod.reset_protection_cache()
    out = run_coro(asyncio.wait_for(health_mod._branch_protection(fr), timeout=5))
    assert out == {f"r{i}": {"rendezvous": True} for i in range(3)}
    health_mod.reset_protection_cache()   # don't leak the fake into others


def test_repo_mirror_block_shape(monkeypatch):
    """ADR-0024: knobs echoed, ledger + volume probe surfaced; None-safe
    without a cache (tests, hypothetical consumers)."""
    from devcake.domain.repo_mirror import NullRepoCache

    class Cache(NullRepoCache):
        def __init__(self):
            super().__init__()
            self.volume_error = "EACCES: not writable"

        def health_map(self):
            return {"alpha": {"ok": False, "detail": "fetch: 500",
                              "synced_at": None, "attempted_at": None,
                              "auth": False}}

        def disk_stats(self):
            return {"total_bytes": 100, "free_bytes": 50}

    fr = _forge_runtime()
    got = _payload(fr, monkeypatch, repo_cache=Cache())["repo_mirror"]
    assert got["volume_error"] == "EACCES: not writable"
    assert got["mirrors"]["alpha"]["ok"] is False
    assert got["disk"] == {"total_bytes": 100, "free_bytes": 50}
    assert got["lfs"] is False and got["sync_max_age_seconds"] == 0
    # no cache injected (default) → block still serves, empty
    bare = _payload(fr, monkeypatch)["repo_mirror"]
    assert bare["mirrors"] == {} and bare["volume_error"] is None


def test_workspaces_block_shape(monkeypatch):
    """ADR-0025: leaked count + volume probe + disk; None-safe without a
    store (tests, hypothetical consumers)."""
    from devcake.domain.workspaces import NullWorkspaceStore

    class WS(NullWorkspaceStore):
        def __init__(self):
            super().__init__()
            self.volume_error = "EACCES: base not writable"

        def leaked_count(self, store):
            return 2

        def disk_stats(self):
            return {"total_bytes": 100, "free_bytes": 9}

    fr = _forge_runtime()
    got = _payload(fr, monkeypatch, workspaces=WS())["workspaces"]
    assert got["volume_error"] == "EACCES: base not writable"
    assert got["leaked"] == 2
    assert got["disk"] == {"total_bytes": 100, "free_bytes": 9}
    bare = _payload(fr, monkeypatch)["workspaces"]
    assert bare == {"volume_error": None, "leaked": 0, "disk": None}
