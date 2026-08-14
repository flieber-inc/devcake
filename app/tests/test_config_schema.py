"""Config schema v4 (instances-with-identities, GUI-stored secrets): name-keyed pmos:/repos:
lists, N-pmo validators, the generic stale-schema refusal (v1 singular keys,
v2 id-keyed entries, old schema_version) — auto-migrations were removed at v0
crystallization; there are no deployments to migrate."""

import yaml
import pytest

import devcake.config as config_mod
from devcake.config import (AppConfig, DevType, PMOInstance, RepoInstance,
                            reject_stale_patch)
from devcake.domain.orchestrator import dispatch

V1_YAML = """
schema_version: 1
pmo:
  system: linear
  api_key_env: LINEAR_API_KEY
  team_key: DEV
repo:
  forge: github
  url: https://github.com/x/y
  token_env: GITHUB_TOKEN
adoption_mode: opt_in
"""


def _base():
    return AppConfig(pmos=[PMOInstance(name="linear", team_key="DEV")],
                     repos=[RepoInstance(name="main",
                                         url="https://github.com/o/r")]).model_dump()


def test_instance_validators_v3():
    base = _base()
    # N pmos allowed — unique names, distinct configured targets
    two = dict(base, pmos=[dict(base["pmos"][0], team_key="A"),
                           dict(base["pmos"][0], name="linearb", team_key="B")])
    AppConfig.model_validate(two)
    with pytest.raises(Exception, match="duplicate instance names"):
        AppConfig.model_validate(dict(base, pmos=[
            dict(base["pmos"][0]), dict(base["pmos"][0])]))
    with pytest.raises(Exception, match="double-dispatch"):
        AppConfig.model_validate(dict(base, pmos=[
            dict(base["pmos"][0], team_key="A"),
            dict(base["pmos"][0], name="linearb", team_key="A")]))
    AppConfig.model_validate(dict(base, pmos=[]))   # 0..N since M12
    # repos: 0..N since M10 — empty is the zero-repo gate, dupes refused
    AppConfig.model_validate(dict(base, repos=[]))
    dupes = dict(base, repos=[base["repos"][0], dict(base["repos"][0])])
    with pytest.raises(Exception, match="duplicate"):
        AppConfig.model_validate(dupes)
    two = dict(base, repos=[dict(base["repos"][0], url="https://h/a/r"),
                            dict(base["repos"][0], name="second",
                                 url="https://h/b/r")])
    AppConfig.model_validate(two)


def test_instance_name_format_enforced():
    """Names embed uppercased in branch/run-id compounds ({INSTANCE}-{key}) —
    lowercase alnum only, no hyphens (ambiguity), ≤12 chars (run-id budget)."""
    for bad in ("linear-a", "Linear", "9lead", "x" * 13, ""):
        with pytest.raises(Exception):
            PMOInstance(name=bad)
    PMOInstance(name="linearb")


def test_reserved_pmo_instance_names_rejected():
    """Audit A15: 'main' marks legacy pre-v3 run records (pmo_ref default) —
    a live instance named main would adopt every legacy record directly and
    count them as its own in the in-flight guard; 'sys' is the HELLO/OAUTH
    pseudo-instance in run ids. Repo names stay free ('main' is the repo
    default and harmless — repos never prefix run ids or pmo_refs)."""
    for reserved in ("main", "sys"):
        with pytest.raises(Exception, match="reserved"):
            AppConfig(pmos=[PMOInstance(name=reserved, team_key="DEV")])
    AppConfig(repos=[RepoInstance(name="main",
                                  url="https://github.com/o/r")])   # still fine


def test_pmo_repo_set_members_must_exist():
    base = _base()
    base["pmos"][0]["repos"] = ["nosuchrepo"]
    with pytest.raises(Exception, match="name no configured repo"):
        AppConfig.model_validate(base)
    base["pmos"][0]["repos"] = [base["repos"][0]["name"],
                                base["repos"][0]["name"]]
    with pytest.raises(Exception, match="duplicate"):
        AppConfig.model_validate(base)
    base["pmos"][0]["repos"] = [base["repos"][0]["name"]]
    AppConfig.model_validate(base)


def test_pre_repo_set_default_repo_shape_refused():
    """Item 2: singular default_repo became the ordered repo set — stale
    bodies/files get the hand-migration hint, never a silent drop."""
    with pytest.raises(ValueError, match="default_repo"):
        reject_stale_patch({"pmos": [{"name": "linear",
                                      "default_repo": "main"}]})


def test_unknown_pmo_system_rejected():
    base = _base()
    base["pmos"][0]["system"] = "jira"          # not in the adapter registry
    with pytest.raises(Exception, match="unknown PMO system"):
        AppConfig.model_validate(base)


