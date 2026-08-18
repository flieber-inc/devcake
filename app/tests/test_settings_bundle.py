"""Settings bundle (ADR-0013): serialize/validate/diff/apply — replace-the-
world semantics, rollback-by-reapply, scrubbed validation errors (a malformed
secrets section must never echo a value), and the .env tripwire."""

import copy
import json
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

def test_serialize_never_carries_harness_receipts(monkeypatch, tmp_path):
    """Probe receipts live under /data/harness_receipts/ and stay off the
    settings bundle — they are keyed to this app digest, not portable config."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    (tmp_path / "harness_receipts").mkdir()
    (tmp_path / "harness_receipts" / "grok-build@0.2.112.json").write_text("{}")
    (tmp_path / "harness_bake_status.json").write_text('{"state":"baking"}')
    (tmp_path / "harness_baker.jsonl").write_text('{"event":"tick"}\n')
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    blob = str(bundle)
    assert "harness_receipts" not in bundle
    assert "harness_receipts" not in blob
    assert "harness_bake_status" not in blob
    assert "baking" not in blob
    assert "grok-build@0.2.112" not in blob


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
    keep = json.loads((dst / "harness_keep_set.json").read_text())
    assert "pins" in keep
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


# ── protect / unprotect envelope integrity (ADR-0013 B/C only) ───────────────

def test_protect_removes_plaintext_secret_sections(monkeypatch, tmp_path):
    """Encrypt-by-default must not leave live secrets/setup_env keys beside
    the protected envelope — the wire form is A + protected only."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pass-value-xyz")
    bundle = sb.serialize_current(cfg, dts, include_config=True,
                                  include_secrets=True, include_setup_env=True)
    assert "secrets" in bundle and "setup_env" in bundle
    out = sb.protect_bundle(bundle, "correct horse")
    assert "protected" in out
    assert "secrets" not in out
    assert "setup_env" not in out
    assert "plaintext_secrets" not in out
    assert "admin-pass-value-xyz" not in json.dumps(out)
    assert "lin_api_secret_value_0001" not in json.dumps(out)
    # section A still readable
    assert out["config"]["app"]["poll_interval_seconds"] == 45
    # round-trip restores B and C only
    back = sb.unprotect_bundle(out, "correct horse")
    assert "protected" not in back
    assert back["secrets"]["harness"]["ANTHROPIC_API_KEY"] == "sk-ant-secret-0003"
    assert back["setup_env"]["values"]["ADMIN_PASSWORD"] == "admin-pass-value-xyz"
    assert back["config"] == out["config"]


def test_unprotect_refuses_non_secret_keys_in_envelope(monkeypatch, tmp_path):
    """The protected payload is B/C only. A crafted ciphertext that also
    carries config (or kind/name) must not overwrite the operator-visible
    plaintext section A after passphrase entry."""
    sb, *_ = _env(monkeypatch, tmp_path)
    from devcake import settings_crypto
    visible = {"app": {"poll_interval_seconds": 30}, "operator_saw": True}
    mal_payload = {
        "secrets": {"connections": {}, "harness": {"K": "from-envelope"}},
        "config": {"app": {"poll_interval_seconds": 1}, "evil": True},
    }
    doc = {
        "kind": sb.BUNDLE_KIND,
        "bundle_schema_version": sb.BUNDLE_SCHEMA_VERSION,
        "config": visible,
        "protected": settings_crypto.encrypt_blob(
            "correct horse", json.dumps(mal_payload).encode()),
    }
    with pytest.raises(sb.BundleError) as e:
        sb.unprotect_bundle(doc, "correct horse")
    assert e.value.status == 422
    assert "secrets" in str(e.value).lower() or "setup_env" in str(e.value).lower()
    assert "evil" not in str(e.value)
    # honest payload still works
    good = {
        "kind": sb.BUNDLE_KIND,
        "bundle_schema_version": sb.BUNDLE_SCHEMA_VERSION,
        "config": visible,
        "protected": settings_crypto.encrypt_blob(
            "correct horse",
            json.dumps({"secrets": mal_payload["secrets"]}).encode()),
    }
    out = sb.unprotect_bundle(good, "correct horse")
    assert out["config"] == visible
    assert out["secrets"]["harness"]["K"] == "from-envelope"
    assert "protected" not in out


