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
        internal=set(),
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
    health_mod.reset_health_caches()
    return run_coro(health_mod.build_health_payload(
        config=AppConfig(), dev_types={}, managers={}, stewards={},
        forge_runtime=fr, shared_breakers={},
        store=SimpleNamespace(active=lambda: []),
        internal_forge=None,
        poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={}),
        repo_cache=repo_cache, workspaces=workspaces))


def test_health_payload_carries_harness_pins(monkeypatch):
    fr = _forge_runtime(last_full_probe_at=datetime.now(timezone.utc))
    got = _payload(fr, monkeypatch)
    pins = got["harness_pins"]
    assert "digest" in pins
    assert "sentinel" in pins
    assert "templates" in pins
    assert "bake_status" in got
    assert got["bake_status"]["baker_alive"] is False


def test_health_payload_reads_host_bake_status(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    (tmp_path / "harness_bake_status.json").write_text(json.dumps({
        "state": "baking",
        "jobs": [{"template": "grok-build", "cli_version": "1.0.4",
                  "state": "baking"}],
    }))
    fr = _forge_runtime(last_full_probe_at=datetime.now(timezone.utc))
    got = _payload(fr, monkeypatch)
    assert got["bake_status"]["state"] == "baking"
    assert got["bake_status"]["jobs"][0]["cli_version"] == "1.0.4"
    # baking with no heartbeat is a crash mid-bake, not a live compile
    assert got["bake_status"]["baker_alive"] is False


def test_health_exposes_digest_and_receipt_summary(monkeypatch):
    from devcake.config import DevType
    from devcake.house_pins import SENTINEL_DIGEST
    from devcake.staffing import receipt_summary

    class Store:
        def get(self, **kw):
            if kw["template"] == "grok-build":
                return {"ok": True, "gated": True, "digest": "sha256:abc"}
            return None

    dts = {
        "implementer": DevType(name="implementer", harness_template="grok-build"),
        "judgment": DevType(name="judgment", harness_template="claude-code"),
    }
    summary = receipt_summary(dts, digest="sha256:abc", store=Store())
    assert summary["digest"] == "sha256:abc"
    assert summary["sentinel"] is False
    assert summary["templates"]["grok-build"]["ok"] is True
    assert summary["templates"]["claude-code"]["ok"] is False
    assert "no receipt" in summary["templates"]["claude-code"]["reason"]

    sent = receipt_summary(dts, digest=SENTINEL_DIGEST, store=Store())
    assert sent["sentinel"] is True
    assert "bake wrapper" in sent["templates"]["grok-build"]["reason"]


def test_merged_advisories_qualify_keys_when_n_gt_1(monkeypatch):
    """Dual-PMO colliding pmo_ids (gitea issue numbers) must not clobber
    each other in merge_handoffs / needs_human. Keys follow the same
    `{instance}:{id}` prefix dependency_cycles already uses."""
    a = SimpleNamespace(
        needs_human={"1": "ENG-1: needs human"},
        merge_handoffs={"1": "ENG-1: awaiting human merge"},
        anomalies={"1": "ENG-1: oops"},
        blocked_reasons={"1": "gated"},
        cycles=[["1", "2"]],
    )
    b = SimpleNamespace(
        needs_human={"1": "CS-1: needs human"},
        merge_handoffs={},
        anomalies={},
        blocked_reasons={},
        cycles=[],
    )
    fr = _forge_runtime()

    async def _true(*a, **k):
        return True
    async def _ingest():
        return {"ok": True, "detail": ""}
    monkeypatch.setattr(health_mod, "_check_redis", _true)
    monkeypatch.setattr(health_mod, "_check_http", _true)
    monkeypatch.setattr(health_mod, "_oo_ingest_check", _ingest)
    health_mod.reset_health_caches()
    got = run_coro(health_mod.build_health_payload(
        config=AppConfig(), dev_types={},
        managers={"eng": a, "cs": b}, stewards={},
        forge_runtime=fr, shared_breakers={},
        store=SimpleNamespace(active=lambda: []),
        internal_forge=None,
        poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={})))
    assert "eng:1" in got["needs_human"]
    assert "cs:1" in got["needs_human"]
    assert got["needs_human"]["eng:1"].startswith("[eng]")
    assert got["merge_handoffs"]["eng:1"].startswith("[eng]")
    assert got["dependency_cycles"] == [["eng:1", "eng:2"]]


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
    health_mod.reset_health_caches()
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
                         instance=lambda name: inst, internal=set())
    health_mod.reset_health_caches()
    out = run_coro(asyncio.wait_for(health_mod._branch_protection(fr), timeout=5))
    assert out == {f"r{i}": {"rendezvous": True} for i in range(3)}
    health_mod.reset_health_caches()   # don't leak the fake into others


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


