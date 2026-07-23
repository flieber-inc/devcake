"""OpenObserve ingest-user auto-provision (app boot).

Public seam: ``devcake.telemetry.oo_provision.ensure_oo_ingest_user``.
Mirrors scripts/provision_oo.py ensure_ingest_user — create / verify /
password-resync — but callable from lifespan so a filled .env is enough.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from devcake.telemetry.oo_provision import (
    OoProvisionError,
    ensure_oo_ingest_user,
)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


ROOT = ("root@example.com", "Root-Pass1!")
INGEST = ("ingest@example.com", "Ingest-Pass1!")
ORG = "default"
BASE = "http://openobserve:5080"


def _auth(email: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{password}".encode()).decode()


def _call(**kwargs):
    defaults = dict(
        base_url=BASE,
        org=ORG,
        root_email=ROOT[0],
        root_password=ROOT[1],
        ingest_email=INGEST[0],
        ingest_password=INGEST[1],
    )
    defaults.update(kwargs)
    return run_coro(ensure_oo_ingest_user(**defaults))


class _FakeOO:
    """Minimal OO users + streams surface for the provisioner."""

    def __init__(self, *, users=None, ingest_ok=True, create_status=200,
                 resync_status=200, list_status=200):
        self.users = list(users or [])
        self.ingest_ok = ingest_ok
        self.create_status = create_status
        self.resync_status = resync_status
        self.list_status = list_status
        self.calls: list[tuple[str, str, str | None]] = []  # method, path, auth

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization")
        path = request.url.path
        self.calls.append((request.method, path, auth))
        # streams probe (ingest or root)
        if path.endswith("/streams") or "/streams" in path:
            if auth == _auth(*INGEST) and self.ingest_ok:
                return httpx.Response(200, json={"list": []})
            return httpx.Response(401, text="Unauthorized Access")
        if path.endswith("/users") and request.method == "GET":
            if auth != _auth(*ROOT):
                return httpx.Response(401, text="Unauthorized Access")
            if self.list_status != 200:
                return httpx.Response(self.list_status, text="oops")
            return httpx.Response(200, json={"data": list(self.users)})
        if path.endswith("/users") and request.method == "POST":
            if auth != _auth(*ROOT):
                return httpx.Response(401, text="Unauthorized Access")
            if self.create_status != 200:
                return httpx.Response(self.create_status, text="create failed")
            body = json.loads(request.content.decode())
            self.users.append({
                "email": body["email"],
                "role": body.get("role", "service_account"),
            })
            self.ingest_ok = True
            return httpx.Response(200, json={"code": 200, "message": "User saved"})
        if "/users/" in path and request.method == "PUT":
            if auth != _auth(*ROOT):
                return httpx.Response(401, text="Unauthorized Access")
            if self.resync_status != 200:
                return httpx.Response(self.resync_status, text="resync failed")
            self.ingest_ok = True
            return httpx.Response(200, json={"code": 200})
        return httpx.Response(404, text=f"unhandled {request.method} {path}")


def test_creates_missing_ingest_user():
    oo = _FakeOO(users=[{"email": ROOT[0], "role": "root"}])
    status = _call(transport=httpx.MockTransport(oo.handler))
    assert status == "created"
    methods = [m for m, p, _ in oo.calls]
    assert "POST" in methods
    assert any(u["email"] == INGEST[0] for u in oo.users)
    post = next(c for c in oo.calls if c[0] == "POST")
    assert post[1].endswith(f"/api/{ORG}/users")


def test_verifies_existing_user_with_matching_password():
    oo = _FakeOO(users=[
        {"email": ROOT[0], "role": "root"},
        {"email": INGEST[0], "role": "service_account"},
    ], ingest_ok=True)
    status = _call(transport=httpx.MockTransport(oo.handler))
    assert status == "verified"
    assert not any(m == "POST" for m, _, _ in oo.calls)
    assert not any(m == "PUT" for m, _, _ in oo.calls)


def test_resyncs_password_when_user_exists_but_creds_fail():
    oo = _FakeOO(users=[
        {"email": ROOT[0], "role": "root"},
        {"email": INGEST[0], "role": "service_account"},
    ], ingest_ok=False)
    # first streams check fails; after PUT, succeed
    attempts = {"n": 0}
    real = oo.handler

    def handler(request):
        if "/streams" in request.url.path and request.headers.get("Authorization") == _auth(*INGEST):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(401, text="Unauthorized Access")
            return httpx.Response(200, json={"list": []})
        return real(request)

    status = _call(transport=httpx.MockTransport(handler))
    assert status == "password_resynced"
    assert any(m == "PUT" for m, _, _ in oo.calls)


def test_missing_ingest_env_raises():
    with pytest.raises(OoProvisionError, match="OO_INGEST"):
        _call(ingest_email="", transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def test_root_list_failure_raises():
    oo = _FakeOO(list_status=401)
    with pytest.raises(OoProvisionError, match="list"):
        _call(transport=httpx.MockTransport(oo.handler))


def test_create_failure_raises():
    oo = _FakeOO(users=[{"email": ROOT[0], "role": "root"}], create_status=400)
    with pytest.raises(OoProvisionError, match="creat"):
        _call(transport=httpx.MockTransport(oo.handler))


def test_create_then_verify_fail_raises():
    """POST succeeds but ingest Basic auth still 401 — hard fail."""
    # ingest_ok stays False even after create (OO accepted the user write
    # but credentials still do not work — e.g. org mismatch).
    oo = _FakeOO(users=[{"email": ROOT[0], "role": "root"}], ingest_ok=False)

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/users"):
            oo.users.append({"email": INGEST[0], "role": "service_account"})
            return httpx.Response(200, json={"code": 200})
        return oo.handler(request)

    with pytest.raises(OoProvisionError, match="do not authenticate"):
        _call(transport=httpx.MockTransport(handler))


def test_create_posts_service_account_role():
    oo = _FakeOO(users=[{"email": ROOT[0], "role": "root"}])
    body_holder: dict = {}

    def handler(request):
        if request.method == "POST" and request.url.path.endswith("/users"):
            body_holder.update(json.loads(request.content.decode()))
        return oo.handler(request)

    _call(transport=httpx.MockTransport(handler))
    assert body_holder.get("role") == "service_account"
    assert body_holder.get("email") == INGEST[0]
    assert body_holder.get("password") == INGEST[1]