def test_unprotect_discards_outer_plaintext_secret_sections(monkeypatch, tmp_path):
    """An 'encrypted' YAML that also carries outer plaintext secrets/
    setup_env must not smuggle those values past unprotect — the envelope
    is the sole source of B/C when `protected` is present."""
    sb, *_ = _env(monkeypatch, tmp_path)
    from devcake import settings_crypto
    env_only = settings_crypto.encrypt_blob(
        "correct horse",
        json.dumps({"setup_env": {"values": {"ADMIN_PASSWORD": "from-env"}}}
                   ).encode())
    smuggled = "sk-smuggled-plaintext-SECRET-99"
    doc = {
        "kind": sb.BUNDLE_KIND,
        "bundle_schema_version": sb.BUNDLE_SCHEMA_VERSION,
        "config": {"app": {}},
        "secrets": {"connections": {}, "harness": {"ANTHROPIC_API_KEY": smuggled}},
        "plaintext_secrets": True,
        "protected": env_only,
    }
    out = sb.unprotect_bundle(doc, "correct horse")
    assert "secrets" not in out
    assert out["setup_env"]["values"]["ADMIN_PASSWORD"] == "from-env"
    assert "plaintext_secrets" not in out
    assert smuggled not in json.dumps(out)


def test_audit_event_redacts_known_secret_values(monkeypatch, tmp_path):
    """Belt-and-braces: if a caller ever put a stored secret into detail,
    the audit line must not keep the live value."""
    sb, _, secrets, *_ = _env(monkeypatch, tmp_path)
    secret = "lin_audit_scrub_secret_ABCDEF"
    secrets.write_connection_secret("pmo", "linear", "api_key", secret)
    sb.audit_event("settings_exported",
                   f"sections=secrets note={secret}")
    line = (tmp_path / "state" / "events.jsonl").read_text().strip()
    assert secret not in line
    rec = json.loads(line)
    assert rec["action"] == "settings_exported"
    assert secret not in rec["detail"]


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


# ── apply ordering: crash honesty (2026-08-12 audit SEC-1) ───────────────────


def _journaled(monkeypatch, sb, secrets, *, tpl=None):
    """Wrap the commit point + every secret-store / config-file mutation to
    append to one shared journal, preserving behavior."""
    journal: list[str] = []

    def wrap(mod, name, tag):
        real = getattr(mod, name)

        def logged(*a, **k):
            journal.append(tag(*a, **k))
            return real(*a, **k)
        monkeypatch.setattr(mod, name, logged)

    wrap(sb, "save_config", lambda c: "COMMIT")
    wrap(secrets, "write_connection_secret",
         lambda s, i, f, v: f"conn_write:{s}-{i}.{f}")
    wrap(secrets, "delete_connection_field",
         lambda s, i, f: f"conn_del_field:{s}-{i}.{f}")
    wrap(secrets, "delete_connection_instance",
         lambda s, i: f"conn_del_inst:{s}-{i}")
    wrap(secrets, "write_harness_secret", lambda v, val: f"harness_write:{v}")
    wrap(secrets, "delete_harness_secret", lambda v: f"harness_del:{v}")
    wrap(sb, "save_dev_type", lambda dt: f"dev_type_write:{dt.name}")
    if tpl is not None:
        wrap(tpl, "save_template",
             lambda mt, name, text: f"tpl_write:{mt}/{name}")
    return journal


