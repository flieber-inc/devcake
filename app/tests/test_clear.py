"""Unit tests for the operator clear-runs wipe (local state side)."""

from datetime import datetime, timezone
from pathlib import Path

from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run
from devcake.api.clear import clear_local_state


def test_runstore_clear(tmp_path: Path):
    store = RunStore(root=tmp_path / "runs")
    for i in range(3):
        store.save(Run(
            run_id=f"T-{i}-1-ONBOARD-AAAAAA",
            mission_key=f"T-{i}",
            mission_type="ONBOARD",
            dev_type="senior-dev",
            seq=1,
            created_at=datetime.now(timezone.utc),
        ))
    assert len(store.all()) == 3
    assert store.wipe_generation == 0
    n = store.clear()
    assert n == 3
    assert store.wipe_generation == 1
    assert store.all() == []
    assert store.clear() == 0
    assert store.wipe_generation == 2


def test_runstore_save_drops_pre_wipe_runs(tmp_path: Path):
    """Post-#32 residual A: in-flight finalize holds a Run and must not
    resurrect the file after clear bumps wipe_generation."""
    store = RunStore(root=tmp_path / "runs")
    run = Run(
        run_id="T-0-1-ONBOARD-AAAAAA",
        mission_key="T-0",
        mission_type="ONBOARD",
        dev_type="senior-dev",
        seq=1,
        store_gen=0,
    )
    store.save(run)
    assert store.get(run.run_id) is not None
    store.clear()
    assert store.get(run.run_id) is None
    run.state = "finished"
    store.save(run)                                   # pre-wipe stamp
    assert store.get(run.run_id) is None
    # post-wipe stamp is allowed
    run.store_gen = store.wipe_generation
    store.save(run)
    assert store.get(run.run_id) is not None


def test_clear_local_state_preserves_nothing_but_wipes_audit(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    runs = data / "state" / "runs"
    runs.mkdir(parents=True)
    audit = data / "state" / "events.jsonl"
    audit.write_text('{"ts":"x","pmo_id":"p","action":"devcake_failed"}\n')
    (data / "config").mkdir()
    (data / "config" / "config.yaml").write_text("schema_version: 2\n")
    (data / "secrets").mkdir()
    (data / "secrets" / "keep.me").write_text("secret")

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(data))
    # re-import paths that were bound at module load — patch the module attributes
    import devcake.api.clear as clear_mod
    monkeypatch.setattr(clear_mod, "DATA_DIR", data)
    monkeypatch.setattr(clear_mod, "STATE_DIR", data / "state")
    monkeypatch.setattr(clear_mod, "AUDIT_PATH", audit)

    store = RunStore(root=runs)
    store.save(Run(
        run_id="T-1-1-ONBOARD-AAAAAA", mission_key="T-1",
        mission_type="ONBOARD", dev_type="senior-dev", seq=1,
    ))
    result = clear_local_state(store)
    assert result["runs_deleted"] == 1
    assert result["audit_cleared"] == 1
    assert store.all() == []
    assert audit.read_text() == ""
    assert (data / "config" / "config.yaml").exists()
    assert (data / "secrets" / "keep.me").read_text() == "secret"


