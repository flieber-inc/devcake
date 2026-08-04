"""The composition root, exercised as an ASGI app (2026-08 evaluation).

Every prior suite tested route helpers as plain functions, so the ONE line
wiring auth onto the app — `app.middleware("http")(enforce_control_plane_auth)`
in api/main.py — had no test: deleting it left the whole suite green while
disabling control-plane auth on a system whose admin password is
host-root-equivalent (settings export carries DAGU_PASSWORD → docker.sock).
These tests build a real TestClient over the REAL `devcake.api.main:app` and
walk `app.routes`, so a route added tomorrow is covered the day it lands.

No lifespan: the client is used without a context manager, so boot (Redis,
label ensure, reconcile) never runs — and the middleware answers 401/403
BEFORE any handler executes, so no handler side effects are possible either.
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.routing import APIRoute          # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

LIVE_PATH = "/api/v1/health/live"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_AUTH = ("ci-admin", "ci-secret-for-surface-tests")


def _app():
    from devcake.api import main as app_main
    return app_main.app


def _routes():
    """(method, concrete_path) for every APIRoute, path params filled with a
    plain token — validation happens after the middleware, so the fill only
    needs to produce a routable URL."""
    out = []
    for route in _app().routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        for part in [p for p in path.split("/") if p.startswith("{")]:
            path = path.replace(part, "param")
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path, route.path))
    return out


ROUTES = _routes()


def test_the_surface_is_the_real_one():
    """A wiring regression that emptied the app would green every parametrized
    case below — pin the floor (52 route templates at time of writing; grows
    freely, and a drop below 50 means the surface itself broke)."""
    assert len({tpl for _m, _p, tpl in ROUTES}) >= 50


@pytest.mark.parametrize("method,path,template", ROUTES,
                         ids=[f"{m} {t}" for m, _p, t in ROUTES])
def test_every_route_requires_auth(monkeypatch, method, path, template):
    """401 without credentials on EVERY route except liveness — this is the
    test that makes api/main.py's middleware attachment load-bearing."""
    monkeypatch.setenv("ADMIN_USER", _AUTH[0])
    monkeypatch.setenv("ADMIN_PASSWORD", _AUTH[1])
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.request(method, path)
    if template == LIVE_PATH:
        assert r.status_code == 200
    else:
        assert r.status_code == 401, f"{method} {template} answered without auth"
        assert r.headers.get("www-authenticate", "").startswith("Basic")


@pytest.mark.parametrize(
    "method,path,template",
    [(m, p, t) for m, p, t in ROUTES if m in MUTATING],
    ids=[f"{m} {t}" for m, p, t in ROUTES if m in MUTATING])
def test_mutating_routes_require_intent_header(monkeypatch, method, path,
                                               template):
    """Authenticated but header-less mutation → 403 (the CSRF closure: a
    browser form can carry cached basic auth but can never set the header)."""
    monkeypatch.setenv("ADMIN_USER", _AUTH[0])
    monkeypatch.setenv("ADMIN_PASSWORD", _AUTH[1])
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.request(method, path, auth=_AUTH)
    assert r.status_code == 403, \
        f"{method} {template} mutated without the intent header"


def test_unconfigured_credentials_fail_closed(monkeypatch):
    """Empty ADMIN env answers 401 even to an empty-credential probe — the
    _valid_basic_auth 'bool(expected…)' term, exercised through the app."""
    monkeypatch.delenv("ADMIN_USER", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    client = TestClient(_app(), raise_server_exceptions=False)
    assert client.get("/api/v1/health").status_code == 401
    assert client.get("/api/v1/health", auth=("", "")).status_code == 401
