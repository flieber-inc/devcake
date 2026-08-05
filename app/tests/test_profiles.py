"""Config profiles (ADR-0013): store CRUD across both files, name hygiene,
divergence honesty, and the API surface (runs-active 409, presence-only
responses, audit events)."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake import profiles, secrets, settings_bundle
    from devcake.prompts import templates as prompt_templates
    return settings_bundle, profiles, secrets, config_mod, prompt_templates


def _small_world(config_mod, secrets):
    cfg = config_mod.AppConfig(
        repos=[config_mod.RepoInstance(name="main",
                                       url="https://github.com/acme/app")],
        assignments={mt: config_mod.Assignment(dev_type="senior-dev")
                     for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})
    dts = {"senior-dev": config_mod.DevType(
        name="senior-dev", harness_template="claude-code",
        identifying_prompt="Senior.")}
    secrets.write_connection_secret("repo", "main", "token",
                                    "ghp_profile_secret_value_01")
    return cfg, dts


# ── store ────────────────────────────────────────────────────────────────────

def test_save_read_roundtrip_splits_the_two_stores(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    profiles.save_profile("staging", bundle)
    # A in config/, B in secrets/ — and the yaml never holds a value
    yaml_text = (tmp_path / "config" / "profiles" / "staging.yaml").read_text()
    assert "ghp_profile_secret_value_01" not in yaml_text
    sec_file = tmp_path / "secrets" / "profiles" / "staging.json"
    assert sec_file.stat().st_mode & 0o777 == 0o600
    back = profiles.read_profile("staging")
    assert back["config"] == bundle["config"]
    assert back["secrets"] == bundle["secrets"]
    assert back["name"] == "staging"


def test_overwrite_semantics(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    profiles.save_profile("p", bundle)
    with pytest.raises(sb.BundleError) as e:
        profiles.save_profile("p", bundle)
    assert e.value.status == 409
    # overwrite with a config-only bundle narrows the profile — the stale
    # secrets snapshot must not survive
    slim = sb.serialize_current(cfg, dts, include_secrets=False)
    profiles.save_profile("p", slim, overwrite=True)
    assert not (tmp_path / "secrets" / "profiles" / "p.json").exists()
    assert "secrets" not in profiles.read_profile("p").get("sections")


def test_rename_moves_both_files_and_updates_breadcrumb(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    profiles.save_profile("old", sb.serialize_current(cfg, dts))
    profiles.record_applied("old")
    profiles.rename_profile("old", "new")
    assert not (tmp_path / "config" / "profiles" / "old.yaml").exists()
    assert not (tmp_path / "secrets" / "profiles" / "old.json").exists()
    assert (tmp_path / "secrets" / "profiles" / "new.json").exists()
    assert profiles.read_profile("new")["name"] == "new"
    assert profiles.last_applied()["name"] == "new"
    with pytest.raises(sb.BundleError) as e:
        profiles.rename_profile("new", "new")
    assert e.value.status == 409


def test_delete_removes_both_files(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    profiles.save_profile("gone", sb.serialize_current(cfg, dts))
    profiles.delete_profile("gone")
    assert not (tmp_path / "config" / "profiles" / "gone.yaml").exists()
    assert not (tmp_path / "secrets" / "profiles" / "gone.json").exists()
    with pytest.raises(sb.BundleError) as e:
        profiles.delete_profile("gone")
    assert e.value.status == 404


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", ".hidden", "x" * 65,
                                 "-leading-dash"])
def test_name_hygiene_rejects_traversal_shapes(monkeypatch, tmp_path, bad):
    sb, profiles, *_ = _env(monkeypatch, tmp_path)
    with pytest.raises(sb.BundleError) as e:
        profiles.require_name(bad)
    assert e.value.status == 422


def test_stale_profile_schema_refused(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    profiles.save_profile("old", sb.serialize_current(cfg, dts))
    p = tmp_path / "config" / "profiles" / "old.yaml"
    p.write_text(p.read_text().replace("bundle_schema_version: 1",
                                       "bundle_schema_version: 99"))
    with pytest.raises(sb.BundleError) as e:
        profiles.read_profile("old")
    assert "not auto-migrated" in str(e.value)


def test_promised_secrets_file_missing_refuses(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    profiles.save_profile("p", sb.serialize_current(cfg, dts,
                                                    include_secrets=True))
    (tmp_path / "secrets" / "profiles" / "p.json").unlink()
    with pytest.raises(sb.BundleError) as e:
        profiles.read_profile("p")
    assert "snapshot file is missing" in str(e.value)


def test_list_profiles_counts_only(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    profiles.save_profile("p", sb.serialize_current(cfg, dts,
                                                    include_secrets=True))
    rows = profiles.list_profiles()
    assert len(rows) == 1
    row = rows[0]
    assert row["counts"] == {"pmos": 0, "repos": 1, "dev_types": 1,
                             "prompt_templates": 0, "secrets": 1}
    assert "ghp_profile_secret_value_01" not in json.dumps(rows)


# ── divergence ───────────────────────────────────────────────────────────────

def test_divergence_flips_on_config_edit_and_secret_rewrite(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, _ = _env(monkeypatch, tmp_path)
    cfg, dts = _small_world(config_mod, secrets)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    profiles.save_profile("p", bundle)
    profiles.record_applied("p")
    applied_at = profiles.last_applied()["at"]

    assert profiles.diverged_since(profiles.read_profile("p"), applied_at,
                                   cfg, dts) is False
    cfg.poll_interval_seconds = 99
    assert profiles.diverged_since(profiles.read_profile("p"), applied_at,
                                   cfg, dts) is True
    cfg.poll_interval_seconds = 30
    assert profiles.diverged_since(profiles.read_profile("p"), applied_at,
                                   cfg, dts) is False
    time.sleep(0.02)   # ensure the rewrite's mtime lands after applied_at
    secrets.write_connection_secret("repo", "main", "token",
                                    "ghp_profile_secret_value_01")
    assert profiles.diverged_since(profiles.read_profile("p"), applied_at,
                                   cfg, dts) is True


# ── API surface (endpoint functions called directly, house pattern) ─────────

def _wire_app(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    from devcake.api import main as app_main
    from fakes import make_services
    cfg, dts = _small_world(config_mod, secrets)
    tpl.seed_devtype_prompts(dts)
    # ADR-0028: one test graph instead of six module-global patches
    monkeypatch.setattr(app_main, "services", make_services(
        config=cfg, dev_types=dts, reload_connections=lambda: None,
        store=SimpleNamespace(active=lambda: []), shared_breakers={},
        forge_runtime=SimpleNamespace(breakers={}), managers={}))
    return sb, profiles, secrets, config_mod, app_main


def test_endpoint_flow_save_list_get_apply_rename_delete(monkeypatch, tmp_path):
    from fastapi import HTTPException
    sb, profiles, secrets, config_mod, app_main = _wire_app(monkeypatch, tmp_path)

    out = run_coro(app_main.save_profile({"name": "base"}))
    assert out["saved"] and out["warnings"] == []
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.save_profile({"name": "base"}))
    assert e.value.status_code == 409
    run_coro(app_main.save_profile({"name": "base", "overwrite": True}))

    rows = run_coro(app_main.list_profiles())["profiles"]
    assert [r["name"] for r in rows] == ["base"]
    assert rows[0]["last_applied_at"] is None

    detail = run_coro(app_main.get_profile("base"))
    text = json.dumps(detail)
    assert "ghp_profile_secret_value_01" not in text
    assert detail["secrets_present"]["connections"] == {"repo-main": ["token"]}
    assert "diff" in detail

    app_main.services.config.poll_interval_seconds = 77   # drift before the apply
    out = run_coro(app_main.apply_profile("base"))
    assert sorted(out["applied"]) == ["config", "secrets"]
    assert app_main.services.config.poll_interval_seconds == 30
    rows = run_coro(app_main.list_profiles())["profiles"]
    assert rows[0]["last_applied_at"] and rows[0]["diverged"] is False

    out = run_coro(app_main.rename_profile("base", {"new_name": "renamed"}))
    assert out["name"] == "renamed"
    out = run_coro(app_main.delete_profile("renamed"))
    assert out["deleted"] == "renamed"

    actions = [json.loads(line)["action"] for line in
               (tmp_path / "state" / "events.jsonl").read_text().splitlines()]
    assert actions.count("profile_saved") == 2
    assert "profile_applied" in actions and "profile_renamed" in actions \
        and "profile_deleted" in actions
    log_text = (tmp_path / "state" / "events.jsonl").read_text()
    assert "ghp_profile_secret_value_01" not in log_text


def test_apply_blocked_while_runs_active(monkeypatch, tmp_path):
    from fastapi import HTTPException
    sb, profiles, secrets, config_mod, app_main = _wire_app(monkeypatch, tmp_path)
    run_coro(app_main.save_profile({"name": "base"}))
    app_main.services.store = SimpleNamespace(
        active=lambda: [SimpleNamespace(state="running")])
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.apply_profile("base"))
    assert e.value.status_code == 409
    assert "run(s) active" in e.value.detail
    # save/list/get stay available while runs are active
    assert run_coro(app_main.get_profile("base"))["name"] == "base"
    run_coro(app_main.save_profile({"name": "second"}))


def test_save_warns_on_configured_instance_without_secret(monkeypatch, tmp_path):
    sb, profiles, secrets, config_mod, app_main = _wire_app(monkeypatch, tmp_path)
    app_main.services.config.pmos = [config_mod.PMOInstance(
        name="linear", team_key="ENG", repos=["main"])]
    out = run_coro(app_main.save_profile({"name": "gappy"}))
    assert any("no stored API key" in w for w in out["warnings"])


def test_apply_missing_profile_404(monkeypatch, tmp_path):
    from fastapi import HTTPException
    sb, profiles, secrets, config_mod, app_main = _wire_app(monkeypatch, tmp_path)
    with pytest.raises(HTTPException) as e:
        run_coro(app_main.apply_profile("ghost"))
    assert e.value.status_code == 404