def test_quarantine_unreadable_moves_corrupt_and_prev2_records(tmp_path: Path):
    """One bad record must never wedge startup (crash-loop regression); a
    pre-v2 record may carry credentials at rest and is quarantined wholesale
    (the v1 scrub was removed at v0 — docs/10 §5). Healthy records stay."""
    import json

    store = RunStore(root=tmp_path / "runs")
    corrupt = store.root / "T-9-1-ONBOARD-CORRUPT.json"
    corrupt.write_text('{"schema_version": 2, "run_id": "T-9')  # truncated write
    bad_model = store.root / "T-2-1-ONBOARD-BADMODEL.json"
    bad_model.write_text(json.dumps({
        "schema_version": 2, "run_id": "T-2-1-ONBOARD-BADMODEL",
        "mission_key": "T-2", "mission_type": "ONBOARD",
        "dev_type": "senior-dev", "seq": "not-an-int-at-all",
        "state": "bogus-state",
    }))
    legacy = store.root / "T-1-1-ONBOARD-LEGACY.json"
    legacy.write_text(json.dumps({
        "schema_version": 1, "run_id": "T-1-1-ONBOARD-LEGACY",
        "mission_key": "T-1", "mission_type": "ONBOARD",
        "dev_type": "senior-dev", "seq": 1, "state": "finished",
        "redis_password": "relay-secret", "spec_env": {"PUBLIC": "yes"},
    }))
    store.save(Run(
        run_id="T-4-1-ONBOARD-OK", mission_key="T-4",
        mission_type="ONBOARD", dev_type="senior-dev", seq=1,
    ))

    quarantined = store.quarantine_unreadable()

    assert sorted(quarantined) == ["T-1-1-ONBOARD-LEGACY", "T-2-1-ONBOARD-BADMODEL",
                                   "T-9-1-ONBOARD-CORRUPT"]
    for name in (corrupt.name, bad_model.name, legacy.name):
        assert not (store.root / name).exists()
        assert (store.root / "quarantine" / name).exists()
    # a parseable quarantined record is scrubbed — no secret-at-rest, even here
    raw = json.loads((store.root / "quarantine" / legacy.name).read_text())
    assert "redis_password" not in raw
    # the truncated file couldn't be scrubbed; its bytes are preserved as-is
    assert (store.root / "quarantine" / corrupt.name).read_text().startswith('{"schema_version"')
    assert [r.run_id for r in store.all()] == ["T-4-1-ONBOARD-OK"]
    assert store.quarantine_unreadable() == []  # idempotent on a clean store


def test_clear_removes_quarantined_files(tmp_path: Path):
    store = RunStore(root=tmp_path / "runs")
    (store.root / "T-9-1-ONBOARD-CORRUPT.json").write_text("{broken")
    store.quarantine_unreadable()
    assert (store.root / "quarantine" / "T-9-1-ONBOARD-CORRUPT.json").exists()
    assert store.clear() == 1
    assert not (store.root / "quarantine" / "T-9-1-ONBOARD-CORRUPT.json").exists()


# ── activity-repo sweep (ADR-0014 D4) ────────────────────────────────────────

import asyncio

from devcake.ports.internal_forge import InternalRepo


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _forge_with(names, fail=()):
    from fakes import FakeInternalForge
    forge = FakeInternalForge()

    async def _list():
        return [InternalRepo(name=n, mission_key=n.rsplit("-", 1)[-1],
                             html_url="", clone_url="") for n in names]
    forge.list_activity_repos = _list
    orig = forge.delete_activity_repo

    async def _delete(name):
        if name in fail:
            raise RuntimeError("gitea 500")
        await orig(name)
    forge.delete_activity_repo = _delete
    return forge


def test_clear_activity_repos_deletes_by_prefix_and_reports():
    from devcake.api.clear import clear_activity_repos
    forge = _forge_with(["activity-linear-t-1", "activity-linear-t-2"],
                        fail={"activity-linear-t-1"})
    out = run_coro(clear_activity_repos(forge))
    assert out["deleted"] == 1 and len(out["errors"]) == 1
    assert forge.deleted == ["activity-linear-t-2"]   # failure ≠ abort
    assert run_coro(clear_activity_repos(None)) == {
        "deleted": 0, "errors": [], "skipped": "internal forge disabled"}


def test_clear_all_threads_internal_forge(tmp_path: Path, monkeypatch):
    import devcake.api.clear as clear_mod

    async def _dagu(executor):
        return {"deleted": 0, "listed": 0, "failed": []}

    async def _oo():
        return {"deleted": [], "errors": []}

    async def _redis(messaging):
        return {"trimmed": 0}
    monkeypatch.setattr(clear_mod, "clear_dagu", _dagu)
    monkeypatch.setattr(clear_mod, "clear_openobserve", _oo)
    monkeypatch.setattr(clear_mod, "clear_redis", _redis)
    monkeypatch.setattr(clear_mod, "AUDIT_PATH", tmp_path / "events.jsonl")
    store = RunStore(root=tmp_path / "runs")

    forge = _forge_with(["activity-a-b"])
    out = run_coro(clear_mod.clear_all(store, None, None,
                                       internal_forge=forge))
    assert out["activity_repos"]["deleted"] == 1
    assert forge.deleted == ["activity-a-b"]
    assert out["ok"] is True
    assert "work repos (devcake-internal)" in out["preserved"]
    assert "repo mirrors" in out["preserved"]      # ADR-0024: cache is config-keyed, not run state

    out2 = run_coro(clear_mod.clear_all(store, None, None,
                                        internal_forge=None))
    assert out2["activity_repos"]["skipped"]
    assert out2["ok"] is True                        # absence never fails ok

    failing = _forge_with(["activity-a-b"], fail={"activity-a-b"})
    out3 = run_coro(clear_mod.clear_all(store, None, None,
                                        internal_forge=failing))
    assert out3["ok"] is False                       # sweep errors surface