def test_operational_fields_reject_zero_and_negative():
    """ISSUES #8/#9: zero/negative operational values must not validate."""
    base = _base()
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "poll_interval_seconds": 0})
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "dev_timeout_minutes": -1})
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "max_attempts": 0})
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "review_loop_warning_every": 0})
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "concurrency": {"global_max": 0}})


def test_max_decomposition_depth_defaults_and_bounds():
    """Decomposition depth limit is operator policy (Traffic control,
    ADR-0012): default 2, 0 = unlimited, negatives refused."""
    base = _base()
    assert AppConfig.model_validate(base).max_decomposition_depth == 2
    for ok in (0, 1, 2):
        got = AppConfig.model_validate(
            {**base, "max_decomposition_depth": ok})
        assert got.max_decomposition_depth == ok
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "max_decomposition_depth": -1})
    # a patch touching an unrelated field must not reset the setting
    from devcake.config import deep_merge
    tuned = {**base, "max_decomposition_depth": 1}
    merged = deep_merge(tuned, {"repos": [
        {**base["repos"][0], "auto_merge": True}]})
    assert AppConfig.model_validate(merged).max_decomposition_depth == 1


def test_discovery_routing_defaults_on_and_round_trips():
    """ADR-0033 D11 (draft field): per-instance gate on the routing lane,
    default ON, plain bool riding the normal draft/save path."""
    base = _base()
    assert AppConfig.model_validate(base).pmos[0].discovery_routing is True
    tuned = {**base, "pmos": [dict(base["pmos"][0],
                                   discovery_routing=False)]}
    got = AppConfig.model_validate(tuned)
    assert got.pmos[0].discovery_routing is False
    redumped = AppConfig.model_validate(got.model_dump())
    assert redumped.pmos[0].discovery_routing is False


def test_budgets_defaults_bounds_and_round_trip():
    """ADR-0033 D7 as amended (founder rulings 2026-08-13): counting
    budgets are operator knobs — defaults 5/3, 0 = unlimited, negatives
    refused, the block rides model_dump round-trips (settings bundle for
    free) — and routing deliberately has NO numeric budget (addendum 14):
    a stored bundle carrying the deleted knobs still validates (ignored)."""
    base = _base()
    b = AppConfig.model_validate(base).budgets
    assert (b.freshness_rereviews, b.discoveries_per_run) == (5, 3)
    assert not hasattr(b, "discovery_routes_per_source")
    assert not hasattr(b, "discovery_in_per_recipient")
    got = AppConfig.model_validate(
        {**base, "budgets": {"freshness_rereviews": 0,
                             "discoveries_per_run": 9,
                             "discovery_in_per_recipient": 5}})
    assert got.budgets.freshness_rereviews == 0        # 0 = unlimited
    assert got.budgets.discoveries_per_run == 9
    with pytest.raises(Exception):
        AppConfig.model_validate(
            {**base, "budgets": {"freshness_rereviews": -1}})
    dumped = got.model_dump()
    assert AppConfig.model_validate(dumped).budgets == got.budgets


def test_recover_misplaced_result_defaults_on_and_round_trips():
    """ADR-0018: misplaced-result recovery is operator policy (Limits) —
    default ON, and the field is additive: a config file written before it
    existed still validates at schema v4 with no migration."""
    base = _base()
    assert "recover_misplaced_result" in base          # dumped for the SPA draft
    assert AppConfig().recover_misplaced_result is True
    assert AppConfig.model_validate(base).recover_misplaced_result is True
    del base["recover_misplaced_result"]              # pre-ADR-0018 config JSON
    loaded = AppConfig.model_validate(base)
    assert loaded.recover_misplaced_result is True
    assert loaded.schema_version == 4                 # additive, not a migration
    off = AppConfig.model_validate({**base, "recover_misplaced_result": False})
    assert off.recover_misplaced_result is False
    round_tripped = AppConfig.model_validate(off.model_dump())
    assert round_tripped.recover_misplaced_result is False
    assert round_tripped.schema_version == 4
    # a patch touching an unrelated field must not reset the setting
    from devcake.config import deep_merge
    dumped = off.model_dump()
    merged = deep_merge(dumped, {"repos": [
        {**(dumped["repos"][0] if dumped["repos"] else
            {"name": "main", "url": "https://github.com/o/r"}),
         "auto_merge": True}]})
    assert AppConfig.model_validate(merged).recover_misplaced_result is False


