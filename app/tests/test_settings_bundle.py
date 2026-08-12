"""Settings bundle (ADR-0013): serialize/validate/diff/apply — replace-the-
world semantics, rollback-by-reapply, scrubbed validation errors (a malformed
secrets section must never echo a value), and the .env tripwire."""

import copy
from pathlib import Path

import pytest


def _env(monkeypatch, tmp_path: Path):
    """Point every store at tmp_path (env resolves per call; CONFIG_PATH is
    import-time, so monkeypatch the module attribute too — house pattern)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake import profiles, secrets, settings_bundle
    from devcake.prompts import templates as prompt_templates
    return settings_bundle, profiles, secrets, config_mod, prompt_templates


def _world(config_mod, secrets, prompt_templates):
    """A small but full world: pmo + repo + custom dev type + operator
    template + secrets."""
    cfg = config_mod.AppConfig(
        pmos=[config_mod.PMOInstance(name="linear", team_key="ENG",
                                     repos=["main"])],
        repos=[config_mod.RepoInstance(name="main",
                                       url="https://github.com/acme/app")],
        poll_interval_seconds=45,
        active_prompt_templates={"PLAN": "tighter-plan"},
        # a consistent world: every assignment names a dev type this world
        # actually carries (live deployments guarantee this — remove_dev_type
        # refuses while assigned)
        assignments={mt: config_mod.Assignment(dev_type="senior-dev")
                     for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")},
        steward=config_mod.Steward(dev_type="senior-dev"),
    )
    dts = {
        "senior-dev": config_mod.DevType(
            name="senior-dev", harness_template="claude-code",
            identifying_prompt="You are Senior Dev.", model="claude-fable-5"),
        "extra-dev": config_mod.DevType(
            name="extra-dev", harness_template="codex",
            identifying_prompt="Extra.", secret_env=["DD_API_KEY"]),
    }
    prompt_templates.seed_devtype_prompts(dts)
    prompt_templates.save_template("PLAN", "tighter-plan",
                                   "Plan tightly for {title}.")
    secrets.write_connection_secret("pmo", "linear", "api_key",
                                    "lin_api_secret_value_0001")
    secrets.write_connection_secret("repo", "main", "token",
                                    "ghp_secret_value_0002")
    secrets.write_harness_secret("ANTHROPIC_API_KEY", "sk-ant-secret-0003")
    return cfg, dts


def _comparable(bundle: dict) -> dict:
    b = copy.deepcopy(bundle)
    b.pop("created_at", None)
    return b


# ── round trip ───────────────────────────────────────────────────────────────

def test_serialize_apply_roundtrip_onto_fresh_deployment(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path / "src")
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)

    # fresh deployment: repoint every store, apply, serialize again
    dst = tmp_path / "dst"
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(dst))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        dst / "config" / "config.yaml")
    cfg2, dts2 = config_mod.AppConfig(), {}
    result = sb.apply_bundle(bundle, config=cfg2, dev_types=dts2,
                             reload=lambda: None)
    assert sorted(result["applied"]) == ["config", "secrets"]
    again = sb.serialize_current(cfg2, dts2, include_secrets=True)
    assert _comparable(again) == _comparable(bundle)
    # the fresh world holds the values on disk, 0600
    assert secrets.read_connection_secret("pmo", "linear", "api_key") \
        == "lin_api_secret_value_0001"
    assert (dst / "secrets" / "connections" / "pmo-linear.json").stat().st_mode & 0o777 == 0o600


def test_rollback_snapshot_keeps_orphan_secrets_byte_exact(monkeypatch, tmp_path):
    """Re-audit #3: user-facing snapshots drop orphan secrets, but the apply
    ROLLBACK snapshot must be byte-exact — include_orphan_secrets=True — or a
    failed apply loses a secret stored for an instance the bundle was adding."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    secrets.write_connection_secret("repo", "incoming", "token", "ghp_incoming_7")
    # user-facing (default) drops the not-yet-in-config secret
    facing = sb.serialize_current(cfg, dts, include_secrets=True)
    assert "repo-incoming" not in facing["secrets"]["connections"]
    # rollback snapshot keeps it byte-exact
    rollback = sb.serialize_current(cfg, dts, include_secrets=True,
                                    include_orphan_secrets=True)
    assert rollback["secrets"]["connections"]["repo-incoming"] == {"token": "ghp_incoming_7"}