def test_apply_orders_adds_then_commit_then_destructive(monkeypatch, tmp_path):
    """Phase order IS the crash-safety property: keys the old world never
    stored land before the config.yaml commit; every overwrite and deletion
    lands after it."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    # bundle world: replaces the pmo api_key (overwrite), drops repo main's
    # token (instance delete via config w/o that repo? keep repo; field del),
    # adds a NEW harness key, drops the old one
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["secrets"]["connections"]["pmo-linear"]["api_key"] = "rotated-key-1"
    del bundle["secrets"]["connections"]["repo-main"]
    bundle["secrets"]["harness"] = {"NEW_KEY": "brand-new-value-42"}

    # existing config files must already be on disk so apply can tell
    # ADD (new name) from MUT (overwrite)
    for dt in dts.values():
        config_mod.save_dev_type(dt)

    journal = _journaled(monkeypatch, sb, secrets, tpl=tpl)
    live_dts = dict(dts)
    sb.apply_bundle(bundle, config=cfg, dev_types=live_dts,
                    reload=lambda: None)

    assert "COMMIT" in journal
    commit_at = journal.index("COMMIT")
    adds = [j for j in journal if j == "harness_write:NEW_KEY"]
    assert adds and all(journal.index(a) < commit_at for a in adds), (
        "new keys must land before the commit point")
    destructive = [j for j in journal if j.startswith(
        ("conn_del", "harness_del")) or j == "conn_write:pmo-linear.api_key"]
    assert destructive, "expected overwrites + deletions in the plan"
    assert all(journal.index(d, commit_at) > commit_at for d in destructive), (
        "every overwrite/deletion must land after the commit point")
    overwrites = [j for j in journal if j.startswith("dev_type_write:")
                  or j.startswith("tpl_write:")]
    assert overwrites, "expected existing config-file overwrites in the plan"
    assert all(journal.index(w) > commit_at for w in overwrites), (
        "overwriting an existing dev-type/template must wait until after "
        "the config.yaml commit")
    # end state converged
    assert secrets.read_connection_secret("pmo", "linear", "api_key") == "rotated-key-1"
    assert secrets.read_connection_secret("repo", "main", "token") == ""
    assert secrets.read_harness_secret("NEW_KEY") == "brand-new-value-42"
    assert secrets.read_harness_secret("ANTHROPIC_API_KEY") == ""


def test_commit_failure_leaves_secret_store_untouched(monkeypatch, tmp_path):
    """New property of the reorder: if save_config raises, the store still
    holds every OLD value — including the instance the bundle would have
    deleted and the value it would have overwritten (rollback then prunes
    the inert ADD-phase extras)."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["secrets"]["connections"]["pmo-linear"]["api_key"] = "rotated-key-1"
    del bundle["secrets"]["connections"]["repo-main"]
    bundle["secrets"]["harness"]["NEW_KEY"] = "brand-new-value-42"

    real_save = sb.save_config
    calls = {"n": 0}

    def boom_once(cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full at the commit point")
        return real_save(cfg)          # the rollback's own commit succeeds

    monkeypatch.setattr(sb, "save_config", boom_once)
    live_dts = dict(dts)
    with pytest.raises(sb.BundleError, match="previous settings restored"):
        sb.apply_bundle(bundle, config=cfg, dev_types=live_dts,
                        reload=lambda: None)
    # old values intact — never modified pre-commit
    assert (secrets.read_connection_secret("pmo", "linear", "api_key")
            == "lin_api_secret_value_0001")
    assert (secrets.read_connection_secret("repo", "main", "token")
            == "ghp_secret_value_0002")
    assert secrets.read_harness_secret("ANTHROPIC_API_KEY") == "sk-ant-secret-0003"
    # and the rollback pruned the inert ADD-phase extra
    assert secrets.read_harness_secret("NEW_KEY") == ""


def test_destructive_phase_failure_rolls_back_overwrites(monkeypatch, tmp_path):
    """A failure INSIDE phase MUT (post-commit) restores both config and the
    already-overwritten secret values via rollback-by-reapply."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["secrets"]["connections"]["pmo-linear"]["api_key"] = "rotated-key-1"
    del bundle["secrets"]["connections"]["repo-main"]   # → instance delete

    def boom(scope, instance):
        raise RuntimeError("store failure mid-deletes")

    monkeypatch.setattr(secrets, "delete_connection_instance", boom)
    monkeypatch.setattr(sb.secrets_store, "delete_connection_instance", boom)
    live_dts = dict(dts)
    with pytest.raises(sb.BundleError, match="previous settings restored"):
        sb.apply_bundle(bundle, config=cfg, dev_types=live_dts,
                        reload=lambda: None)
    # the overwrite that already landed was rolled back to the old value
    assert (secrets.read_connection_secret("pmo", "linear", "api_key")
            == "lin_api_secret_value_0001")
    assert (secrets.read_connection_secret("repo", "main", "token")
            == "ghp_secret_value_0002")


def test_secrets_only_apply_orders_adds_before_destructive(monkeypatch, tmp_path):
    """No config section → no config.yaml commit; the declared commit point
    is the ADD→MUT boundary and the order must still hold."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_config=False,
                                  include_secrets=True)
    bundle["secrets"]["connections"]["pmo-linear"]["api_key"] = "rotated-key-2"
    bundle["secrets"]["harness"] = {"NEW_ONLY": "value-brand-new"}

    journal = _journaled(monkeypatch, sb, secrets)
    result = sb.apply_bundle(bundle, config=cfg, dev_types=dict(dts),
                             reload=lambda: None)
    assert result["applied"] == ["secrets"]
    assert "COMMIT" not in journal
    first_mut = min(i for i, j in enumerate(journal)
                    if j.startswith(("conn_del", "harness_del"))
                    or j == "conn_write:pmo-linear.api_key")
    adds = [i for i, j in enumerate(journal) if j == "harness_write:NEW_ONLY"]
    assert adds and max(adds) < first_mut