def test_continuation_fields_default_and_round_trip():
    """ADR-0022: in-container continuation is operator policy (Limits) —
    policy `auto`, budget 2, both additive: a pre-ADR-0022 config file still
    validates at schema v4 with no migration."""
    base = _base()
    assert "continuation_policy" in base               # dumped for the SPA draft
    assert "max_continuations" in base
    assert AppConfig().continuation_policy == "auto"
    assert AppConfig().max_continuations == 2
    del base["continuation_policy"]                    # pre-ADR-0022 config JSON
    del base["max_continuations"]
    loaded = AppConfig.model_validate(base)
    assert loaded.continuation_policy == "auto"
    assert loaded.max_continuations == 2
    assert loaded.schema_version == 4                  # additive, not a migration
    tuned = AppConfig.model_validate(
        {**base, "continuation_policy": "fresh-only", "max_continuations": 50})
    round_tripped = AppConfig.model_validate(tuned.model_dump())
    assert round_tripped.continuation_policy == "fresh-only"
    assert round_tripped.max_continuations == 50
    # a patch touching an unrelated field must not reset the settings
    from devcake.config import deep_merge
    dumped = tuned.model_dump()
    merged = deep_merge(dumped, {"repos": [
        {**dumped["repos"][0], "auto_merge": True}]})
    assert AppConfig.model_validate(merged).max_continuations == 50


def test_attempt_reset_and_brake_fields_default_and_round_trip():
    """ADR-0026: spend discipline is operator policy (Limits & Traffic) —
    strict `label-ops` default, brake widening OFF, both additive: a
    pre-ADR-0026 config file still validates at schema v4 with no migration."""
    base = _base()
    assert "attempt_reset" in base                     # dumped for the SPA draft
    assert "brake_on_bad_output" in base
    assert AppConfig().attempt_reset == "label-ops"
    assert AppConfig().brake_on_bad_output is False
    del base["attempt_reset"]                          # pre-ADR-0026 config JSON
    del base["brake_on_bad_output"]
    loaded = AppConfig.model_validate(base)
    assert loaded.attempt_reset == "label-ops"
    assert loaded.brake_on_bad_output is False
    assert loaded.schema_version == 4                  # additive, not a migration
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "attempt_reset": "sometimes"})
    tuned = AppConfig.model_validate(
        {**base, "attempt_reset": "unlimited", "brake_on_bad_output": True})
    round_tripped = AppConfig.model_validate(tuned.model_dump())
    assert round_tripped.attempt_reset == "unlimited"
    assert round_tripped.brake_on_bad_output is True
    # a patch touching an unrelated field must not reset the settings
    from devcake.config import deep_merge
    dumped = tuned.model_dump()
    merged = deep_merge(dumped, {"repos": [
        {**(dumped["repos"][0] if dumped["repos"] else
            {"name": "main", "url": "https://github.com/o/r"}),
         "auto_merge": True}]})
    assert AppConfig.model_validate(merged).attempt_reset == "unlimited"


def test_repo_mirror_defaults_and_round_trip():
    """ADR-0024: the mirror is MANDATORY (no enable field exists — its
    absence IS the contract); the two knobs default to sync-every-dispatch
    and LFS off, additive at schema v4 with no migration."""
    base = _base()
    assert "repo_mirror" in base                       # dumped for the SPA draft
    assert "enabled" not in base["repo_mirror"]        # no off switch, by design
    assert AppConfig().repo_mirror.sync_max_age_seconds == 0
    assert AppConfig().repo_mirror.lfs is False
    del base["repo_mirror"]                            # pre-ADR-0024 config JSON
    loaded = AppConfig.model_validate(base)
    assert loaded.repo_mirror.sync_max_age_seconds == 0
    assert loaded.schema_version == 4                  # additive, not a migration
    tuned = AppConfig.model_validate(
        {**base, "repo_mirror": {"sync_max_age_seconds": 300, "lfs": True}})
    round_tripped = AppConfig.model_validate(tuned.model_dump())
    assert round_tripped.repo_mirror.sync_max_age_seconds == 300
    assert round_tripped.repo_mirror.lfs is True
    with pytest.raises(Exception):
        AppConfig.model_validate(
            {**base, "repo_mirror": {"sync_max_age_seconds": -1}})
    # a nested partial patch must not reset the sibling knob
    from devcake.config import deep_merge
    merged = deep_merge(tuned.model_dump(), {"repo_mirror": {"lfs": False}})
    got = AppConfig.model_validate(merged)
    assert got.repo_mirror.lfs is False
    assert got.repo_mirror.sync_max_age_seconds == 300


def test_continuation_fields_bounds():
    """Budget: 0 (off) and LARGE values are both legal — founder decision
    2026-08-02: 10/50-continuation experiments must validate; deliberately no
    upper bound, unlike max_attempts. Negatives and unknown policies refused."""
    base = _base()
    for ok in (0, 1, 2, 10, 50, 500):
        assert AppConfig.model_validate(
            {**base, "max_continuations": ok}).max_continuations == ok
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "max_continuations": -1})
    for ok in ("auto", "resume-only", "fresh-only", "off"):
        assert AppConfig.model_validate(
            {**base, "continuation_policy": ok}).continuation_policy == ok
    with pytest.raises(Exception):
        AppConfig.model_validate({**base, "continuation_policy": "always"})


