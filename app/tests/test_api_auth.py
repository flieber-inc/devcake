import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from devcake.api.auth import enforce_control_plane_auth


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def test_control_plane_auth_and_request_intent(monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    app = FastAPI()
    app.middleware("http")(enforce_control_plane_auth)

    @app.get("/api/v1/health/live")
    async def live():
        return {"app": True}

    @app.get("/api/v1/config")
    async def read_config():
        return {"ok": True}

    @app.post("/api/v1/action")
    async def action():
        return {"ok": True}

    client = TestClient(app)
    auth = {"Authorization": _basic("operator", "correct-horse")}

    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/config").status_code == 401
    assert client.get("/api/v1/config", headers=auth).status_code == 200
    assert client.post("/api/v1/action", headers=auth).status_code == 403
    assert client.post(
        "/api/v1/action", headers={**auth, "X-DevCake-Request": "1"}
    ).status_code == 200


def test_malformed_basic_auth_is_rejected(monkeypatch):
    monkeypatch.setenv("ADMIN_USER", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    app = FastAPI()
    app.middleware("http")(enforce_control_plane_auth)

    @app.get("/api/v1/config")
    async def read_config():
        return {"ok": True}

    client = TestClient(app)
    assert client.get(
        "/api/v1/config", headers={"Authorization": "Basic not-base64"}
    ).status_code == 401


def test_malformed_host_cannot_disguise_protected_path_as_liveness(monkeypatch):
    """The Host header must not influence which route is exempt from auth.

    This pins the composite invariant, not auth.py alone: starlette >= 1.0.1
    itself refuses to let a malformed authority poison ``request.url.path``
    (GHSA-86qp-5c8j-p5mr), so on the pinned starlette this test passes even
    with the old ``request.url.path`` exemption (verified empirically). The
    ``scope["path"]`` check in auth.py is defense-in-depth against a
    starlette downgrade or upstream regression — a revert of auth.py by
    itself will NOT turn this test red while starlette stays patched.
    """
    monkeypatch.setenv("ADMIN_USER", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    app = FastAPI()
    app.middleware("http")(enforce_control_plane_auth)

    @app.get("/api/v1/health/live")
    async def live():
        return {"app": True}

    @app.get("/api/v1/config")
    async def read_config():
        return {"ok": True}

    client = TestClient(app)
    response = client.get(
        "/api/v1/config",
        headers={"Host": "example.com/api/v1/health/live?ignored="},
    )

    assert response.status_code == 401


def _auth_app(monkeypatch, user: str, password: str) -> TestClient:
    monkeypatch.setenv("ADMIN_USER", user)
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    app = FastAPI()
    app.middleware("http")(enforce_control_plane_auth)

    @app.get("/api/v1/config")
    async def read_config():
        return {"ok": True}

    return TestClient(app)


def test_wrong_password_same_length_is_401(monkeypatch):
    """Mismatched password of equal length must be 401, never 500."""
    client = _auth_app(monkeypatch, "operator", "correct-horse")
    bad = {"Authorization": _basic("operator", "correct-xxxxx")}  # same len
    assert client.get("/api/v1/config", headers=bad).status_code == 401


def test_wrong_password_different_length_is_401(monkeypatch):
    """Length mismatch must stay 401 (never an uncaught compare_digest error)."""
    client = _auth_app(monkeypatch, "operator", "correct-horse")
    bad = {"Authorization": _basic("operator", "nope")}  # shorter
    resp = client.get("/api/v1/config", headers=bad)
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}"


def test_wrong_password_unicode_is_401(monkeypatch):
    """Historical 500: secrets.compare_digest on str rejects non-ASCII (TypeError)."""
    client = _auth_app(monkeypatch, "operator", "correct-horse")
    bad = {"Authorization": _basic("operator", "пароль-неверный")}
    resp = client.get("/api/v1/config", headers=bad)
    assert resp.status_code == 401, f"expected 401, got {resp.status_code}"


def test_correct_credentials_still_200(monkeypatch):
    client = _auth_app(monkeypatch, "operator", "correct-horse")
    auth = {"Authorization": _basic("operator", "correct-horse")}
    assert client.get("/api/v1/config", headers=auth).status_code == 200


def test_non_ascii_admin_password_accepts_and_rejects(monkeypatch):
    """Configured ADMIN_PASSWORD may be non-ASCII — must not 500 (bricking)."""
    client = _auth_app(monkeypatch, "operator", "sëcret-🔐-pass")
    good = {"Authorization": _basic("operator", "sëcret-🔐-pass")}
    bad = {"Authorization": _basic("operator", "wrong")}
    assert client.get("/api/v1/config", headers=good).status_code == 200
    assert client.get("/api/v1/config", headers=bad).status_code == 401


def test_non_ascii_admin_username_accepts_and_rejects(monkeypatch):
    """Configured ADMIN_USER may be non-ASCII — same compare_digest trap."""
    client = _auth_app(monkeypatch, "оператор", "correct-horse")
    good = {"Authorization": _basic("оператор", "correct-horse")}
    bad = {"Authorization": _basic("operator", "correct-horse")}
    assert client.get("/api/v1/config", headers=good).status_code == 200
    assert client.get("/api/v1/config", headers=bad).status_code == 401


def test_both_credential_fields_compared_even_on_username_miss(monkeypatch):
    """SEC-9 (2026-08-12 audit): the old short-circuit `and` skipped the
    password compare when the username missed — a measurable timing oracle
    for username validity. Call-count pins the property (timing asserts
    flake); both comparisons must run on every well-formed attempt."""
    from devcake.api import auth as auth_mod

    monkeypatch.setenv("ADMIN_USER", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    calls = []
    real = auth_mod._const_eq

    def counting(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_mod, "_const_eq", counting)
    assert auth_mod._valid_basic_auth(_basic("wrong-user", "whatever")) is False
    assert len(calls) == 2, "username miss must still compare the password"
    calls.clear()
    assert auth_mod._valid_basic_auth(_basic("operator", "correct-horse")) is True
    assert len(calls) == 2