# ── credential files: the one-way door closed (2026-08-12 audit SEC-2) ──────


def test_credential_files_export_apply_round_trip(monkeypatch, tmp_path):
    """Host migration as advertised: exported credential files ARRIVE on the
    target — written 0600 through the one writer seam — and the group is
    replace-the-world (an unlisted host file is deleted)."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path / "src")
    cfg, dts = _world(config_mod, secrets, tpl)
    secrets.write_credential_file("senior-dev", "grok-auth.json",
                                  '{"tok": "oauth-secret-abc123"}')
    bundle = sb.serialize_current(cfg, dts, include_secrets=True,
                                  include_credential_files=True)
    assert "credential_files" in bundle["secrets"]

    dst = tmp_path / "dst"
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(dst))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        dst / "config" / "config.yaml")
    # a stale credential on the target that the bundle does not list
    secrets.write_credential_file("senior-dev", "stale-auth.json", "old")
    result = sb.apply_bundle(bundle, config=config_mod.AppConfig(),
                             dev_types={}, reload=lambda: None)
    target = dst / "secrets" / "senior-dev" / "grok-auth.json"
    assert target.read_text() == '{"tok": "oauth-secret-abc123"}'
    assert target.stat().st_mode & 0o777 == 0o600
    assert not (dst / "secrets" / "senior-dev" / "stale-auth.json").exists()
    assert "dev-type credential files" not in result["untouched"]


def test_credential_files_absent_key_leaves_files_untouched(monkeypatch, tmp_path):
    """No `credential_files` key (every pre-SEC-2 profile/bundle) → the group
    is untouched: an ordinary profile apply must never wipe OAuth creds."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    secrets.write_credential_file("senior-dev", "grok-auth.json", "keep-me")
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)   # no creds
    result = sb.apply_bundle(bundle, config=cfg, dev_types=dict(dts),
                             reload=lambda: None)
    assert (tmp_path / "secrets" / "senior-dev" / "grok-auth.json"
            ).read_text() == "keep-me"
    assert "dev-type credential files" in result["untouched"]


def test_credential_files_validation_refusals(monkeypatch, tmp_path):
    import base64
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)

    def bundle_with(dev, entry):
        b = sb.serialize_current(cfg, dts, include_secrets=True)
        b["secrets"]["credential_files"] = {dev: [entry]}
        return b

    ok = base64.b64encode(b"x").decode()
    with pytest.raises(sb.BundleError, match="invalid credential filename"):
        sb.validate_bundle(bundle_with(
            "senior-dev", {"filename": "../../etc/passwd", "content_b64": ok}))
    with pytest.raises(sb.BundleError, match="invalid credential dev_type"):
        sb.validate_bundle(bundle_with(
            "connections", {"filename": "a.json", "content_b64": ok}))
    with pytest.raises(sb.BundleError, match="UTF-8"):
        sb.validate_bundle(bundle_with(
            "senior-dev", {"filename": "a.json",
                           "content_b64": base64.b64encode(b"\xff\xfe").decode()}))
    big = base64.b64encode(b"x" * (secrets.MAX_CREDENTIAL_FILE_BYTES + 1)).decode()
    with pytest.raises(sb.BundleError, match="exceeds"):
        sb.validate_bundle(bundle_with(
            "senior-dev", {"filename": "a.json", "content_b64": big}))


def test_credential_files_unknown_dev_type_skipped_with_warning(
        monkeypatch, tmp_path):
    import base64
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["secrets"]["credential_files"] = {
        "ghost-dev": [{"filename": "a.json",
                       "content_b64": base64.b64encode(b"v").decode()}]}
    result = sb.apply_bundle(bundle, config=cfg, dev_types=dict(dts),
                             reload=lambda: None)
    assert any("ghost-dev" in w and "skipped" in w for w in result["warnings"])
    assert not (tmp_path / "secrets" / "ghost-dev").exists()