def test_cost_inputs_defaults_validation_and_round_trip():
    """ADR-0021: the operator rate card is additive config (no migration),
    validated (no negative rates, no duplicate prefixes), and deep_merge-
    patchable: a narrow PUT body {cost_inputs: {...}} replaces the rates
    list wholesale while preserving the untouched sibling key."""
    from devcake.config import CostInputs, ModelRate, deep_merge

    fresh = AppConfig()
    assert [r.model_prefix for r in fresh.cost_inputs.rates] == [
        "grok-4.5", "claude-opus"]
    assert fresh.cost_inputs.override_native is False
    assert fresh.cost_inputs.rate_card_id == "builtin-v2"

    # pre-feature config file (no cost_inputs key) still validates at v4
    base = _base()
    base.pop("cost_inputs", None)
    assert AppConfig.model_validate(base).cost_inputs.rate_card_id == "builtin-v2"

    # a default-card edit must not write through to DEFAULT_MODEL_RATES
    fresh.cost_inputs.rates[0].input_per_mtok = 99.0
    assert AppConfig().cost_inputs.rates[0].input_per_mtok == 2.00

    with pytest.raises(ValueError):
        ModelRate(model_prefix="x", input_per_mtok=-1.0,
                  cache_read_per_mtok=0.1, output_per_mtok=1.0)
    with pytest.raises(ValueError):
        CostInputs(rates=[
            ModelRate(model_prefix="grok-4.5", input_per_mtok=1.0,
                      cache_read_per_mtok=0.1, output_per_mtok=1.0),
            ModelRate(model_prefix="grok-4.5", input_per_mtok=2.0,
                      cache_read_per_mtok=0.2, output_per_mtok=2.0)])

    # narrow PUT patch: rates replaced wholesale, sibling flag preserved
    on = AppConfig.model_validate(
        {**_base(), "cost_inputs": {"override_native": True}})
    assert on.cost_inputs.override_native is True
    assert [r.model_prefix for r in on.cost_inputs.rates] == [
        "grok-4.5", "claude-opus"]
    merged = deep_merge(on.model_dump(), {"cost_inputs": {"rates": [
        {"model_prefix": "claude-opus", "input_per_mtok": 5.0,
         "cache_read_per_mtok": 0.5, "output_per_mtok": 25.0}]}})
    patched = AppConfig.model_validate(merged)
    assert [r.model_prefix for r in patched.cost_inputs.rates] == ["claude-opus"]
    assert patched.cost_inputs.override_native is True    # sibling survived
    assert patched.cost_inputs.rate_card_id.startswith("operator:")

    # round-trip through model_dump keeps the operator card
    again = AppConfig.model_validate(patched.model_dump())
    assert again.cost_inputs.rate_card_id == patched.cost_inputs.rate_card_id


def test_repo_url_shape_validated():
    """ISSUES #10: malformed forge URLs rejected at schema layer."""
    base = _base()
    bad = dict(base, repos=[{**base["repos"][0], "url": "not-a-url"}])
    with pytest.raises(Exception, match="invalid"):
        AppConfig.model_validate(bad)
    # one path segment is malformed on EVERY forge (owner/repo minimum) —
    # and the error copy must stay forge-neutral (F1)
    short = dict(base, repos=[{**base["repos"][0],
                               "url": "https://github.com/onlyowner"}])
    with pytest.raises(Exception, match="owner/repo"):
        AppConfig.model_validate(short)
    ok = dict(base, repos=[{**base["repos"][0],
                            "url": "https://github.com/o/r"}])
    AppConfig.model_validate(ok)


def test_make_pmo_dispatches_from_registry():
    from devcake.adapters.gitea_issues import GiteaIssuesAdapter
    from devcake.adapters.linear import LinearAdapter
    from devcake.adapters.registry import PMO_SYSTEMS, make_pmo
    cfg = AppConfig(pmos=[PMOInstance(name="linear", team_key="DEV")])
    assert isinstance(make_pmo(cfg.pmos[0]), LinearAdapter)
    # both in-tree PMO systems (Linear + forge-issue Gitea Issues)
    assert set(PMO_SYSTEMS) == {"linear", "gitea_issues"}
    info = PMO_SYSTEMS["linear"]
    # api_key_env_default removed at v4 (secrets are GUI-stored, not env-named)
    assert info.secret_env_vars and info.token_patterns and info.secret_shape_prefixes
    gitea = PMO_SYSTEMS["gitea_issues"]
    assert gitea.needs_api_base is True
    assert gitea.token_patterns == []  # 40-hex tokens — value registration only
    gi = AppConfig(pmos=[PMOInstance(
        name="gitea", system="gitea_issues", team_key="org/board",
        api_base="http://gitea:3000")])
    assert isinstance(make_pmo(gi.pmos[0]), GiteaIssuesAdapter)