# ── PMO probe cache + concurrency (2026-08-12 audit F3) ──────────────────────


def _pmo_world(probe, tmp_path, monkeypatch, name="linear", team="ENG"):
    """A config with one CONFIGURED instance (stored api_key — `configured`
    reads the secret store) whose manager probe is `probe`."""
    from devcake import secrets as secrets_store
    from devcake.config import PMOInstance
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    secrets_store.write_connection_secret("pmo", name, "api_key", "k-test")
    cfg = AppConfig(pmos=[PMOInstance(name=name, team_key=team)])
    mgr = SimpleNamespace(pmo=SimpleNamespace(health_probe=probe),
                          anomalies={}, blocked_reasons={}, needs_human={},
                          merge_handoffs={}, cycles=[])
    return cfg, {name: mgr}


def _pmo_payload(cfg, managers, monkeypatch, *, reset=True):
    async def _true(*a, **k):
        return True

    async def _ingest():
        return {"ok": True, "detail": ""}

    monkeypatch.setattr(health_mod, "_check_redis", _true)
    monkeypatch.setattr(health_mod, "_check_http", _true)
    monkeypatch.setattr(health_mod, "_oo_ingest_check", _ingest)
    if reset:
        health_mod.reset_health_caches()
    return run_coro(health_mod.build_health_payload(
        config=cfg, dev_types={}, managers=managers, stewards={},
        forge_runtime=_forge_runtime(), shared_breakers={},
        store=SimpleNamespace(active=lambda: []),
        internal_forge=None,
        poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={})))


def test_pmo_probe_is_cached_between_health_calls(tmp_path, monkeypatch):
    """The SPA polls /health every 10 s; the vendor needs one look a minute.
    (The gitea probe used to WRITE per call — F3; caching bounds even the
    read cost.)"""
    calls = []

    async def probe(team):
        calls.append(team)
        return SimpleNamespace(ok=True)

    cfg, managers = _pmo_world(probe, tmp_path, monkeypatch)
    assert _pmo_payload(cfg, managers, monkeypatch)["pmo"] is True
    assert _pmo_payload(cfg, managers, monkeypatch,
                        reset=False)["pmo"] is True
    assert len(calls) == 1, "second /health inside the TTL must not reprobe"
    health_mod.reset_health_caches()
    _pmo_payload(cfg, managers, monkeypatch, reset=False)
    assert len(calls) == 2, "reset_health_caches must force a reprobe"


def test_pmo_health_exposes_probe_detail_and_capability_flags(tmp_path, monkeypatch):
    from fakes import fake_pmo_capabilities

    async def probe(team):
        return SimpleNamespace(ok=True, detail="relations=off (token)")

    cfg, managers = _pmo_world(probe, tmp_path, monkeypatch)
    managers["linear"].pmo.capabilities = lambda: fake_pmo_capabilities(
        relations_supported=False)
    payload = _pmo_payload(cfg, managers, monkeypatch)
    inst = payload["pmo_instances"]["linear"]
    assert inst["ok"] is True
    assert inst["detail"] == "relations=off (token)"
    assert inst["relations_supported"] is False
    assert inst["attachments_supported"] is True


def test_one_hanging_pmo_cannot_stall_health(tmp_path, monkeypatch):
    """Per-probe 5 s timeout + concurrent gather: a sick PMO reports
    ok:False; /health returns without waiting out a 30 s client timeout."""
    from devcake.config import PMOInstance

    async def hang(team):
        await asyncio.sleep(3600)

    async def fine(team):
        return SimpleNamespace(ok=True)

    from devcake import secrets as secrets_store
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    secrets_store.write_connection_secret("pmo", "sick", "api_key", "k1")
    secrets_store.write_connection_secret("pmo", "fine", "api_key", "k2")
    cfg = AppConfig(pmos=[PMOInstance(name="sick", team_key="A"),
                          PMOInstance(name="fine", team_key="B")])
    def _mgr(probe):
        return SimpleNamespace(pmo=SimpleNamespace(health_probe=probe),
                               anomalies={}, blocked_reasons={},
                               needs_human={}, merge_handoffs={},
                               cycles=[])

    managers = {"sick": _mgr(hang), "fine": _mgr(fine)}
    monkeypatch.setattr(health_mod, "_PMO_PROBE_TIMEOUT", 0.05)
    payload = _pmo_payload(cfg, managers, monkeypatch)
    assert payload["pmo_instances"]["sick"]["ok"] is False, (
        "the timeout must convert a hang into ok:False")
    assert payload["pmo_instances"]["fine"]["ok"] is True