def test_rollback_restores_overwritten_credential_file(monkeypatch, tmp_path):
    """The rollback snapshot must carry the group when the bundle touches it
    — without the include_credential_files flag on `previous`, a failed
    apply that already overwrote grok-auth.json could not restore it."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    secrets.write_credential_file("senior-dev", "grok-auth.json", "NEWER-oauth")
    bundle = sb.serialize_current(cfg, dts, include_secrets=True,
                                  include_credential_files=True)
    # bundle captured, then simulate the export being stale vs a re-OAuth
    import base64 as b64mod
    bundle["secrets"]["credential_files"]["senior-dev"] = [
        {"filename": "grok-auth.json",
         "content_b64": b64mod.b64encode(b"OLDER-oauth").decode()}]

    calls = {"n": 0}

    def failing_reload():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("adapter reload blew up")

    with pytest.raises(sb.BundleError, match="previous settings restored"):
        sb.apply_bundle(bundle, config=cfg, dev_types=dict(dts),
                        reload=failing_reload)
    assert (tmp_path / "secrets" / "senior-dev" / "grok-auth.json"
            ).read_text() == "NEWER-oauth"


def test_diff_previews_credential_file_names_only(monkeypatch, tmp_path):
    import base64
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    secrets.write_credential_file("senior-dev", "replaced.json", "current")
    secrets.write_credential_file("senior-dev", "removed.json", "current")
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    bundle["secrets"]["credential_files"] = {
        "senior-dev": [
            {"filename": "replaced.json",
             "content_b64": base64.b64encode(b"incoming").decode()},
            {"filename": "added.json",
             "content_b64": base64.b64encode(b"incoming").decode()},
        ]}
    out = sb.diff_bundle(bundle, cfg, dts)
    delta = out["sections"]["secrets"]["credential_files"]
    assert delta == {"added": ["senior-dev/added.json"],
                     "replaced": ["senior-dev/replaced.json"],
                     "removed": ["senior-dev/removed.json"]}
    blob = str(out)
    assert "incoming" not in blob and "current" not in blob


def test_wrong_type_json_secret_reads_as_absent_not_attribute_error(
        monkeypatch, tmp_path):
    """SEC-10 (2026-08-12 audit): a secret file whose JSON parses to a
    list/string escaped the lenient-read except and AttributeError'd at the
    caller (inside the poll cycle via PMOInstance.api_key). Lenient reads
    treat it as absent; strict reads refuse read-modify-write."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    p = tmp_path / "secrets" / "connections" / "pmo-linear.json"
    p.parent.mkdir(parents=True)
    p.write_text('["not", "an", "object"]')
    assert secrets.read_connection_secret("pmo", "linear", "api_key") == ""
    assert secrets.connection_status("pmo", "linear", "api_key") == {
        "present": False, "updated_at": None}
    with pytest.raises(ValueError, match="refusing read-modify-write"):
        secrets.write_connection_secret("pmo", "linear", "api_key", "v")


# ── skill-source connection secrets (CAKE-65) ────────────────────────────────