def test_stale_put_bodies_rejected_not_dropped():
    """pydantic ignores unknown keys, so without the guard a stale client's
    PUT would silently lose the edit instead of failing."""
    with pytest.raises(ValueError, match="schema v1"):
        reject_stale_patch({"pmo": {"team_key": "OPS"}})
    with pytest.raises(ValueError, match="schema v1"):
        reject_stale_patch({"repo": {"url": "https://github.com/x/y"}})
    # v2 shape: id-keyed instance entries / explicit old version
    with pytest.raises(ValueError, match="v2"):
        reject_stale_patch({"pmos": [{"id": "main", "team_key": "OPS"}]})
    with pytest.raises(ValueError, match="stale"):
        reject_stale_patch({"schema_version": 2})
    # v3 shape: *_env indirection fields (secrets are GUI-stored at v4)
    with pytest.raises(ValueError, match=r"v3 \*_env"):
        reject_stale_patch({"pmos": [{"name": "linear", "api_key_env": "LINEAR_API_KEY"}]})
    with pytest.raises(ValueError, match=r"v3 \*_env"):
        reject_stale_patch({"repos": [{"name": "main", "token_env": "GITHUB_TOKEN"}]})
    with pytest.raises(ValueError, match=r"v3 \*_env"):
        reject_stale_patch({"repos": [{"name": "main", "reviewer_token_env": None}]})
    # current bodies pass through untouched (auto_merge is per-repo, ADR-0020)
    reject_stale_patch({"repos": [{"name": "main", "auto_merge": False}]})
    reject_stale_patch({"pmos": [{"team_key": "OPS"}]})
    reject_stale_patch({"pmos": [{"name": "linear", "team_key": "OPS"}]})


def test_load_config_refuses_v1_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(V1_YAML)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    with pytest.raises(RuntimeError, match="schema v1"):
        config_mod.load_config()
    # the refusal must not touch the file — the operator migrates it by hand
    assert yaml.safe_load(path.read_text())["schema_version"] == 1


def test_load_config_warns_on_legacy_top_level_merge_doctrine(tmp_path, monkeypatch,
                                                             caplog):
    """ADR-0020: pre-v1 top-level auto_merge is dropped (defaults per-repo);
    operators must see a loud warning, not a quiet no-op."""
    import logging
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema_version: 4\n"
        "pmos:\n- name: linear\n  team_key: DEV\n"
        "repos:\n- name: main\n  url: https://github.com/o/r\n"
        "auto_merge: true\n"
        "auto_resolve_merge_conflicts: false\n"
    )
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    with caplog.at_level(logging.WARNING, logger="devcake.config"):
        # logger name may be the package logger — also catch root "devcake"
        with caplog.at_level(logging.WARNING):
            cfg = config_mod.load_config()
    assert cfg.repos[0].auto_merge is False          # default, not migrated
    assert cfg.repos[0].auto_resolve_merge_conflicts is True
    text = "\n".join(r.message for r in caplog.records)
    assert "auto_merge" in text
    assert "DROPPED" in text or "dropped" in text.lower()
    assert "per-repo" in text.lower() or "ADR-0020" in text


def test_auto_merge_flipped_on_helper():
    from devcake.config import auto_merge_flipped_on
    prev = [{"name": "a", "auto_merge": False},
            {"name": "b", "auto_merge": True}]
    new = [RepoInstance(name="a", url="https://github.com/o/a", auto_merge=True),
           RepoInstance(name="b", url="https://github.com/o/b", auto_merge=True),
           RepoInstance(name="c", url="https://github.com/o/c", auto_merge=True)]
    assert auto_merge_flipped_on(prev, new) == {"a", "c"}


