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


# ── inventory + bulk clear (operator Clear-secrets modal) ────────────────────

def test_delete_credential_file_unlinks(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    d = tmp_path / "secrets" / "judgment"
    d.mkdir(parents=True)
    p = d / "grok-auth.json"
    p.write_text('{"token":"oauth-sentinel-1234"}')
    p.chmod(0o600)
    s.delete_credential_file("judgment", "grok-auth.json")
    assert not p.exists()
    s.delete_credential_file("judgment", "missing.json")  # no-op
    # traversal / reserved dirs must refuse
    import pytest as _pytest
    with _pytest.raises(ValueError):
        s.delete_credential_file("harness", "ANTHROPIC_API_KEY.json")
    with _pytest.raises(ValueError):
        s.delete_credential_file("../etc", "passwd")
    with _pytest.raises(ValueError):
        s.delete_credential_file("judgment", "../harness/X.json")


def test_inventory_presence_only_never_values(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.write_connection_secret("pmo", "linear", "api_key", "pmo-sentinel-secret-xyz")
    s.write_connection_secret("repo", "main", "token", "repo-sentinel-secret-xyz")
    s.write_harness_secret("ANTHROPIC_API_KEY", "harness-sentinel-secret-xyz")
    d = tmp_path / "secrets" / "judgment"
    d.mkdir(parents=True)
    (d / "grok-auth.json").write_text('{"token":"oauth-sentinel-secret-xyz"}')
    # reserved system dirs must not appear as credential_files
    (tmp_path / "secrets" / "profiles").mkdir(parents=True)
    (tmp_path / "secrets" / "profiles" / "staging.json").write_text("{}")
    (tmp_path / "secrets" / "internal_forge").mkdir(parents=True)
    (tmp_path / "secrets" / "internal_forge" / "tok.json").write_text("{}")

    inv = s.inventory()
    assert {h["var"] for h in inv["harness"]} == {"ANTHROPIC_API_KEY"}
    assert all("updated_at" in h and "value" not in h for h in inv["harness"])
    assert {(c["scope"], c["instance"], c["field"]) for c in inv["connections"]} == {
        ("pmo", "linear", "api_key"),
        ("repo", "main", "token"),
    }
    assert {(f["dev_type"], f["filename"]) for f in inv["credential_files"]} == {
        ("judgment", "grok-auth.json"),
    }
    blob = json.dumps(inv)
    for secret in ("pmo-sentinel-secret-xyz", "repo-sentinel-secret-xyz",
                   "harness-sentinel-secret-xyz", "oauth-sentinel-secret-xyz"):
        assert secret not in blob


def test_secrets_inventory_endpoint_presence_only(monkeypatch, tmp_path):
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    s.write_harness_secret("XAI_API_KEY", "xai-sentinel-value-1234")
    d = tmp_path / "secrets" / "coder"
    d.mkdir(parents=True)
    (d / "codex-auth.json").write_text("oauth-file-sentinel-1234")
    s.write_connection_secret("repo", "main", "token_ro", "ro-sentinel-value-1234")

    out = run_coro(app_main.secrets_inventory())
    assert {h["var"] for h in out["harness"]} == {"XAI_API_KEY"}
    assert {(f["dev_type"], f["filename"]) for f in out["credential_files"]} == {
        ("coder", "codex-auth.json"),
    }
    assert any(c["field"] == "token_ro" for c in out["connections"])
    blob = json.dumps(out)
    for secret in ("xai-sentinel-value-1234", "oauth-file-sentinel-1234",
                   "ro-sentinel-value-1234"):
        assert secret not in blob


def test_secrets_clear_deletes_selected_only(monkeypatch, tmp_path):
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    s.write_harness_secret("XAI_API_KEY", "keep-or-drop-xai")
    s.write_harness_secret("ANTHROPIC_API_KEY", "drop-anthropic")
    s.write_connection_secret("pmo", "linear", "api_key", "drop-pmo")
    s.write_connection_secret("repo", "main", "token", "keep-repo")
    d = tmp_path / "secrets" / "coder"
    d.mkdir(parents=True)
    (d / "codex-auth.json").write_text("drop-oauth")
    (d / "other.json").write_text("keep-file")

    out = run_coro(app_main.clear_secrets({
        "harness": ["ANTHROPIC_API_KEY"],
        "connections": [{"scope": "pmo", "instance": "linear", "field": "api_key"}],
        "credential_files": [{"dev_type": "coder", "filename": "codex-auth.json"}],
    }))
    assert out["ok"] is True
    assert out["deleted"]["harness"] == ["ANTHROPIC_API_KEY"]
    assert s.harness_status("ANTHROPIC_API_KEY")["present"] is False
    assert s.harness_status("XAI_API_KEY")["present"] is True
    assert s.connection_status("pmo", "linear", "api_key")["present"] is False
    assert s.connection_status("repo", "main", "token")["present"] is True
    assert not (d / "codex-auth.json").exists()
    assert (d / "other.json").exists()
    # omit pause_intake → API default false; intake unchanged
    assert out["intake_paused"] is False
    assert app_main.config.intake_paused is False


def test_secrets_clear_rejects_traversal_and_empty(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main = _main(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.clear_secrets({}))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.clear_secrets({
            "harness": ["../EVIL"],
        }))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.clear_secrets({
            "credential_files": [{"dev_type": "connections",
                                  "filename": "repo-main.json"}],
        }))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.clear_secrets({
            "connections": [{"scope": "repo", "instance": "../x",
                             "field": "token"}],
        }))
    assert e.value.status_code == 422


