"""Config schema v4 (instances-with-identities, GUI-stored secrets): name-keyed pmos:/repos:
lists, N-pmo validators, the generic stale-schema refusal (v1 singular keys,
v2 id-keyed entries, old schema_version) — auto-migrations were removed at v0
crystallization; there are no deployments to migrate."""

import yaml
import pytest

import devcake.config as config_mod
from devcake.config import AppConfig, PMOInstance, RepoInstance, reject_stale_patch

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
    from devcake.adapters.linear import LinearAdapter
    from devcake.adapters.registry import PMO_SYSTEMS, make_pmo
    cfg = AppConfig(pmos=[PMOInstance(name="linear", team_key="DEV")])
    assert isinstance(make_pmo(cfg.pmos[0]), LinearAdapter)
    assert set(PMO_SYSTEMS) == {"linear"}
    info = PMO_SYSTEMS["linear"]
    # api_key_env_default removed at v4 (secrets are GUI-stored, not env-named)
    assert info.secret_env_vars and info.token_patterns and info.secret_shape_prefixes


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
    # current bodies pass through untouched
    reject_stale_patch({"auto_merge": False})
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