def test_load_config_stale_shapes_and_current(tmp_path, monkeypatch):
    """Detection keys on SHAPE first, version second — a hand-written current
    file without schema_version boots; v2 id-keyed files and old explicit
    versions are refused with hand-migration instructions."""
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)

    path.write_text("pmos:\n- name: linear\n  team_key: DEV\nrepos:\n- name: main\n")
    assert config_mod.load_config().pmos[0].team_key == "DEV"

    path.write_text("pmos:\n- id: main\n  team_key: DEV\nrepos:\n- id: main\n")
    with pytest.raises(RuntimeError, match="v2 .id. field"):
        config_mod.load_config()

    path.write_text("schema_version: 2\npmos:\n- name: linear\nrepos:\n- name: main\n")
    with pytest.raises(RuntimeError, match="schema_version 2"):
        config_mod.load_config()

    # v3 file: *_env fields present — pydantic would silently drop them and
    # leave every secret reading empty, so the shape is refused loudly
    path.write_text("schema_version: 3\npmos:\n- name: linear\n  team_key: DEV\n"
                    "  api_key_env: LINEAR_API_KEY\nrepos:\n- name: main\n")
    with pytest.raises(RuntimeError, match=r"v3 \*_env"):
        config_mod.load_config()
    path.write_text("pmos:\n- name: linear\nrepos:\n- name: main\n"
                    "  token_env: GITHUB_TOKEN\n  token_ro_env: RO\n")
    with pytest.raises(RuntimeError, match=r"v3 \*_env"):
        config_mod.load_config()

    path.write_text("")  # empty file → defaults, same as first boot
    assert config_mod.load_config().schema_version == 4


def test_dev_type_secret_env_names_validated():
    """secret_env delivers named GUI-stored secrets into the Dev's runspec
    env, where the secret half OVERRIDES spec_env (runs.py runspec.result) —
    names must be harness-secret-shaped (api.connections_service
    ._HARNESS_VAR_RE) and must
    not shadow the Dev protocol/tooling contract."""
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 secret_env=["DD_API_KEY", "DD_APP_KEY"])
    assert dt.secret_env == ["DD_API_KEY", "DD_APP_KEY"]
    # pre-existing Dev Type YAML without the field loads with []
    assert DevType(name="x", harness_template="claude-code").secret_env == []
    for bad in ("dd_api_key", "9DD_KEY", "", "X" * 65, "DD API KEY"):
        with pytest.raises(Exception, match="secret env"):
            DevType(name="x", harness_template="claude-code", secret_env=[bad])
    with pytest.raises(Exception, match="duplicate"):
        DevType(name="x", harness_template="claude-code",
                secret_env=["DD_API_KEY", "DD_API_KEY"])
    for shadow in ("DEVCAKE_MISSION_ID", "OTEL_EXPORTER_OTLP_ENDPOINT",
                   "GIT_ASKPASS", "PATH", "HOME",
                   # transport auth: send() re-reads it per message — a
                   # shadowed value kills every artifact undiagnosed
                   "REDIS_PASSWORD",
                   # forge CLI tokens: no GIT_ prefix ("GITLAB"/"GITEA"
                   # have no underscore) and clobbered by the entrypoint
                   "GH_TOKEN", "GITLAB_TOKEN", "GITEA_SERVER_TOKEN"):
        with pytest.raises(Exception, match="shadow"):
            DevType(name="x", harness_template="claude-code",
                    secret_env=[shadow])


def test_secret_env_blocklist_covers_registry_and_protocol():
    """The reserved-name set is hand-typed in config.py (config stays
    import-light — no adapters import). This test derives the must-cover
    set from the live forge registry and the Dev-protocol env builder, so
    drift — a new forge's CLI token env, a new protocol var — fails CI
    instead of shipping a shadowable name."""
    from types import SimpleNamespace

    from devcake.adapters.registry import forges

    def refused(name):
        with pytest.raises(Exception, match="shadow"):
            DevType(name="x", harness_template="claude-code",
                    secret_env=[name])

    # every forge adapter's CLI token env (the entrypoint overwrites these
    # from DEVCAKE_FORGE_TOKEN — an operator value would be silently lost)
    for desc in forges().values():
        for var in desc.cli_token_envs:
            refused(var)
    # every app-authoritative protocol var (_protocol_spec_env ignores self)
    forge = SimpleNamespace(descriptor=SimpleNamespace(
        clone_user="x", git_user_name="x", git_email="x@x",
        cli_token_envs=["GH_TOKEN"]))
    repo = SimpleNamespace(url="https://x/r.git", default_branch="main")
    spec = dispatch._protocol_spec_env(
        None, mission_id="m", mission_key="K-1", mission_type="EXECUTE",
        dev_type=DevType(name="x", harness_template="claude-code"), seq=1,
        extra_args="", repo=repo, forge=forge)
    for var in spec:
        refused(var)
    # the entrypoint's stage-1 transport env (docs/07 §3, read before the
    # runspec merge but shadowable through os.environ.update)
    for var in ("REDIS_URL", "REDIS_USER", "REDIS_PASSWORD", "TRACEPARENT"):
        refused(var)