def test_pmo_probe_transport_failure_carries_detail(tmp_path, monkeypatch):
    """A transport/timeout exception must leave a non-empty detail (like
    _oo_ingest_check) — ok:False alone leaves the operator with no cause."""

    async def boom(team):
        raise ConnectionError("pmo upstream refused connection")

    cfg, managers = _pmo_world(boom, tmp_path, monkeypatch)
    payload = _pmo_payload(cfg, managers, monkeypatch)
    inst = payload["pmo_instances"]["linear"]
    assert inst["ok"] is False
    assert inst.get("detail"), "transport failure must populate detail"
    assert "refused" in inst["detail"].lower() or "connection" in inst["detail"].lower()


def test_redis_connect_env_is_shared_chokepoint(monkeypatch):
    """/health and build_services must resolve REDIS_* via the same helper
    (ADR-0034) — no import-time frozen copy with a divergent default."""
    import ast
    import inspect
    from pathlib import Path

    from devcake.adapters.redis import redis_connect_env
    from devcake.adapters.redis.messaging import DEFAULT_REDIS_URL

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    url, password = redis_connect_env()
    assert url == DEFAULT_REDIS_URL
    assert password == ""
    assert DEFAULT_REDIS_URL == "redis://redis:6379/0"

    monkeypatch.setenv("REDIS_URL", "redis://custom:6379/1")
    monkeypatch.setenv("REDIS_PASSWORD", "secret")
    assert redis_connect_env() == ("redis://custom:6379/1", "secret")

    health_src = Path(inspect.getfile(health_mod)).read_text()
    # no module-level REDIS_URL = os.environ.get(...) freeze
    tree = ast.parse(health_src)
    frozen = [
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in ("REDIS_URL", "REDIS_PASSWORD")
                for t in n.targets)
    ]
    assert not frozen, "health.py must not freeze REDIS_* at import time"

    services_path = Path(inspect.getfile(health_mod)).parent / "services.py"
    services_src = services_path.read_text()
    assert "redis_connect_env" in health_src
    assert "redis_connect_env" in services_src


def test_branch_protection_skips_internal_repos():
    """Internal mission repos (ForgeRuntime.internal) are not operator work
    cards: nobody watches the internal Gitea and apply-protection walks
    config.repos only, so the unprotected-default-branch advisory would be
    unactionable noise. Their rows are the real model_construct'd shape
    (hyphenated name, `internal`), so a regression that consults the row
    before the skip surfaces here too."""
    from devcake.config import RepoInstance

    class _Prot:
        def model_dump(self):
            return {"protected": False}

    class _Forge:
        async def default_branch_protection(self, branch):
            return _Prot()

    internal_name = "devcakeinternal-cs-22"
    internal = RepoInstance.model_construct(
        name=internal_name, forge="gitea", url="http://gitea/o/r.git",
        default_branch="main", api_base=None, auto_merge=True,
        auto_resolve_merge_conflicts=True, merge_retry_window_minutes=30,
        _internal=True)
    work = SimpleNamespace(reference_only=False, default_branch="main")
    insts = {internal_name: internal, "work": work}
    fr = SimpleNamespace(forges={"work": _Forge(), internal_name: _Forge()},
                         instance=insts.get, internal={internal_name})
    health_mod.reset_health_caches()
    try:
        out = run_coro(asyncio.wait_for(health_mod._branch_protection(fr), timeout=5))
    finally:
        health_mod.reset_health_caches()
    assert out == {"work": {"protected": False}}


def test_branch_protection_maps_row_lookup_errors_to_none():
    """Probe contract (docs/15 §7): whatever fails for one repo — here the
    row's own secret read-through raising — that repo maps to None and
    /health never 500s. Before this, the reference-only check sat outside
    the probe's try and one bad row 500'd the whole payload."""
    class _Forge:
        async def default_branch_protection(self, branch):
            raise AssertionError("must not be reached")

    class _Inst:
        default_branch = "main"

        @property
        def reference_only(self):
            raise ValueError("invalid connection instance 'bad-name'")

    fr = SimpleNamespace(forges={"bad": _Forge()}, instance=lambda n: _Inst(),
                         internal=set())
    health_mod.reset_health_caches()
    try:
        out = run_coro(asyncio.wait_for(health_mod._branch_protection(fr), timeout=5))
    finally:
        health_mod.reset_health_caches()
    assert out == {"bad": None}


