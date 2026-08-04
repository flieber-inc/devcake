"""Per-dev-type OAuth: the credential lands in the NAMED Dev Type's secrets
dir, using flow + image from the harness registry (session state snapshotted
at start — a dev type deleted or re-harnessed mid-login must not misroute)."""

import asyncio

import pytest

from devcake.config import DevType
from devcake.harness import HARNESSES
from devcake.domain.oauth import OAuthManager
from devcake.adapters.files.run_store import RunStore


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


class FakeExecutor:
    def __init__(self):
        self.params = None

    async def start(self, params, dag_run_id):
        self.params = params


class NullMessaging:
    async def create_run_user(self, rid): return "pw"
    async def delete_run_user(self, rid): pass
    async def delete_reply_stream(self, rid): pass


def make_mgr(tmp_path, monkeypatch):
    from devcake.domain.runs import RunManager

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    store = RunStore(tmp_path / "runs")
    executor = FakeExecutor()
    messaging = NullMessaging()
    runs = RunManager(store, messaging, executor)
    breakers = {"main-dev": "DEV_AUTH"}
    dev_types = {
        "main-dev": DevType(name="main-dev", harness_template="grok-build"),
        "second-grok": DevType(name="second-grok", harness_template="grok-build"),
        "senior-dev": DevType(name="senior-dev", harness_template="claude-code"),
    }
    return OAuthManager(runs, messaging, dev_types, breakers=breakers)


def test_start_rejects_unknown_and_flowless(tmp_path, monkeypatch):
    mgr = make_mgr(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unknown dev type"):
        run_coro(mgr._start_inner("ghost"))
    with pytest.raises(ValueError, match="no OAuth flow"):
        run_coro(mgr._start_inner("senior-dev"))   # claude-code: env token only


def test_start_uses_registry_and_snapshots(tmp_path, monkeypatch):
    mgr = make_mgr(tmp_path, monkeypatch)
    result = run_coro(mgr._start_inner("main-dev"))
    run_id = result["run_id"]
    assert mgr.runs.executor.params["IMAGE"] == HARNESSES["grok-build"].image
    s = mgr.sessions[run_id]
    assert s["dev_type"] == "main-dev"
    assert s["secret_file"] == "grok-auth.json"
    run = mgr.runs.store.get(run_id)
    assert run.spec_env["DEVCAKE_OAUTH_LOGIN_CMD"] == "grok login --device-auth"
    assert run.auth_digest is not None


def test_result_lands_in_named_dev_type_dir(tmp_path, monkeypatch):
    """Two dev types on one harness: connecting the SECOND must not write into
    the first's dir (the old first-match bug), and must survive the dev type
    being deleted mid-login (snapshot, no re-lookup)."""
    mgr = make_mgr(tmp_path, monkeypatch)
    run_id = run_coro(mgr._start_inner("second-grok"))["run_id"]
    del mgr.dev_types["second-grok"]           # deleted mid-login
    run_coro(mgr._on_result_inner(run_id, {"content": '{"auth": 1}'}))

    p = tmp_path / "secrets" / "second-grok" / "grok-auth.json"
    assert p.read_text() == '{"auth": 1}'
    assert (p.stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / "secrets" / "main-dev").exists()
    assert mgr.sessions[run_id]["state"] == "completed"
    assert mgr.runs.store.get(run_id).state == "finished"


def test_result_clears_breaker_for_that_dev_type(tmp_path, monkeypatch):
    mgr = make_mgr(tmp_path, monkeypatch)
    run_id = run_coro(mgr._start_inner("main-dev"))["run_id"]
    run_coro(mgr._on_result_inner(run_id, {"content": "{}"}))
    assert "main-dev" not in mgr.breakers


def test_result_cleans_run_workspace(tmp_path, monkeypatch):
    """ADR-0025 Hook D: OAuth completion bypasses finalize AND kill — the
    (empty) pre-created workspace dir is reclaimed on the success path."""
    mgr = make_mgr(tmp_path, monkeypatch)
    cleaned = []
    mgr.runs.workspaces.cleanup = lambda rid: (cleaned.append(rid), True)[1]
    run_id = run_coro(mgr._start_inner("main-dev"))["run_id"]
    run_coro(mgr._on_result_inner(run_id, {"content": "{}"}))
    assert cleaned == [run_id]


def test_result_stamps_ended_at(tmp_path, monkeypatch):
    """2026-08 reviewer catch: the OAuth success path was the ONE terminal
    transition in the codebase that omitted ended_at, so every successful
    login's runtime grew monotonically forever in the Runs tab
    ((ended_at or now) - started_at, repriced on every 10 s poll) and
    permanently topped the duration sort."""
    mgr = make_mgr(tmp_path, monkeypatch)
    run_id = run_coro(mgr._start_inner("main-dev"))["run_id"]
    run_coro(mgr._on_result_inner(run_id, {"content": "{}"}))
    saved = mgr.runs.store.get(run_id)
    assert saved.state == "finished"
    assert saved.ended_at is not None


def test_late_result_does_not_resurrect_a_terminal_run(tmp_path, monkeypatch):
    """A credential landing AFTER the watchdog's 660 s kill (slow human on
    the device-code page) must still be stored — the login DID succeed — but
    the run record's history is not rewritten: pre-guard, the late
    oauth.result flipped a timed_out run back to "finished" while racing the
    kill's own save."""
    mgr = make_mgr(tmp_path, monkeypatch)
    run_id = run_coro(mgr._start_inner("main-dev"))["run_id"]
    killed = mgr.runs.store.get(run_id)
    killed.state = "timed_out"
    from devcake.domain.run import utcnow
    killed.ended_at = utcnow()
    mgr.runs.store.save(killed)

    run_coro(mgr._on_result_inner(run_id, {"content": '{"late": 1}'}))
    p = tmp_path / "secrets" / "main-dev" / "grok-auth.json"
    assert p.read_text() == '{"late": 1}'          # credential still lands
    assert mgr.sessions[run_id]["state"] == "completed"
    saved = mgr.runs.store.get(run_id)
    assert saved.state == "timed_out"              # history not rewritten
    assert saved.ended_at == killed.ended_at