def test_harness_var_regex_shared():
    """One shape definition: BOTH downstream copies compile from
    config.HARNESS_VAR_PATTERN — the endpoint validator, the bundle
    validator, and the store gate can't drift apart (2026-08-12 audit SEC-8:
    settings_bundle carried a hand-copied literal)."""
    from devcake.api.connections_service import _HARNESS_VAR_RE
    from devcake.config import HARNESS_VAR_PATTERN
    from devcake.settings_bundle import _HARNESS_RE
    assert _HARNESS_VAR_RE.pattern == f"^{HARNESS_VAR_PATTERN}$"
    assert _HARNESS_RE.pattern == f"^{HARNESS_VAR_PATTERN}$"


def test_reference_repos_validated_and_disjoint():
    base = _base()
    base["pmos"][0]["reference_repos"] = ["nosuchrepo"]
    with pytest.raises(Exception, match="name no configured repo"):
        AppConfig.model_validate(base)
    repo = base["repos"][0]["name"]
    base["pmos"][0]["repos"] = [repo]
    base["pmos"][0]["reference_repos"] = [repo]
    with pytest.raises(Exception, match="both a"):
        AppConfig.model_validate(base)
    base["pmos"][0]["repos"] = []
    AppConfig.model_validate(base)


def test_pmo_assignment_override_keys_validated():
    base = _base()
    # unknown mission-type key refused loudly (a typo would otherwise be
    # silently inert — the override map is consulted by mission type)
    base["pmos"][0]["assignments"] = {
        "DEPLOY": {"dev_type": "judgment", "extra_cli_args": ""}}
    with pytest.raises(Exception, match="unknown mission type"):
        AppConfig.model_validate(base)
    # an override row with an empty dev_type is a contradiction: presence of
    # the key means "override", emptiness means "inherit" — refuse, pointing
    # at the fix
    base["pmos"][0]["assignments"] = {
        "EXECUTE": {"dev_type": "", "extra_cli_args": "--max-turns 15"}}
    with pytest.raises(Exception, match="remove the key to inherit"):
        AppConfig.model_validate(base)
    # partial override is the designed shape: one row overridden, the rest
    # of the map absent (inherits global)
    base["pmos"][0]["assignments"] = {
        "EXECUTE": {"dev_type": "judgment", "extra_cli_args": ""}}
    cfg = AppConfig.model_validate(base)
    assert cfg.pmos[0].assignments["EXECUTE"].dev_type == "judgment"
    # default: no overrides — existing configs parse unchanged
    assert PMOInstance(name="x", team_key="T").assignments == {}


def test_default_assignments_are_copies_not_shared_objects():
    """The assignments default factory must DEEP-copy DEFAULT_ASSIGNMENTS:
    with shared Assignment objects, an in-place edit (rename_dev_type does
    `a.dev_type = new`) on one defaults-shaped config writes through to the
    module constant and every later AppConfig() for the process lifetime."""
    from devcake.config import DEFAULT_ASSIGNMENTS
    a = AppConfig()
    assert a.assignments["EXECUTE"] is not DEFAULT_ASSIGNMENTS["EXECUTE"]
    a.assignments["EXECUTE"].dev_type = "mutated"
    assert DEFAULT_ASSIGNMENTS["EXECUTE"].dev_type == "implementer"
    assert AppConfig().assignments["EXECUTE"].dev_type == "implementer"


def test_assignment_for_resolves_override_wholesale_or_global():
    from devcake.config import assignment_for
    base = _base()
    # global ONBOARD carries harness-specific args; the cs instance overrides
    # ONBOARD to another dev type with NO args
    base["pmos"] = [dict(base["pmos"][0], name="eng", team_key="ENG"),
                    dict(base["pmos"][0], name="cs", team_key="CS",
                         assignments={"ONBOARD": {"dev_type": "implementer",
                                                  "extra_cli_args": ""}})]
    cfg = AppConfig.model_validate(base)
    eng, cs = cfg.pmos
    # no override → the global row, args included
    a = assignment_for(cfg, eng, "ONBOARD")
    assert (a.dev_type, a.extra_cli_args) == ("judgment", "--max-turns 15")
    # override → the override row WHOLESALE: empty args stay empty, never
    # inherited from the global row (flags are harness-specific — mixing a
    # judgment-harness flag into an implementer run is the exact mismatch
    # the admin UI warns about)
    a = assignment_for(cfg, cs, "ONBOARD")
    assert (a.dev_type, a.extra_cli_args) == ("implementer", "")
    # non-overridden type on the overriding instance still inherits
    assert assignment_for(cfg, cs, "EXECUTE").dev_type == "implementer"