def test_serialize_excludes_orphan_connection_secrets(monkeypatch, tmp_path):
    """Audit D5 #15/#16: a stored secret for an instance NOT in the config
    (deleted card, secret lingered) must not ride the snapshot — else it shows
    'replaced' in the apply preview but is dropped by apply, and flags the
    profile permanently diverged."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    # an orphan: 'bare' is in neither cfg.pmos nor cfg.repos
    secrets.write_connection_secret("repo", "bare", "token", "ghp_orphan_9999")
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    conns = bundle["secrets"]["connections"]
    assert "repo-bare" not in conns                 # orphan excluded
    assert "pmo-linear" in conns and "repo-main" in conns   # live ones kept
    # the live orphan on disk is untouched
    assert secrets.read_connection_secret("repo", "bare", "token") == "ghp_orphan_9999"


def test_apply_is_replace_the_world_and_spares_the_exclusions(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)

    # target world: extras the bundle does not carry...
    extra_dt = config_mod.DevType(name="doomed-dev", harness_template="codex")
    config_mod.save_dev_type(extra_dt)
    tpl.save_template("REVIEW", "doomed-template", "Review {title}.")
    secrets.write_connection_secret("repo", "doomed", "token",
                                    "doomed-token-value-0009")
    secrets.write_harness_secret("DOOMED_KEY", "doomed-harness-value-0010")
    # ...and the never-touched stores
    cred = tmp_path / "secrets" / "senior-dev" / "creds.json"
    cred.parent.mkdir(parents=True)
    cred.write_text("{}")
    forge_tok = tmp_path / "secrets" / "internal_forge" / "service.json"
    forge_tok.parent.mkdir(parents=True)
    forge_tok.write_text("{}")
    prof = tmp_path / "secrets" / "profiles" / "keep.json"
    prof.parent.mkdir(parents=True)
    prof.write_text("{}")
    run = tmp_path / "state" / "runs" / "r1.json"
    run.parent.mkdir(parents=True)
    run.write_text("{}")

    live = config_mod.AppConfig()
    live_dts = {"doomed-dev": extra_dt}
    sb.apply_bundle(bundle, config=live, dev_types=live_dts,
                    reload=lambda: None)

    assert not (tmp_path / "config" / "dev_types" / "doomed-dev.yaml").exists()
    assert "doomed-dev" not in live_dts and "senior-dev" in live_dts
    assert not (tmp_path / "config" / "prompt_templates" / "REVIEW"
                / "doomed-template.yaml").exists()
    assert not (tmp_path / "secrets" / "connections" / "repo-doomed.json").exists()
    assert not (tmp_path / "secrets" / "harness" / "DOOMED_KEY.json").exists()
    for spared in (cred, forge_tok, prof, run):
        assert spared.exists()
    assert live.poll_interval_seconds == 45


def test_config_only_bundle_keeps_live_secrets(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=False)
    assert "secrets" not in bundle and bundle["sections"] == ["config"]
    live = config_mod.AppConfig()
    result = sb.apply_bundle(bundle, config=live, dev_types={},
                             reload=lambda: None)
    assert result["applied"] == ["config"]
    assert secrets.read_harness_secret("ANTHROPIC_API_KEY") == "sk-ant-secret-0003"


def test_orphan_secrets_are_skipped_with_warning_never_written(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["secrets"]["connections"]["repo-ghost"] = {
        "token": "ghost-token-value-4242"}
    live, live_dts = config_mod.AppConfig(), {}
    result = sb.apply_bundle(bundle, config=live, dev_types=live_dts,
                             reload=lambda: None)
    assert any("repo-ghost" in w for w in result["warnings"])
    assert not (tmp_path / "secrets" / "connections" / "repo-ghost.json").exists()


def test_dismissed_alerts_stripped_on_serialize_and_preserved_on_apply(
        monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    cfg.dismissed_alerts = ["gui-secrets-basic-auth:sig"]
    bundle = sb.serialize_current(cfg, dts, include_secrets=False)
    assert "dismissed_alerts" not in bundle["config"]["app"]
    live = config_mod.AppConfig(dismissed_alerts=["local-dismissal:sig2"])
    sb.apply_bundle(bundle, config=live, dev_types={}, reload=lambda: None)
    assert live.dismissed_alerts == ["local-dismissal:sig2"]


# ── stale refusals ───────────────────────────────────────────────────────────

def test_stale_bundle_version_refused_loudly(monkeypatch, tmp_path):
    sb, *_ = _env(monkeypatch, tmp_path)
    with pytest.raises(sb.BundleError) as e:
        sb.validate_bundle({"kind": sb.BUNDLE_KIND, "bundle_schema_version": 2})
    assert e.value.status == 422
    assert "not auto-migrated" in str(e.value)


def test_v3_env_shape_inside_bundle_gets_the_migration_string(monkeypatch, tmp_path):
    sb, *_ = _env(monkeypatch, tmp_path)
    bundle = {"kind": sb.BUNDLE_KIND, "bundle_schema_version": 1,
              "config": {"app": {"repos": [{"name": "main",
                                            "token_env": "GH_TOKEN"}]}}}
    with pytest.raises(sb.BundleError) as e:
        sb.validate_bundle(bundle)
    assert "v3 *_env" in str(e.value)


def test_wrong_kind_refused(monkeypatch, tmp_path):
    sb, *_ = _env(monkeypatch, tmp_path)
    with pytest.raises(sb.BundleError):
        sb.validate_bundle({"bundle_schema_version": 1})


# ── cross-refs + scrubbing ───────────────────────────────────────────────────

def test_cross_ref_violations_are_422(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=False)
    # assignment names a dev type the bundle does not carry
    bad = copy.deepcopy(bundle)
    bad["config"]["app"]["assignments"]["PLAN"]["dev_type"] = "missing-dev"
    with pytest.raises(sb.BundleError) as e:
        sb.validate_bundle(bad)
    assert "missing-dev" in str(e.value)
    # active template not in bundle ∪ builtins
    bad = copy.deepcopy(bundle)
    bad["config"]["app"]["active_prompt_templates"]["PLAN"] = "nowhere"
    with pytest.raises(sb.BundleError) as e:
        sb.validate_bundle(bad)
    assert "nowhere" in str(e.value)


def test_serialize_drops_orphan_active_devtype_prompts(monkeypatch, tmp_path):
    """Export/profile snapshots must not freeze orphan active_devtype_prompts
    keys for deleted Dev Types (would make re-apply 422 forever)."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    cfg.active_devtype_prompts = {
        "senior-dev": "Customer Success",
        "junior-dev": "Customer Success",   # not in dts
        "ghost": "Development",
    }
    bundle = sb.serialize_current(cfg, dts, include_secrets=False)
    active = bundle["config"]["app"]["active_devtype_prompts"]
    assert active == {"senior-dev": "Customer Success"}
    # live config is not mutated by serialize
    assert "junior-dev" in cfg.active_devtype_prompts


