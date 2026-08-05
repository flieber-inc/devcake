"""Settings export/import endpoints (ADR-0013 part 2): encryption
enforcement, no-values-in-responses, import-lands-as-profile, the generated
.env, and parse hardening (safe_load, size cap)."""

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _wire_app(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake import secrets
    from devcake.api import main as app_main
    from devcake.prompts import templates as tpl
    cfg = config_mod.AppConfig(
        repos=[config_mod.RepoInstance(name="main",
                                       url="https://github.com/acme/app")],
        assignments={mt: config_mod.Assignment(dev_type="senior-dev")
                     for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})
    dts = {"senior-dev": config_mod.DevType(
        name="senior-dev", harness_template="claude-code",
        identifying_prompt="Senior.")}
    tpl.seed_devtype_prompts(dts)
    secrets.write_connection_secret("repo", "main", "token",
                                    "ghp_transfer_secret_value_01")
    secrets.write_harness_secret("ANTHROPIC_API_KEY", "sk-ant-transfer-02")
    from devcake.domain.skills import SkillService
    from fakes import make_services
    # ADR-0028: one test graph instead of five module-global patches.
    # SkillService(None) serves the bundled skills forge-less, exactly as
    # the old module global did in a GITEA-less test env.
    monkeypatch.setattr(app_main, "services", make_services(
        config=cfg, dev_types=dts, reload_connections=lambda: None,
        store=SimpleNamespace(active=lambda: []), shared_breakers={},
        managers={}, forge_runtime=SimpleNamespace(breakers={}),
        skill_service=SkillService(None)))
    return app_main, config_mod, secrets


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


ALL = {"config": True, "secrets": True, "setup_env": False}
ENC = {"mode": "passphrase", "passphrase": "correct horse"}


# ── export ───────────────────────────────────────────────────────────────────

def test_export_requires_encryption_choice_for_secret_sections(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.export_settings({"sections": ALL}))
    assert e.value.status_code == 422 and "encryption" in e.value.detail
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.export_settings(
            {"sections": ALL, "encryption": {"mode": "plaintext"}}))
    assert "acknowledged" in e.value.detail
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.export_settings(
            {"sections": ALL,
             "encryption": {"mode": "passphrase", "passphrase": "short"}}))
    assert "at least 8" in e.value.detail
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.export_settings({"sections": {}}))
    assert "at least one section" in e.value.detail
    # config-only needs no encryption choice
    r = run_coro(app_main.export_settings(
        {"sections": {"config": True}}))
    assert "ghp_transfer_secret_value_01" not in r.body.decode()


def test_encrypted_export_contains_no_secret_bytes_plaintext_does(monkeypatch, tmp_path):
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    enc = run_coro(app_main.export_settings(
        {"sections": ALL, "encryption": ENC})).body.decode()
    assert "ghp_transfer_secret_value_01" not in enc
    assert "sk-ant-transfer-02" not in enc
    assert "protected:" in enc and "plaintext_secrets" not in enc
    plain = run_coro(app_main.export_settings(
        {"sections": ALL,
         "encryption": {"mode": "plaintext",
                        "acknowledge_plaintext": True}})).body.decode()
    assert "ghp_transfer_secret_value_01" in plain
    assert "plaintext_secrets: true" in plain
    assert "attachment" in dict(
        run_coro(app_main.export_settings(
            {"sections": {"config": True}})).headers)["content-disposition"]


def test_export_audits_with_encrypted_flag(monkeypatch, tmp_path):
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    run_coro(app_main.export_settings({"sections": ALL, "encryption": ENC}))
    lines = [json.loads(line) for line in
             (tmp_path / "state" / "events.jsonl").read_text().splitlines()]
    ev = [e for e in lines if e["action"] == "settings_exported"][-1]
    assert "encrypted=True" in ev["detail"]
    assert "ghp_transfer_secret_value_01" not in ev["detail"]


def test_export_from_profile_source(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    run_coro(app_main.save_profile({"name": "base"}))
    out = run_coro(app_main.export_settings(
        {"source": {"profile": "base"}, "sections": ALL,
         "encryption": ENC})).body.decode()
    assert "protected:" in out
    # profiles never hold setup values
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.export_settings(
            {"source": {"profile": "base"},
             "sections": {"setup_env": True}, "encryption": ENC}))
    assert "setup" in e.value.detail
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.export_settings(
            {"source": {"profile": "ghost"}, "sections": {"config": True}}))
    assert e.value.status_code == 404


# ── preview + import ─────────────────────────────────────────────────────────