def test_global_assignments_validated_at_the_model(tmp_path):
    """SEC-3 (2026-08-12 audit): the global map is validated where EVERY
    entry point funnels — model_validate — so a hand-edited config.yaml
    refuses at boot with remediation instead of KeyErroring inside the poll
    cycle at assignment_for()."""
    base = _base()
    # missing mission types refused, remediation names the fix
    base["assignments"] = {
        "ONBOARD": {"dev_type": "judgment", "extra_cli_args": ""}}
    with pytest.raises(Exception, match="delete the `assignments:` key"):
        AppConfig.model_validate(base)
    # empty-typed row refused (the PUT used to be the only guard)
    base["assignments"] = {
        mt: {"dev_type": "judgment" if mt != "EXECUTE" else "",
             "extra_cli_args": ""}
        for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}
    with pytest.raises(Exception, match="must name a\\s+Dev Type"):
        AppConfig.model_validate(base)
    # unknown key refused with the valid-keys hint
    base["assignments"] = {
        **{mt: {"dev_type": "judgment", "extra_cli_args": ""}
           for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")},
        "DEPLOY": {"dev_type": "judgment", "extra_cli_args": ""}}
    with pytest.raises(Exception, match="unknown mission type"):
        AppConfig.model_validate(base)
    # deleting the key restores the default factory — the remediation is real
    base["assignments"] = None
    del base["assignments"]
    cfg = AppConfig.model_validate(base)
    assert set(cfg.assignments) == {"ONBOARD", "PLAN", "EXECUTE", "REVIEW"}


def test_put_assignments_rejects_unknown_mission_type_key(
        tmp_path, monkeypatch):
    """The PUT accepted unknown mission-type keys before the shared rule
    (latent bug closed by SEC-3's one-validation-path move)."""
    import asyncio

    from fastapi import HTTPException

    from devcake.api.devtypes_service import put_assignments
    from devcake.config import DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    cfg = AppConfig()
    dts = {"judgment": DevType(name="judgment",
                               harness_template="claude-code")}
    body = {mt: {"dev_type": "judgment", "extra_cli_args": ""}
            for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}
    bad = {**body, "DEPLOY": {"dev_type": "judgment", "extra_cli_args": ""}}
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(HTTPException) as exc:
            loop.run_until_complete(
                put_assignments(bad, config=cfg, dev_types=dts))
        assert exc.value.status_code == 422
        assert "unknown mission type" in exc.value.detail
    finally:
        loop.close()


def test_container_limits_defaults_bounds_and_round_trip():
    """Per-container cgroup knobs (2026-08-13): defaults mirror the old
    best-effort DAG budget (4g / 2 cpus), pids off; 0 = unlimited;
    negatives refused; rides the settings bundle via model_dump."""
    cfg = AppConfig()
    cl = cfg.container_limits
    assert (cl.memory_mb, cl.cpus, cl.pids) == (4096, 2.0, 0)
    got = AppConfig.model_validate(
        {**_base(), "container_limits": {"memory_mb": 0, "cpus": 0.5}})
    assert got.container_limits.memory_mb == 0     # 0 = unlimited
    assert got.container_limits.cpus == 0.5
    assert got.container_limits.pids == 0          # sibling default holds
    with pytest.raises(Exception):
        AppConfig.model_validate(
            {**_base(), "container_limits": {"memory_mb": -1}})
    assert AppConfig.model_validate(cfg.model_dump()).container_limits == cl


def test_skill_sources_round_trip_validation_and_name_disjointness():
    """2026-08-14 ruling (supersedes ADR-0016-addendum decision 1): skills
    connect through dedicated skill_sources, never repo cards. Relative
    subdir only, `..` refused; names share the mirror namespace with repo
    cards so collisions are refused; repo cards have NO skills facet."""
    from devcake.config import RepoInstance, SkillSource
    src = SkillSource(name="shelf", url="https://github.com/o/skills",
                      subdir="skills/")
    assert src.subdir == "skills"               # normalized, no slashes
    with pytest.raises(Exception):
        SkillSource(name="shelf", url="https://github.com/o/skills",
                    subdir="../escape")
    cfg = AppConfig(pmos=[], repos=[], skill_sources=[src])
    assert AppConfig.model_validate(cfg.model_dump()) \
        .skill_sources[0].subdir == "skills"
    # one mirror namespace: a source may not shadow a repo card
    with pytest.raises(Exception, match="collide"):
        AppConfig(pmos=[], skill_sources=[SkillSource(
            name="main", url="https://github.com/o/skills")],
            repos=[RepoInstance(name="main", url="https://github.com/o/r")])
    with pytest.raises(Exception, match="duplicate"):
        AppConfig(pmos=[], repos=[], skill_sources=[src, src])
    # the old facet is gone from repo cards
    assert not hasattr(
        RepoInstance(name="main", url="https://github.com/o/r"),
        "skills_subdir")