# ── ADR-0040: request budgets on /health ─────────────────────────────────────

def test_health_payload_carries_pmo_budget_rows(monkeypatch):
    from devcake.adapters import budget as pmo_budget
    b = pmo_budget.budget_for("tracker.example", "k", system="linear",
                              instance="a")
    b.observe(pmo_budget.RateSignal(limit=2500, remaining=2000,
                                    reset_at=1_700_000_000.0, window_s=3600))
    fr = _forge_runtime(last_full_probe_at=datetime.now(timezone.utc))
    got = _payload(fr, monkeypatch)
    row = got["pmo_budget"][b.label]
    assert row["limit"] == 2500 and row["remaining"] == 2000
    assert row["instances"] == ["a"] and row["systems"] == ["linear"]
    assert got["pmo_budget_warnings"] == {}


def test_budget_warning_names_the_poll_interval_that_fits():
    snap = {"tracker.example/user:u1": {
        "limit": 2500, "instances": ["a", "b"], "foreign_spend": 0,
        "demand_per_hour": {"a": 1500, "b": 1400}}}
    out = health_mod._budget_warnings(snap, 15)
    text = out["tracker.example/user:u1"]
    assert "about 2900 requests/hour against 2500/hour" in text
    # 15 s × 2900 / (2500 × 0.8) = 21.75 → 22 s → rounded up to a 15 s step
    assert "at least 30 s" in text
    assert "a, b" in text
    # the text is stable across small demand changes (dismissal keys hash it)
    snap["tracker.example/user:u1"]["demand_per_hour"] = {"a": 1520, "b": 1410}
    assert health_mod._budget_warnings(snap, 15) == out


def test_budget_warning_notes_a_foreign_consumer_and_stays_quiet_otherwise():
    quiet = {"t/u": {"limit": 2500, "instances": ["a"], "foreign_spend": 0,
                     "demand_per_hour": {"a": 900}}}
    assert health_mod._budget_warnings(quiet, 30) == {}
    shared = {"t/u": {"limit": 2500, "instances": ["a"], "foreign_spend": 600,
                      "demand_per_hour": {"a": 900}}}
    assert "another consumer" in health_mod._budget_warnings(shared, 30)["t/u"]


def test_paced_probe_keeps_the_last_known_state_instead_of_going_red(monkeypatch):
    """A probe refused by the request budget is not an outage: the row keeps
    its last known `ok` (or unknown) with a 'deferred' detail."""
    from devcake.ports.pmo import PMOBudgetExceeded
    from devcake.config import PMOInstance

    class Probe:
        def __init__(self):
            self.calls = 0

        async def health_probe(self, team):
            self.calls += 1
            if self.calls == 1:
                from devcake.ports.pmo import PMOHealth
                return PMOHealth(ok=True, workspace=team)
            raise PMOBudgetExceeded("request budget (t/u): reserved")

        def capabilities(self):
            return None

    probe = Probe()
    cfg = AppConfig(pmos=[PMOInstance(name="a", system="linear", team_key="T")])
    managers = {"a": SimpleNamespace(pmo=probe, anomalies={}, merge_handoffs={},
                                     needs_human={}, cycles=[], blocked_reasons={})}

    async def _true(*a, **k):
        return True

    async def _ingest():
        return {"ok": True, "detail": ""}
    monkeypatch.setattr(health_mod, "_check_redis", _true)
    monkeypatch.setattr(health_mod, "_check_http", _true)
    monkeypatch.setattr(health_mod, "_oo_ingest_check", _ingest)
    health_mod.reset_health_caches()
    fr = _forge_runtime()

    def build():
        return run_coro(health_mod.build_health_payload(
            config=cfg, dev_types={}, managers=managers, stewards={},
            forge_runtime=fr, shared_breakers={},
            store=SimpleNamespace(active=lambda: []), internal_forge=None,
            poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={})))

    first = build()["pmo_instances"]["a"]
    assert first["ok"] is True
    health_mod._pmo_probe_cache[("a", "T")]["ts"] -= 10_000   # expire the row
    second = build()["pmo_instances"]["a"]
    assert second["ok"] is True                     # last known state kept
    assert second["detail"].startswith("probe deferred")