# ── stop-and-drain (Clear-Runs hardening, founder-scoped 2026-07-18) ─────────
# Root cause: clear_redis's `ACL DELUSER dev-*` ran while Dev containers were
# still inside Dagu's 30s SIGTERM grace → AuthenticationError → exit 1.

import asyncio
from types import SimpleNamespace


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _DrainExec:
    """Reports each run as running for N status polls, then terminal.

    Implements ``stop`` (no-op) so the force-remove pass does not AttributeError
    on the Fake — a wedged container is modelled by ``running_polls=None``
    (always running) or a huge poll budget.
    """

    def __init__(self, running_polls: int | None = 1):
        self.polls: dict[str, int] = {}
        self.running_polls = running_polls  # None = forever running
        self.stops: list[str] = []

    async def stop(self, rid):
        self.stops.append(rid)
        return True

    async def status(self, rid):
        n = self.polls.get(rid, 0)
        self.polls[rid] = n + 1
        if self.running_polls is None or n < self.running_polls:
            return {"dagRunDetails": {"statusLabel": "running"}}
        return {"dagRunDetails": {"statusLabel": "failed"}}


class _RM:
    def __init__(self):
        self.kills = []

    async def kill(self, r, state, reason, *, error_class=None):
        # error_class is RECORDED, not discarded (ADR-0018): the operator sites
        # are the only callers that override `_kill_inner`'s state-keyed default,
        # so a fake swallowing it makes that wiring untestable
        self.kills.append((r.run_id, state, reason, error_class))


def _store(runs, live=None):
    """active() returns the initial snapshot; get() returns the CURRENT object
    (from `live` if given) so the drain's re-fetch guard is exercised."""
    live = live if live is not None else {r.run_id: r for r in runs}
    return SimpleNamespace(active=lambda: runs, get=lambda rid: live.get(rid))


def test_stop_and_drain_kills_waits_and_skips_finalizing(monkeypatch):
    from devcake.api.clear import stop_and_drain
    monkeypatch.setattr("asyncio.sleep", lambda *_: _instant())
    live = SimpleNamespace(run_id="R-1", state="running")
    fin = SimpleNamespace(run_id="R-2", state="finalizing")
    rm, ex = _RM(), _DrainExec(running_polls=2)
    out = run_coro(stop_and_drain(_store([live, fin]), ex, rm, timeout_s=30))
    assert [k[0] for k in rm.kills] == ["R-1"]          # finalizing untouched
    assert "clear" in rm.kills[0][2]
    assert rm.kills[0][3] == "DEV_OPERATOR_STOP"        # ADR-0018 override
    assert out["stopped"] == 1
    assert out["undrained"] == []
    assert out["force_remove_attempted"] is False
    assert ex.polls["R-1"] >= 3                         # waited past running


def test_stop_and_drain_times_out_on_wedged_container(monkeypatch):
    from devcake.api.clear import stop_and_drain
    monkeypatch.setattr("asyncio.sleep", lambda *_: _instant())
    live = SimpleNamespace(run_id="R-9", state="running")
    ex = _DrainExec(running_polls=None)                 # forever wedged
    out = run_coro(stop_and_drain(_store([live]), ex, _RM(), timeout_s=0.01))
    assert out["stopped"] == 1
    assert out["undrained"] == ["R-9"]                  # reported, wipe proceeds
    assert out["force_remove_attempted"] is True        # force pass ran
    assert out["force_stops"] >= 1
    assert "R-9" in ex.stops


def test_stop_and_drain_force_remove_clears_undrained(monkeypatch):
    """Force pass: soft drain sees still-running; force stop + re-poll empties live."""
    from devcake.api.clear import stop_and_drain
    monkeypatch.setattr("asyncio.sleep", lambda *_: _instant())

    class ForceExec(_DrainExec):
        def __init__(self):
            super().__init__(running_polls=None)
            self._force_cleared = False

        async def stop(self, rid):
            await super().stop(rid)
            self._force_cleared = True
            return True

        async def status(self, rid):
            if self._force_cleared:
                return {"dagRunDetails": {"statusLabel": "failed"}}
            return await super().status(rid)

    live = SimpleNamespace(run_id="R-force", state="running")
    ex = ForceExec()
    out = run_coro(stop_and_drain(_store([live]), ex, _RM(), timeout_s=0.01))
    assert out["force_remove_attempted"] is True
    assert "R-force" in ex.stops
    assert out["undrained"] == []