def test_preview_needs_passphrase_then_no_values(monkeypatch, tmp_path):
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    text = run_coro(app_main.export_settings(
        {"sections": ALL, "encryption": ENC})).body.decode()
    out = run_coro(app_main.import_preview({"content_b64": _b64(text)}))
    assert out == {"needs_passphrase": True}
    out = run_coro(app_main.import_preview(
        {"content_b64": _b64(text), "passphrase": "correct horse"}))
    s = json.dumps(out)
    assert "ghp_transfer_secret_value_01" not in s
    assert "sk-ant-transfer-02" not in s
    assert out["sections_present"] == ["config", "secrets"]
    assert out["plaintext_secrets"] is False


def test_wrong_passphrase_is_one_422(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    text = run_coro(app_main.export_settings(
        {"sections": ALL, "encryption": ENC})).body.decode()
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.import_preview(
            {"content_b64": _b64(text), "passphrase": "wrong horse!!"}))
    assert e.value.status_code == 422
    assert e.value.detail == "wrong passphrase or corrupted bundle"


def test_import_lands_as_profile_then_apply_restores(monkeypatch, tmp_path):
    app_main, config_mod, secrets = _wire_app(monkeypatch, tmp_path)
    text = run_coro(app_main.export_settings(
        {"sections": ALL, "encryption": ENC})).body.decode()
    # drift the live world, then import + apply the bundle
    app_main.services.config.poll_interval_seconds = 99
    secrets.write_harness_secret("ANTHROPIC_API_KEY", "sk-rotated-03")
    out = run_coro(app_main.import_settings(
        {"content_b64": _b64(text), "passphrase": "correct horse",
         "save_as": "imported"}))
    assert out["saved_as"] == "imported"
    assert out["sections"] == ["config", "secrets"]
    assert out["has_setup_env"] is False
    # nothing applied yet
    assert app_main.services.config.poll_interval_seconds == 99
    run_coro(app_main.apply_profile("imported"))
    assert app_main.services.config.poll_interval_seconds == 30
    assert secrets.read_harness_secret("ANTHROPIC_API_KEY") == "sk-ant-transfer-02"


def test_import_name_collision_409(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    text = run_coro(app_main.export_settings(
        {"sections": {"config": True}})).body.decode()
    run_coro(app_main.import_settings({"content_b64": _b64(text),
                                       "save_as": "dup"}))
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.import_settings({"content_b64": _b64(text),
                                           "save_as": "dup"}))
    assert e.value.status_code == 409
    run_coro(app_main.import_settings({"content_b64": _b64(text),
                                       "save_as": "dup", "overwrite": True}))


# ── setup_env (.env) ─────────────────────────────────────────────────────────

def test_env_roundtrip_generates_placeable_file(monkeypatch, tmp_path):
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-strong")
    monkeypatch.setenv("DOCKER_GID", "989")
    monkeypatch.setenv("DEVCAKE_ALLOW_INSECURE", "1")
    text = run_coro(app_main.export_settings(
        {"sections": {"setup_env": True}, "encryption": ENC})).body.decode()
    r = run_coro(app_main.import_env(
        {"content_b64": _b64(text), "passphrase": "correct horse"}))
    env = r.body.decode()
    assert "ADMIN_PASSWORD=hunter2-strong" in env
    assert "DOCKER_GID=989" in env
    assert "HOST-SPECIFIC" in env
    assert "DEVCAKE_ALLOW_INSECURE" not in env
    for line in env.splitlines():                     # parseable KEY=VALUE
        assert not line or line.startswith("#") or "=" in line
    assert 'filename="devcake.env"' in dict(r.headers)["content-disposition"]


def test_env_absent_section_422(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    text = run_coro(app_main.export_settings(
        {"sections": {"config": True}})).body.decode()
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.import_env({"content_b64": _b64(text)}))
    assert "no setup_env" in e.value.detail


# ── parse hardening ──────────────────────────────────────────────────────────

def test_yaml_object_tags_are_refused(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    evil = "!!python/object/apply:os.system ['id']"
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.import_preview({"content_b64": _b64(evil)}))
    assert e.value.status_code == 422


def test_size_cap_and_bad_base64(monkeypatch, tmp_path):
    from fastapi import HTTPException
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    from devcake.api import settings_transfer
    monkeypatch.setattr(settings_transfer, "MAX_BUNDLE_BYTES", 64)
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.import_preview({"content_b64": _b64("x" * 100)}))
    assert "exceeds" in e.value.detail
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.import_preview({"content_b64": "not-base-64!!"}))
    assert "base64" in e.value.detail


def test_summary_counts_only(monkeypatch, tmp_path):
    app_main, *_ = _wire_app(monkeypatch, tmp_path)
    out = run_coro(app_main.export_summary())
    assert out["secrets"]["total"] == 2
    assert out["secrets"]["by_scope"]["repo"] == 1
    assert "ghp_transfer_secret_value_01" not in json.dumps(out)