def test_serialize_includes_skill_source_connection_secrets(monkeypatch, tmp_path):
    """Skill-source tokens ride user-facing snapshots with the skill card —
    same live-key rule as pmo/repo (tutorial 02; CONNECTION_FIELDS already
    allowlists skill). Orphans without a card stay excluded; rollback keeps
    them when include_orphan_secrets=True."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, dts = _world(config_mod, secrets, tpl)
    cfg.skill_sources = [
        config_mod.SkillSource(
            name="shelf", url="https://github.com/acme/skills"),
    ]
    secrets.write_connection_secret("skill", "shelf", "token_ro",
                                    "skill_ro_secret_value_0042")
    secrets.write_connection_secret("skill", "ghost", "token_ro",
                                    "skill_orphan_ghost_0043")
    facing = sb.serialize_current(cfg, dts, include_secrets=True)
    conns = facing["secrets"]["connections"]
    assert conns["skill-shelf"] == {"token_ro": "skill_ro_secret_value_0042"}
    assert "skill-ghost" not in conns
    assert "pmo-linear" in conns and "repo-main" in conns
    rollback = sb.serialize_current(cfg, dts, include_secrets=True,
                                    include_orphan_secrets=True)
    assert rollback["secrets"]["connections"]["skill-ghost"] == {
        "token_ro": "skill_orphan_ghost_0043"}


def test_serialize_apply_roundtrip_keeps_skill_source_secrets(monkeypatch,
                                                              tmp_path):
    """Operator-visible contract: a secrets-bearing snapshot of a world with
    a skill source restores those tokens on apply (and does not wipe them as
    'extra' unknowns)."""
    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path / "src")
    cfg, dts = _world(config_mod, secrets, tpl)
    cfg.skill_sources = [
        config_mod.SkillSource(
            name="shelf", url="https://github.com/acme/skills"),
    ]
    secrets.write_connection_secret("skill", "shelf", "token_ro",
                                    "skill_ro_roundtrip_0050")
    bundle = sb.serialize_current(cfg, dts, include_secrets=True)
    assert "skill-shelf" in bundle["secrets"]["connections"]

    dst = tmp_path / "dst"
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(dst))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        dst / "config" / "config.yaml")
    cfg2, dts2 = config_mod.AppConfig(), {}
    # seed a live skill token that must survive replace-the-world only when
    # the bundle carries it — and must not be deleted as an unknown key
    secrets.write_connection_secret("skill", "shelf", "token_ro",
                                    "preexisting-should-be-overwritten")
    result = sb.apply_bundle(bundle, config=cfg2, dev_types=dts2,
                             reload=lambda: None)
    assert "secrets" in result["applied"]
    assert secrets.read_connection_secret("skill", "shelf", "token_ro") \
        == "skill_ro_roundtrip_0050"
    assert any(s.name == "shelf" for s in cfg2.skill_sources)
    again = sb.serialize_current(cfg2, dts2, include_secrets=True)
    assert again["secrets"]["connections"]["skill-shelf"] == {
        "token_ro": "skill_ro_roundtrip_0050"}


def test_config_put_deletes_removed_skill_source_secrets(monkeypatch, tmp_path):
    """Removing a skill_sources card deletes skill-{name} secrets — same
    best-effort contract as pmo/repo instance removal."""
    import asyncio

    from devcake.api import config_service

    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, _dts = _world(config_mod, secrets, tpl)
    cfg.skill_sources = [
        config_mod.SkillSource(
            name="shelf", url="https://github.com/acme/skills"),
    ]
    secrets.write_connection_secret("skill", "shelf", "token_ro",
                                    "skill_ro_to_delete_0060")
    assert (tmp_path / "secrets" / "connections" / "skill-shelf.json").exists()

    monkeypatch.setattr(config_service, "save_config", lambda c: None)
    monkeypatch.setattr(config_service, "validate_config_semantics",
                        lambda *a, **k: None)
    monkeypatch.setattr(config_service, "dry_run_adapters", lambda *a, **k: None)

    body = {"skill_sources": []}
    asyncio.new_event_loop().run_until_complete(
        config_service.apply_config_patch(
            body, config=cfg, dev_types={}, managers={}, reload=lambda: None))
    assert cfg.skill_sources == []
    assert not (tmp_path / "secrets" / "connections" / "skill-shelf.json").exists()
    assert secrets.read_connection_secret("skill", "shelf", "token_ro") == ""


def test_config_put_renames_skill_source_moves_secrets(monkeypatch, tmp_path):
    """In-place rename (same card index, new name) moves the connection
    secret file — tokens follow the new name; no orphan under the old."""
    import asyncio

    from devcake.api import config_service

    sb, _, secrets, config_mod, tpl = _env(monkeypatch, tmp_path)
    cfg, _dts = _world(config_mod, secrets, tpl)
    cfg.skill_sources = [
        config_mod.SkillSource(
            name="shelf", url="https://github.com/acme/skills"),
    ]
    secrets.write_connection_secret("skill", "shelf", "token_ro",
                                    "skill_ro_rename_0061")
    secrets.write_connection_secret("skill", "shelf", "token",
                                    "skill_rw_rename_0062")

    monkeypatch.setattr(config_service, "save_config", lambda c: None)
    monkeypatch.setattr(config_service, "validate_config_semantics",
                        lambda *a, **k: None)
    monkeypatch.setattr(config_service, "dry_run_adapters", lambda *a, **k: None)

    body = {
        "skill_sources": [
            {"name": "bookshelf", "forge": "github",
             "url": "https://github.com/acme/skills",
             "default_branch": "", "subdir": ""},
        ],
    }
    asyncio.new_event_loop().run_until_complete(
        config_service.apply_config_patch(
            body, config=cfg, dev_types={}, managers={}, reload=lambda: None))
    assert [s.name for s in cfg.skill_sources] == ["bookshelf"]
    assert secrets.read_connection_secret("skill", "bookshelf", "token_ro") \
        == "skill_ro_rename_0061"
    assert secrets.read_connection_secret("skill", "bookshelf", "token") \
        == "skill_rw_rename_0062"
    assert not (tmp_path / "secrets" / "connections" / "skill-shelf.json").exists()
    assert secrets.read_connection_secret("skill", "shelf", "token_ro") == ""