def test_stop_and_drain_refetches_state_before_kill(monkeypatch):
    """Audit D5 #7: a run in the initial snapshot that flipped to finalizing
    (via the concurrent ingress consumer) must NOT be killed — the re-fetch
    guard reads current state, not the stale snapshot object."""
    from devcake.api.clear import stop_and_drain
    monkeypatch.setattr("asyncio.sleep", lambda *_: _instant())
    snap = SimpleNamespace(run_id="R-1", state="running")   # stale: says running
    current = SimpleNamespace(run_id="R-1", state="finalizing")  # actually finalizing
    rm = _RM()
    out = run_coro(stop_and_drain(_store([snap], live={"R-1": current}),
                                  _DrainExec(running_polls=0), rm, timeout_s=1))
    assert rm.kills == []                               # never killed
    assert out["stopped"] == 0


async def _instant():
    return None


def test_clear_all_drains_before_every_wipe(monkeypatch):
    """Order is load-bearing: the ACL sweep must never race a live container.
    All subsystem clears are recorded; the drain must come first."""
    import devcake.api.clear as clear_mod

    calls: list[str] = []

    async def fake_drain(store, executor, run_manager, timeout_s=40.0):
        calls.append("drain")
        return {"stopped": 1, "undrained": []}

    def fake_local(store, runlog=None):
        calls.append("local")
        return {"runs_deleted": 0, "runlogs_deleted": 0, "audit_cleared": 0}

    async def fake_dagu(executor):
        calls.append("dagu")
        return {"stop_errors": [], "listed": 0, "deleted": 0, "failed": []}

    async def fake_oo():
        calls.append("oo")
        return {"deleted": [], "errors": []}

    async def fake_redis(messaging):
        calls.append("redis")
        return {"keys_deleted": 0, "ingress_trimmed": True,
                "acl_users_deleted": 0}

    async def fake_activity(internal_forge):
        calls.append("activity")
        return {"deleted": 0, "errors": []}

    monkeypatch.setattr(clear_mod, "stop_and_drain", fake_drain)
    monkeypatch.setattr(clear_mod, "clear_local_state", fake_local)
    monkeypatch.setattr(clear_mod, "clear_dagu", fake_dagu)
    monkeypatch.setattr(clear_mod, "clear_openobserve", fake_oo)
    monkeypatch.setattr(clear_mod, "clear_redis", fake_redis)
    monkeypatch.setattr(clear_mod, "clear_activity_repos", fake_activity)

    out = run_coro(clear_mod.clear_all(None, None, None,
                                       run_manager=object()))
    assert calls[0] == "drain"
    assert calls.index("drain") < calls.index("redis")
    assert out["ok"] is True and out["stopped"]["stopped"] == 1


def test_clear_all_without_run_manager_still_wipes(monkeypatch):
    """Back-compat: callers without a run_manager (old signature) skip the
    drain phase explicitly — reported, never crashing."""
    import devcake.api.clear as clear_mod

    async def fake_dagu(executor):
        return {"stop_errors": [], "listed": 0, "deleted": 0, "failed": []}

    async def fake_oo():
        return {"deleted": [], "errors": []}

    async def fake_redis(messaging):
        return {"keys_deleted": 0, "ingress_trimmed": True,
                "acl_users_deleted": 0}

    async def fake_activity(internal_forge):
        return {"deleted": 0, "errors": []}

    monkeypatch.setattr(clear_mod, "clear_local_state",
                        lambda store, runlog=None: {"runs_deleted": 0,
                                                    "runlogs_deleted": 0,
                                                    "audit_cleared": 0})
    monkeypatch.setattr(clear_mod, "clear_dagu", fake_dagu)
    monkeypatch.setattr(clear_mod, "clear_openobserve", fake_oo)
    monkeypatch.setattr(clear_mod, "clear_redis", fake_redis)
    monkeypatch.setattr(clear_mod, "clear_activity_repos", fake_activity)

    out = run_coro(clear_mod.clear_all(None, None, None))
    assert out["stopped"]["skipped"] == "no run_manager"
