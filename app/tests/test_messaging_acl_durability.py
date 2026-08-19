"""Clear-runs / per-run ACL durability pins (docs/09 §1a).

Hermetic: no live Redis. The production clear-runs path used to bulk
``ACL DELUSER dev-*`` without ``ACL SAVE``, so a redis-only restart
resurrected wiped users from the compose aclfile. Per-run create/delete
already SAVE; the bulk revoke must too.
"""
from __future__ import annotations

import asyncio

from devcake.adapters.redis.messaging import Messaging
from devcake.security import redact, register_runtime_secret, unregister_runtime_secret


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _AclRedis:
    """Minimal fake: USERS / DELUSER / SAVE only (clear-runs ACL seam)."""

    def __init__(self, users: list[str]):
        self.users = list(users)
        self.commands: list[tuple] = []

    async def execute_command(self, *args):
        self.commands.append(args)
        op = args[0]
        sub = args[1] if len(args) > 1 else None
        if op == "ACL" and sub == "USERS":
            return list(self.users)
        if op == "ACL" and sub == "DELUSER":
            name = args[2]
            self.users = [u for u in self.users if u != name]
            return 1
        if op == "ACL" and sub == "SAVE":
            return "OK"
        raise AssertionError(f"unexpected command: {args}")


def test_revoke_leftover_run_users_acl_saves_after_deluser():
    """Bulk clear-runs revoke must persist DELUSER into the aclfile."""
    m = Messaging.__new__(Messaging)
    m.redis = _AclRedis(["default", "dev-RUN-A", "dev-RUN-B", "appother"])
    m._chunks = {}

    n = run(m.revoke_leftover_run_users())
    assert n == 2
    assert "dev-RUN-A" not in m.redis.users
    assert "dev-RUN-B" not in m.redis.users
    assert "default" in m.redis.users
    assert "appother" in m.redis.users

    cmds = m.redis.commands
    delusers = [c for c in cmds if c[:2] == ("ACL", "DELUSER")]
    saves = [c for c in cmds if c[:2] == ("ACL", "SAVE")]
    assert {c[2] for c in delusers} == {"dev-RUN-A", "dev-RUN-B"}
    assert len(saves) == 1, "one ACL SAVE after the bulk DELUSER set"
    # SAVE must follow every DELUSER (durability is useless if written first)
    last_deluser_idx = max(i for i, c in enumerate(cmds) if c[:2] == ("ACL", "DELUSER"))
    save_idx = next(i for i, c in enumerate(cmds) if c[:2] == ("ACL", "SAVE"))
    assert save_idx > last_deluser_idx


def test_revoke_leftover_run_users_no_save_when_nothing_to_delete():
    m = Messaging.__new__(Messaging)
    m.redis = _AclRedis(["default"])
    m._chunks = {}

    assert run(m.revoke_leftover_run_users()) == 0
    assert not any(c[:2] == ("ACL", "SAVE") for c in m.redis.commands)


def test_revoke_leftover_run_users_unregisters_runtime_secrets():
    """Clear-runs must drop process-local redaction entries for wiped users."""
    rid = "WIPE-RUN-1"
    secret = "clear-runs-relay-password-abcdef"
    register_runtime_secret(rid, secret)
    assert secret not in redact(f"leak {secret}")

    m = Messaging.__new__(Messaging)
    m.redis = _AclRedis(["default", f"dev-{rid}"])
    m._chunks = {}
    try:
        assert run(m.revoke_leftover_run_users()) == 1
        # after unregister, redact no longer knows the token
        assert secret in redact(f"leak {secret}")
    finally:
        unregister_runtime_secret(rid)


def test_clear_redis_routes_acl_sweep_through_revoke_helper(monkeypatch):
    """clear_redis must not reimplement bulk DELUSER without SAVE, and must
    clear in-process chunk assemblies via the public Messaging method."""
    from devcake.api import clear as clear_mod

    calls: list[str] = []

    class _Msg:
        def __init__(self):
            self.redis = _ScanRedis()
            self._chunks = {("a", "b", "c"): {}}

        async def revoke_leftover_run_users(self):
            calls.append("revoke")
            return 3

        def clear_chunk_assemblies(self) -> None:
            calls.append("chunks")
            self._chunks.clear()

    class _ScanRedis:
        async def scan_iter(self, match=None, count=200):
            if False:  # make this an async generator
                yield None

        async def xtrim(self, *a, **k):
            return 0

        async def delete(self, *a, **k):
            return 1

    msg = _Msg()
    out = run(clear_mod.clear_redis(msg))
    assert calls == ["revoke", "chunks"]
    assert out["acl_users_deleted"] == 3
    assert out["ingress_trimmed"] is True
    assert msg._chunks == {}
