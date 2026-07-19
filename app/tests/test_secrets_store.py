"""Secret store hardening (audit A5/A9/A10/A18): deletes are real deletes,
corrupt files refuse read-modify-write, and the API endpoints validate their
inputs — closing the secrets-check path-traversal oracle."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import secrets
    return secrets


# ── store semantics ──────────────────────────────────────────────────────────

def test_delete_field_removes_key_then_file(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.write_connection_secret("repo", "main", "token", "tok-value-1234")
    s.write_connection_secret("repo", "main", "token_ro", "ro-value-1234")
    s.delete_connection_field("repo", "main", "token")
    p = tmp_path / "secrets" / "connections" / "repo-main.json"
    raw = json.loads(p.read_text())
    assert "token" not in raw                       # a real delete, not ""
    assert raw["token_ro"] == "ro-value-1234"
    s.delete_connection_field("repo", "main", "token_ro")
    assert not p.exists()                           # empty file removed


def test_delete_field_on_missing_file_is_a_noop(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.delete_connection_field("repo", "ghost", "token")
    assert not (tmp_path / "secrets" / "connections" / "repo-ghost.json").exists()


def test_corrupt_file_refuses_write_but_reads_lenient(monkeypatch, tmp_path):
    """A corrupt JSON file must not be silently replaced by a read-modify-
    write that would drop the sibling fields it once held."""
    s = _store(monkeypatch, tmp_path)
    p = tmp_path / "secrets" / "connections" / "repo-main.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"token": "tok-value-1234", CORRUPT')
    assert s.read_connection_secret("repo", "main", "token") == ""   # lenient
    assert s.connection_status("repo", "main", "token")["present"] is False
    with pytest.raises(ValueError):
        s.write_connection_secret("repo", "main", "token", "new-value-99")
    with pytest.raises(ValueError):
        s.delete_connection_field("repo", "main", "token")
    assert p.read_text() == '{"token": "tok-value-1234", CORRUPT'    # untouched


def test_harness_delete_unlinks(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.write_harness_secret("ANTHROPIC_API_KEY", "sk-ant-value-1234")
    assert s.harness_status("ANTHROPIC_API_KEY")["present"] is True
    s.delete_harness_secret("ANTHROPIC_API_KEY")
    assert s.harness_status("ANTHROPIC_API_KEY")["present"] is False
    assert not (tmp_path / "secrets" / "harness" / "ANTHROPIC_API_KEY.json").exists()
    s.delete_harness_secret("NEVER_SET")                             # no-op


# ── endpoint input validation (audit A5: the traversal oracle) ───────────────

def _main(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import main as app_main
    return app_main


def test_public_config_response_never_echoes_stored_secrets(monkeypatch,
                                                            tmp_path):
    """GET /config exposes configuration, never credential fields or values."""
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    from devcake.config import AppConfig, PMOInstance, RepoInstance

    pmo_secret = "pmo-sentinel-secret-1234"
    repo_secrets = {
        "token": "repo-write-sentinel-1234",
        "token_ro": "repo-read-sentinel-1234",
        "reviewer_token": "repo-review-sentinel-1234",
    }
    harness_secret = "harness-sentinel-secret-1234"
    s.write_connection_secret("pmo", "linear", "api_key", pmo_secret)
    for field, value in repo_secrets.items():
        s.write_connection_secret("repo", "main", field, value)
    s.write_harness_secret("ANTHROPIC_API_KEY", harness_secret)
    planted = {pmo_secret, *repo_secrets.values(), harness_secret}

    monkeypatch.setattr(
        app_main, "config",
        AppConfig(
            pmos=[PMOInstance(name="linear", team_key="DEV")],
            repos=[RepoInstance(name="main", url="https://example.test/o/r")],
        ),
    )
    probe = FastAPI()
    probe.add_api_route("/api/v1/config", app_main.get_config, methods=["GET"])
    response = TestClient(probe).get("/api/v1/config")
    assert response.status_code == 200

    payload = response.json()
    serialized = json.dumps(payload, sort_keys=True)
    for secret in planted:
        assert secret not in serialized

    forbidden = {"api_key", "token", "token_ro", "reviewer_token"}

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    exposed = {key for key in keys(payload)
               if key in forbidden or key.endswith("_env")}
    assert exposed == set()


def test_secrets_check_drops_invalid_refs(monkeypatch, tmp_path):
    """Query-string scope/instance/field reach the filesystem — hostile
    values (`../`, absolute paths) must be dropped, not stat'ed. Previously
    this was an existence+mtime+JSON-key oracle for arbitrary *.json files."""
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    s.write_connection_secret("repo", "main", "token", "tok-value-1234")
    out = run_coro(app_main.secrets_check(
        conn="repo:main:token"
             ",pmo:../../../etc/passwd:api_key"
             ",repo:main:../../harness/X"
             ",nope:main:token",
        harness="GOOD_VAR,../../evil,lower_case"))
    assert list(out["conn"]) == ["repo:main:token"]
    status = out["conn"]["repo:main:token"]
    assert set(status) == {"present", "updated_at"}
    assert status["present"] is True
    assert "tok-value-1234" not in json.dumps(out)
    assert list(out["harness"]) == ["GOOD_VAR"]


def test_put_secret_validates_scope_instance_field(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main = _main(monkeypatch, tmp_path)
    for scope, instance, field in [
            ("pmo", "../evil", "api_key"),          # traversal instance
            ("pmo", "Bad_Name", "api_key"),         # violates the name pattern
            ("repo", "main", "not_a_field"),        # unknown field
            ("pmo", "main", "token"),               # repo field on pmo scope
    ]:
        with pytest.raises(HTTPException) as e:
            run_coro(app_main.put_secret(scope, instance, field,
                                         {"value": "v-1234"}))
        assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.put_secret("nope", "main", "token", {"value": "v"}))
    assert e.value.status_code == 404


def test_delete_secret_nonexistent_creates_nothing(monkeypatch, tmp_path):
    app_main = _main(monkeypatch, tmp_path)
    run_coro(app_main.delete_secret("repo", "ghost", "token"))
    assert not (tmp_path / "secrets" / "connections" / "repo-ghost.json").exists()


def test_harness_secret_endpoint_validation_and_delete(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main = _main(monkeypatch, tmp_path)
    for bad in ("bad..var", "lower", "1LEADING", "A" * 65):
        with pytest.raises(HTTPException) as e:
            run_coro(app_main.put_harness_secret(bad, {"value": "v-1234"}))
        assert e.value.status_code == 422
    run_coro(app_main.put_harness_secret("XAI_API_KEY", {"value": "v-1234"}))
    out = run_coro(app_main.delete_harness_secret("XAI_API_KEY"))
    assert out == {"present": False}
    assert not (tmp_path / "secrets" / "harness" / "XAI_API_KEY.json").exists()
    with pytest.raises(HTTPException):
        run_coro(app_main.delete_harness_secret("../evil"))