def test_orphan_active_devtype_prompts_pruned_not_422(monkeypatch, tmp_path):
    """Defense in depth: unknown active_devtype_prompts keys are dropped with
    a warning — never a hard 422 — so Workflow Switcher save / profile apply
    heal after a prior incomplete Dev Type delete."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=False)
    # inject an orphan the way a broken live config would export if serialize
    # had not filtered (hand-crafted / pre-fix snapshot)
    bundle["config"]["app"]["active_devtype_prompts"] = {
        "senior-dev": "Customer Success",
        "junior-dev": "Customer Success",
    }
    parsed = sb.validate_bundle(bundle)
    assert parsed["config"].active_devtype_prompts == {
        "senior-dev": "Customer Success"}
    assert any("junior-dev" in w for w in parsed["warnings"])

    live = config_mod.AppConfig()
    live_dts: dict = {}
    result = sb.apply_bundle(bundle, config=live, dev_types=live_dts,
                             reload=lambda: None)
    assert live.active_devtype_prompts == {"senior-dev": "Customer Success"}
    assert any("junior-dev" in w for w in result["warnings"])


def test_config_put_prunes_orphan_active_devtype_prompts_after_deep_merge(
        monkeypatch, tmp_path):
    """deep_merge cannot delete dict keys — PUT /config must still prune
    ghosts left behind when Dev Types were deleted."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    from devcake.api import config_service
    from devcake.config import AppConfig, DevType, Assignment

    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    (tmp_path / "config").mkdir(parents=True)

    cfg = AppConfig(
        assignments={mt: Assignment(dev_type="judgment")
                     for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")},
        active_devtype_prompts={
            "judgment": "Development",
            "junior-dev": "Customer Success",
        },
    )
    dts = {"judgment": DevType(name="judgment",
                               harness_template="claude-code")}
    # SPA only patches a live key; deep_merge would preserve junior-dev
    body = {"schema_version": 4,
            "active_devtype_prompts": {"judgment": "Customer Success"}}

    out = __import__("asyncio").new_event_loop().run_until_complete(
        config_service.apply_config_patch(
            body, config=cfg, dev_types=dts, managers={},
            reload=lambda: None))
    assert out["active_devtype_prompts"] == {
        "judgment": "Customer Success"}
    assert cfg.active_devtype_prompts == {"judgment": "Customer Success"}


def test_validation_errors_never_echo_secret_values(monkeypatch, tmp_path):
    """The leak vector (ADR-0013 hardening): pydantic embeds input values in
    its message; a malformed secrets section must not echo one."""
    sb, *_ = _env(monkeypatch, tmp_path)
    leaked = "sk-live-SUPER-SECRET-999999"
    bundle = {"kind": sb.BUNDLE_KIND, "bundle_schema_version": 1,
              "secrets": {"connections": {},
                          "harness": {"GOOD_KEY": leaked,
                                      "bad var!": leaked}}}
    with pytest.raises(sb.BundleError) as e:
        sb.validate_bundle(bundle)
    assert leaked not in str(e.value)
    # a config field carrying a secret-looking wrong-typed value
    bundle = {"kind": sb.BUNDLE_KIND, "bundle_schema_version": 1,
              "config": {"app": {"poll_interval_seconds": leaked}}}
    with pytest.raises(sb.BundleError) as e:
        sb.validate_bundle(bundle)
    assert leaked not in str(e.value)


# ── rollback ─────────────────────────────────────────────────────────────────

def test_reload_failure_rolls_back_to_exact_pre_state(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    live = config_mod.AppConfig()
    live_dts = dict(dts)
    for dt in dts.values():
        config_mod.save_dev_type(dt)
    config_mod.save_config(live)
    before = sb.serialize_current(live, live_dts, include_secrets=True)

    incoming = sb.serialize_current(cfg, dts, include_secrets=True)
    incoming["config"]["app"]["poll_interval_seconds"] = 77

    calls = {"n": 0}

    def failing_reload():
        calls["n"] += 1
        if calls["n"] == 1:            # the apply's reload fails...
            raise RuntimeError("adapter exploded")
        # ...the rollback's reload succeeds

    with pytest.raises(sb.BundleError) as e:
        sb.apply_bundle(incoming, config=live, dev_types=live_dts,
                        reload=failing_reload)
    assert e.value.status == 500
    assert "previous settings restored" in str(e.value)
    after = sb.serialize_current(live, live_dts, include_secrets=True)
    assert _comparable(after) == _comparable(before)
    assert live.poll_interval_seconds == 30    # never kept the bundle value


def test_deterministic_reload_failure_still_restores_files(monkeypatch, tmp_path):
    """The rollback's own reload failing must not abort the FILE restore —
    put_config's 'restore reload also failed' semantics."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    live, live_dts = config_mod.AppConfig(), {}
    before = sb.serialize_current(live, live_dts, include_secrets=True)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)

    def always_failing_reload():
        raise RuntimeError("always broken")

    with pytest.raises(sb.BundleError) as e:
        sb.apply_bundle(bundle, config=live, dev_types=live_dts,
                        reload=always_failing_reload)
    assert "previous settings restored" in str(e.value)
    after = sb.serialize_current(live, live_dts, include_secrets=True)
    assert _comparable(after) == _comparable(before)


# ── diff (preview) ───────────────────────────────────────────────────────────

def test_diff_reports_names_and_counts_never_values(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    live, live_dts = config_mod.AppConfig(), {}
    secrets.write_harness_secret("STALE_KEY", "stale-value-to-be-deleted-01")
    diff = sb.diff_bundle(bundle, live, live_dts)
    s = str(diff)
    for value in ("lin_api_secret_value_0001", "ghp_secret_value_0002",
                  "sk-ant-secret-0003", "stale-value-to-be-deleted-01"):
        assert value not in s
    sec = diff["sections"]["secrets"]
    # the live store in this test already holds the values (same data dir),
    # so they read as replaced, and the live-only key as removed
    assert "pmo-linear.api_key" in sec["connections"]["replaced"]
    assert "STALE_KEY" in sec["harness"]["removed"]
    assert "poll_interval_seconds" in diff["sections"]["config"]["app_changed"]


def test_diff_warns_when_live_secret_is_newer_than_snapshot(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["created_at"] = "2000-01-01T00:00:00+00:00"   # ancient snapshot
    diff = sb.diff_bundle(bundle, cfg, dts)
    assert any("updated after this snapshot" in w for w in diff["warnings"])


def test_diff_flags_intake_pause_change(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=False)
    live = config_mod.AppConfig(intake_paused=True)
    diff = sb.diff_bundle(bundle, live, {})
    assert any("UNPAUSED" in w for w in diff["warnings"])


# ── setup_env (section C plumbing; endpoints land in PR2) ───────────────────

def test_setup_env_serialization_and_insecure_flag_exclusion(monkeypatch, tmp_path):
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2-strong")
    monkeypatch.setenv("DEVCAKE_ALLOW_INSECURE", "1")
    cfg = config_mod.AppConfig()
    bundle = sb.serialize_current(cfg, {}, include_config=False,
                                  include_secrets=False,
                                  include_setup_env=True)
    values = bundle["setup_env"]["values"]
    assert values.get("ADMIN_PASSWORD") == "hunter2-strong"
    assert "DEVCAKE_ALLOW_INSECURE" not in values
    assert "DOCKER_GID" in bundle["setup_env"]["host_specific"]
    # ADR-0025: the workspace base is a host-absolute path — exported for
    # verification only, re-derived by up.sh on the target host
    assert "DEVCAKE_WS_HOST" in bundle["setup_env"]["host_specific"]
    parsed = sb.validate_bundle(bundle)
    assert parsed["setup_env"]["values"].get("ADMIN_PASSWORD") == "hunter2-strong"


def test_setup_env_import_drops_insecure_and_unknown_vars(monkeypatch, tmp_path):
    sb, *_ = _env(monkeypatch, tmp_path)
    bundle = {"kind": sb.BUNDLE_KIND, "bundle_schema_version": 1,
              "setup_env": {"values": {"DEVCAKE_ALLOW_INSECURE": "1",
                                       "TOTALLY_UNKNOWN": "x",
                                       "REDIS_PASSWORD": "r-pass"}}}
    parsed = sb.validate_bundle(bundle)
    assert parsed["setup_env"]["values"] == {"REDIS_PASSWORD": "r-pass"}
    assert any("DEVCAKE_ALLOW_INSECURE" in w for w in parsed["warnings"])
    assert any("TOTALLY_UNKNOWN" in w for w in parsed["warnings"])


def test_setup_env_vars_tripwire_matches_env_example():
    """SETUP_ENV_VARS mirrors .env.example — a var added to one without the
    other is a drift bug. Skips when the repo root isn't available (the
    app-test image copies only app/)."""
    root = Path(__file__).resolve().parents[2] / ".env.example"
    if not root.exists():
        pytest.skip(".env.example not in the test image")
    import re
    from devcake.settings_bundle import SETUP_ENV_VARS
    text = root.read_text()
    declared = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    declared -= {"DEVCAKE_ALLOW_INSECURE", "TAG"}
    ours = {name for name, _ in SETUP_ENV_VARS}
    assert ours == declared, (
        f"SETUP_ENV_VARS drift — missing {sorted(declared - ours)}, "
        f"extra {sorted(ours - declared)}")


# ── audit events ─────────────────────────────────────────────────────────────

def test_audit_event_appends_scrubbed_line(monkeypatch, tmp_path):
    sb, *_ = _env(monkeypatch, tmp_path)
    sb.audit_event("profile_saved", "name=staging secrets=3")
    line = (tmp_path / "state" / "events.jsonl").read_text().strip()
    import json
    rec = json.loads(line)
    assert rec["action"] == "profile_saved"
    assert rec["detail"] == "name=staging secrets=3"
    assert rec["pmo_id"] == "" and rec["instance"] == ""


# ── cross-store semantics: assignment refs (global + ADR-0019 overrides) ─────

def test_semantics_refuse_unknown_devtype_in_any_assignment_map(
        monkeypatch, tmp_path):
    """The cross-store assignment check runs UNCONDITIONALLY — one path for
    boot, PUT /config, and bundle apply (2026-08-12 audit SEC-3; the old
    check_assignments flag left the PUT blind). A config whose global map or
    instance override names a missing Dev Type must refuse, or the mission
    type silently never staffs (override) / KeyErrors at dispatch (global)."""
    sb, _p, secrets, config_mod, prompt_templates = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, prompt_templates)
    names = set(dts)
    exists = lambda mt, name: True

    sb.validate_config_semantics(cfg, names, exists)

    bad_global = copy.deepcopy(cfg)
    bad_global.assignments["EXECUTE"].dev_type = "ghost"
    with pytest.raises(sb.BundleError, match="ghost"):
        sb.validate_config_semantics(bad_global, names, exists)

    bad_override = copy.deepcopy(cfg)
    bad_override.pmos[0].assignments = {
        "EXECUTE": config_mod.Assignment(dev_type="ghost")}
    with pytest.raises(sb.BundleError, match="linear.*ghost|ghost.*linear"):
        sb.validate_config_semantics(bad_override, names, exists)
