"""Host-side Grok OAuth refresh (domain/grok_oauth) — pure + fake port."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from devcake.domain import grok_oauth as go
from devcake.ports.oidc_token import (OidcRefreshRevoked, OidcTokenError,
                                      TokenRefreshResult)


def _jwt(exp: int, iat: int | None = None) -> str:
    """Minimal unsigned JWT for tests (header.payload.)."""
    import base64

    def b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"exp": exp, "iat": iat or (exp - 21600), "aud": "cid"})
    return f"{header}.{payload}.sig"


def _cli_file(*, exp_unix: float, refresh: str = "rt-old",
              access: str | None = None) -> str:
    tok = access or _jwt(int(exp_unix))
    from datetime import datetime, timezone
    expires_at = datetime.fromtimestamp(
        exp_unix, timezone.utc).isoformat().replace("+00:00", "Z")
    doc = {
        "https://auth.x.ai::cid": {
            "key": tok,
            "refresh_token": refresh,
            "expires_at": expires_at,
            "oidc_client_id": "cid",
            "email": "op@example.com",
            "auth_mode": "oidc",
            "team_id": "team-1",
        }
    }
    return json.dumps(doc)


class FakePort:
    def __init__(self, result: TokenRefreshResult | None = None,
                 error: Exception | None = None):
        self.calls: list[dict] = []
        self._result = result
        self._error = error

    def refresh(self, *, refresh_token, client_id=None, token_url="",
                timeout=15.0):
        self.calls.append({
            "refresh_token": refresh_token, "client_id": client_id,
            "timeout": timeout,
        })
        if self._error:
            raise self._error
        assert self._result is not None
        return self._result


def test_parse_and_serialize_round_trip_preserves_fields():
    raw = _cli_file(exp_unix=time.time() + 3600)
    session = go.parse_grok_auth(raw)
    assert session.entry["email"] == "op@example.com"
    assert session.entry["oidc_client_id"] == "cid"
    out = go.serialize_grok_auth(session)
    again = go.parse_grok_auth(out)
    assert again.entry["team_id"] == "team-1"
    assert again.entry["refresh_token"] == "rt-old"


def test_needs_refresh_near_expiry():
    now = 1_000_000.0
    raw = _cli_file(exp_unix=now + 60)  # within 120s slack
    session = go.parse_grok_auth(raw)
    assert go.needs_refresh(session, now=now, slack_s=120) is True
    raw2 = _cli_file(exp_unix=now + 600)
    assert go.needs_refresh(go.parse_grok_auth(raw2), now=now) is False


def test_apply_refresh_maps_access_token_to_key():
    now = 1_000_000.0
    session = go.parse_grok_auth(_cli_file(exp_unix=now - 10))
    result = TokenRefreshResult(
        access_token=_jwt(int(now + 7200), iat=int(now)),
        refresh_token="rt-new",
        expires_in=7200.0,
    )
    updated = go.apply_refresh(session, result, now=now)
    assert updated.entry["key"] == result.access_token
    assert updated.entry["refresh_token"] == "rt-new"
    assert updated.entry["email"] == "op@example.com"  # preserved
    # expires_at advanced
    assert go.access_expiry_unix(updated.entry) == pytest.approx(now + 7200)


def test_ensure_fresh_skips_port_when_not_expired(tmp_path):
    now = time.time()
    raw = _cli_file(exp_unix=now + 3600)
    port = FakePort(result=TokenRefreshResult(access_token="nope"))
    writes: list[str] = []
    out = go.ensure_fresh_for_inject(
        raw, token_port=port, write_full=writes.append,
        lock_path=tmp_path / ".lock", now=now)
    assert port.calls == []
    assert writes == []
    assert "rt-old" in out


def test_ensure_fresh_refreshes_writes_and_returns_full_file(tmp_path):
    now = time.time()
    raw = _cli_file(exp_unix=now - 30)
    new_access = _jwt(int(now + 8000), iat=int(now))
    port = FakePort(result=TokenRefreshResult(
        access_token=new_access, refresh_token="rt-rotated", expires_in=8000))
    writes: list[str] = []
    out = go.ensure_fresh_for_inject(
        raw, token_port=port, write_full=writes.append,
        lock_path=tmp_path / ".lock", now=now)
    assert len(port.calls) == 1
    assert port.calls[0]["refresh_token"] == "rt-old"
    assert port.calls[0]["client_id"] == "cid"
    assert len(writes) == 1
    assert "rt-rotated" in writes[0]
    assert new_access in out
    assert "rt-rotated" in out  # full file injected (includes RT)


def test_ensure_fresh_revoked_raises_credential_refresh_error(tmp_path):
    now = time.time()
    raw = _cli_file(exp_unix=now - 1)
    port = FakePort(error=OidcRefreshRevoked("400 invalid_grant"))
    with pytest.raises(go.CredentialRefreshError, match="revoked"):
        go.ensure_fresh_for_inject(
            raw, token_port=port, write_full=lambda _t: None,
            lock_path=tmp_path / ".lock", now=now)


def test_ensure_fresh_transient_raises_credential_refresh_error(tmp_path):
    now = time.time()
    raw = _cli_file(exp_unix=now - 1)
    port = FakePort(error=OidcTokenError("timeout"))
    with pytest.raises(go.CredentialRefreshError, match="failed"):
        go.ensure_fresh_for_inject(
            raw, token_port=port, write_full=lambda _t: None,
            lock_path=tmp_path / ".lock", now=now)


def test_ensure_fresh_corrupt_file_fails_closed(tmp_path):
    port = FakePort(result=TokenRefreshResult(access_token="x"))
    with pytest.raises(go.CredentialRefreshError, match="corrupt"):
        go.ensure_fresh_for_inject(
            "{not-json", token_port=port, write_full=lambda _t: None,
            lock_path=tmp_path / ".lock")


def test_ensure_fresh_lock_serializes_second_refresh(tmp_path):
    """Second waiter re-reads after lock; sees already-fresh file → one call."""
    now = time.time()
    path = tmp_path / "grok-auth.json"
    path.write_text(_cli_file(exp_unix=now - 5))
    state = {"n": 0}

    def refresh(**kwargs):
        state["n"] += 1
        # Simulate first refresher writing a long-lived token under the lock
        # before releasing — second caller re-reads via reread=.
        fresh = _cli_file(exp_unix=now + 10_000, refresh="rt-2",
                          access=_jwt(int(now + 10_000)))
        path.write_text(fresh)
        return TokenRefreshResult(
            access_token=_jwt(int(now + 10_000)),
            refresh_token="rt-2", expires_in=10_000)

    class CountingPort:
        def refresh(self, **kw):
            return refresh(**kw)

    port = CountingPort()
    lock = tmp_path / ".lock"

    def once():
        return go.ensure_fresh_for_inject(
            path.read_text(), token_port=port,
            write_full=path.write_text, lock_path=lock, now=now,
            reread=path.read_text)

    once()
    once()
    assert state["n"] == 1


def test_runspec_get_oauth_refresh_does_not_block_event_loop(
        tmp_path, monkeypatch):
    """runspec.get must leave the asyncio loop free while host Grok OAuth
    refresh blocks (flock + sync token POST). A concurrent probe on the
    same loop must complete before the blocking refresh returns."""
    import asyncio

    from devcake.adapters.files.run_store import RunStore
    from devcake.config import AppConfig, DevType, PMOInstance, RepoInstance
    from devcake.domain.run import Run
    from devcake.domain.runs import RunManager
    from fakes import FakeForgeRuntime, make_mission_manager

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets as secrets_store
    secrets_store.write_connection_secret(
        "repo", "main", "token", "ghp_write_token_for_tests_0001")

    now = time.time()
    auth_path = tmp_path / "secrets" / "main-dev" / "grok-auth.json"
    auth_path.parent.mkdir(parents=True)
    auth_path.write_text(_cli_file(exp_unix=now - 60, refresh="rt-block"))

    order: list[str] = []
    block_s = 0.35

    class BlockingPort:
        def refresh(self, *, refresh_token, client_id=None, token_url="",
                    timeout=15.0):
            assert refresh_token == "rt-block"
            time.sleep(block_s)
            order.append("refresh_return")
            return TokenRefreshResult(
                access_token=_jwt(int(now + 9000), iat=int(now)),
                refresh_token="rt-new",
                expires_in=9000.0,
            )

    cfg = AppConfig()
    cfg.repos = [RepoInstance(name="main", url="https://github.com/o/r")]
    mission_mgr = make_mission_manager(
        config=cfg,
        instance=PMOInstance(name="linear", team_key="DEV", repos=["main"]),
        forge_runtime=FakeForgeRuntime(object(), inst=cfg.repos[0]),
        dev_types={
            "main-dev": DevType(name="main-dev", harness_template="grok-build"),
        },
    )
    mission_mgr.oidc_tokens = BlockingPort()

    replies: list[tuple] = []

    class FakeMessaging:
        async def reply(self, run_id, kind, payload):
            replies.append((kind, payload))

        async def delete_runspec_result(self, rid):
            pass

    store = RunStore(tmp_path / "runs")
    manager = RunManager(store, FakeMessaging(), executor=None,
                         finalizer=mission_mgr)
    run = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="main-dev", seq=1,
              repo_ref="main", state="dispatched")
    store.save(run)

    async def probe():
        await asyncio.sleep(0.05)
        order.append("probe")

    async def exercise():
        await asyncio.gather(
            manager.handle(run.run_id, "runspec.get", {}),
            probe(),
        )

    asyncio.new_event_loop().run_until_complete(exercise())

    assert replies and replies[-1][0] == "runspec.result"
    assert "probe" in order and "refresh_return" in order
    assert order.index("probe") < order.index("refresh_return"), (
        f"event loop starved during OAuth refresh; order={order}")