def test_secrets_clear_pause_intake_default_and_explicit(monkeypatch, tmp_path):
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    s.write_harness_secret("XAI_API_KEY", "v-pause-test")
    app_main.config.intake_paused = False

    out = run_coro(app_main.clear_secrets({
        "harness": ["XAI_API_KEY"],
        "pause_intake": False,
    }))
    assert out["intake_paused"] is False
    assert app_main.config.intake_paused is False

    s.write_harness_secret("XAI_API_KEY", "v-pause-test-2")
    out = run_coro(app_main.clear_secrets({
        "harness": ["XAI_API_KEY"],
        "pause_intake": True,
    }))
    assert out["intake_paused"] is True
    assert app_main.config.intake_paused is True


def test_secrets_clear_pause_first_aborts_deletes_on_save_failure(
        monkeypatch, tmp_path):
    """If intake pause save fails, no secrets may be deleted."""
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    s.write_harness_secret("XAI_API_KEY", "must-survive")
    app_main.config.intake_paused = False

    def boom(_cfg):
        raise OSError("disk full")

    monkeypatch.setattr("devcake.config.save_config", boom)
    with pytest.raises(OSError):
        run_coro(app_main.clear_secrets({
            "harness": ["XAI_API_KEY"],
            "pause_intake": True,
        }))
    assert s.harness_status("XAI_API_KEY")["present"] is True
    assert app_main.config.intake_paused is True  # in-memory set before save


def test_secrets_clear_clears_breakers_and_audits(monkeypatch, tmp_path):
    app_main = _main(monkeypatch, tmp_path)
    s = _store(monkeypatch, tmp_path)
    from devcake.config import DevType
    from devcake.harness import HARNESSES

    # pick a real harness credential env var
    ht = next(iter(HARNESSES))
    var = HARNESSES[ht].credential_env[0]
    s.write_harness_secret(var, "breaker-sentinel-value")
    d = tmp_path / "secrets" / "coder"
    d.mkdir(parents=True)
    (d / "auth.json").write_text("file-sentinel")

    app_main.dev_types = {
        "coder": DevType(name="coder", harness_template=ht),
    }
    app_main.shared_breakers["coder"] = "DEV_AUTH"

    out = run_coro(app_main.clear_secrets({
        "harness": [var],
        "credential_files": [{"dev_type": "coder", "filename": "auth.json"}],
    }))
    assert out["ok"] is True
    assert "coder" not in app_main.shared_breakers

    audit = (tmp_path / "state" / "events.jsonl")
    # audit may live under DATA_DIR/state — ensure it was written somewhere
    # the settings_bundle path uses DEVCAKE_DATA_DIR
    from pathlib import Path
    import os
    data = Path(os.environ["DEVCAKE_DATA_DIR"])
    lines = []
    for p in data.rglob("events.jsonl"):
        lines.extend(p.read_text().splitlines())
    assert any("secrets_cleared" in ln for ln in lines)
    blob = "\n".join(lines)
    assert "breaker-sentinel-value" not in blob
    assert "file-sentinel" not in blob
