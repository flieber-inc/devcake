"""Per-Mission-Type prompt templates (v0.1.1 feature): the safe renderer
(allowlisted {var} substitution — no raw str.format over operator text), the
/data-seeded default templates, storage CRUD + validation, resolve-with-
fallback, and the dispatch-level wiring."""

import asyncio
from datetime import datetime, timezone

import pytest

from devcake.domain.model import Mission
from devcake.prompts import (DEFAULT_PLAYBOOKS, PLAYBOOK_VARS, execute_prompt,
                             onboard_prompt, plan_prompt, render_playbook,
                             review_prompt)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _mission(kind="issue"):
    return Mission(pmo_id="p1", pmo_kind=kind, instance="linear", key="T-9",
                   title="the title", status="in_progress",
                   description="do the thing",
                   updated_at=datetime.now(timezone.utc))


GH_PR = ("Open a PR (idempotent): `gh pr list --head {branch}` — else "
         "`gh pr create --base {default} --head {branch} --title \"[{key}] {title}\"`")


# ── slice 1: the renderer ────────────────────────────────────────────────────

def test_render_substitutes_only_allowlisted_vars():
    out = render_playbook('a {key} b {nope} {"json": 1} {{esc}}',
                          {"key": "T-9"})
    assert out == 'a T-9 b {nope} {"json": 1} {{esc}}'


def test_default_playbooks_are_undoubled_with_vars_intact():
    ob = DEFAULT_PLAYBOOKS["ONBOARD"]
    assert '{"schema_version": 1, "outcome": "plan_needed"' in ob  # single-braced
    assert "{key}" in ob and "{project_note}" in ob
    assert set(DEFAULT_PLAYBOOKS) == set(PLAYBOOK_VARS) == {
        "ONBOARD", "PLAN", "EXECUTE", "REVIEW"}


def test_builders_byte_identical_with_explicit_default():
    m = _mission()
    assert onboard_prompt("ID", m) == onboard_prompt(
        "ID", m, playbook=DEFAULT_PLAYBOOKS["ONBOARD"])
    assert plan_prompt("ID", m) == plan_prompt(
        "ID", m, playbook=DEFAULT_PLAYBOOKS["PLAN"])
    assert review_prompt("ID", m) == review_prompt(
        "ID", m, playbook=DEFAULT_PLAYBOOKS["REVIEW"])
    assert execute_prompt("ID", m, "repo", GH_PR) == execute_prompt(
        "ID", m, "repo", GH_PR, playbook=DEFAULT_PLAYBOOKS["EXECUTE"])


# ── slice 2: custom playbook keeps the appended fragments ────────────────────

def test_custom_playbook_renders_vars_and_keeps_fragments():
    m = _mission()
    out = execute_prompt("ID", m, "repo", GH_PR,
                         playbook="DO {key} on {branch} via {pr_instructions}")
    assert "DO T-9 on devcake/LINEAR-T-9" in out
    assert "gh pr create" in out                  # nested descriptor rendered
    assert "human_needed" in out                  # HUMAN_HANDOFF appended
    assert "🧑 HUMAN" in out                      # HUMAN_COMMENTS_NOTE appended


# ── slice 3: storage + seeding ───────────────────────────────────────────────

def _tpl(monkeypatch, tmp_path):
    import devcake.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake.prompts import templates
    return templates


def test_seed_writes_defaults_and_reseeds_drift(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    base = tmp_path / "config" / "prompt_templates"
    assert sorted(p.parent.name for p in base.glob("*/Development.yaml")) == [
        "EXECUTE", "ONBOARD", "PLAN", "REVIEW"]
    # the second built-in preset seeds too (Customer Success, 2026-07-15)
    assert len(list(base.glob("*/Customer Success.yaml"))) == 4
    # a drifted default is rewritten to canonical (default is API-read-only,
    # so overwrite never destroys operator data); user files stay untouched
    (base / "PLAN" / "Development.yaml").write_text("mission_type: PLAN\n"
                                                "name: Development\ntemplate: hacked\n")
    t.save_template("PLAN", "mine", "custom {key}")
    t.seed_default_templates()
    assert "hacked" not in (base / "PLAN" / "Development.yaml").read_text()
    assert t.resolve_playbook("PLAN", "mine")[0] == "custom {key}"


def test_save_list_delete_roundtrip_and_validation(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "terse", "just {key} on {branch}")
    listing = t.list_templates()
    names = {e["name"]: e for e in listing["EXECUTE"]}
    assert names["Development"]["builtin"] is True
    assert names["Customer Success"]["builtin"] is True
    assert names["terse"]["builtin"] is False
    for reserved in ("default", "Development", "Customer Success"):
        with pytest.raises(ValueError, match="built-in"):
            t.save_template("EXECUTE", reserved, "x")
    with pytest.raises(ValueError, match="unknown placeholder"):
        t.save_template("EXECUTE", "bad", "uses {summary} and {key}")
    with pytest.raises(ValueError, match="valid variables"):
        t.save_template("PLAN", "bad", "{branch}")     # branch not a PLAN var
    with pytest.raises(ValueError, match="empty"):
        t.save_template("PLAN", "bad", "   ")
    with pytest.raises(ValueError, match="64"):
        t.save_template("PLAN", "big", "x" * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="unknown mission type"):
        t.save_template("STEWARD", "x", "y")
    t.delete_template("EXECUTE", "terse")
    assert all(e["name"] != "terse" for e in t.list_templates()["EXECUTE"])
    with pytest.raises(FileNotFoundError):
        t.delete_template("EXECUTE", "terse")
    with pytest.raises(ValueError, match="built-in"):
        t.delete_template("EXECUTE", "Development")


# ── slice 4: resolve + fallback + warnings ───────────────────────────────────

def test_decomposition_rule_placeholder_onboard_only(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("ONBOARD", "mine", "{decomposition_rule} for {key}")
    with pytest.raises(ValueError, match="valid variables"):
        t.save_template("PLAN", "bad", "{decomposition_rule}")


def test_template_warning_when_onboard_lacks_decomposition_rule(monkeypatch, tmp_path):
    """A custom ONBOARD template saved before ADR-0012 keeps the old static
    prohibition — with a depth limit above 1 the knob would be silently
    inert, so /health must say so; limit 1 matches the old sentence and
    stays quiet, as do the placeholder-carrying builtins."""
    from devcake.config import AppConfig
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("ONBOARD", "legacy",
                    "triage {key}: Never decompose a mission whose labels "
                    "include DEVCAKE-CREATED.")
    stale = AppConfig(active_prompt_templates={"ONBOARD": "legacy"})
    assert any("decomposition_rule" in w for w in t.template_warnings(stale))
    quiet = AppConfig(active_prompt_templates={"ONBOARD": "legacy"},
                      max_decomposition_depth=1)
    assert not any("decomposition_rule" in w
                   for w in t.template_warnings(quiet))
    builtin = AppConfig()
    assert not any("decomposition_rule" in w
                   for w in t.template_warnings(builtin))


def test_resolve_falls_back_to_default_with_warning(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "terse", "just {key}")
    text, warn = t.resolve_playbook("EXECUTE", "terse")
    assert text == "just {key}" and warn is None
    text, warn = t.resolve_playbook("EXECUTE", "default")   # legacy alias
    assert text == DEFAULT_PLAYBOOKS["EXECUTE"] and warn is None
    from devcake.prompts.customer_success import CS_PLAYBOOKS
    text, warn = t.resolve_playbook("EXECUTE", "Customer Success")
    assert text == CS_PLAYBOOKS["EXECUTE"] and warn is None
    text, warn = t.resolve_playbook("EXECUTE", None)
    assert text == DEFAULT_PLAYBOOKS["EXECUTE"] and warn is None
    text, warn = t.resolve_playbook("EXECUTE", "ghost")
    assert text == DEFAULT_PLAYBOOKS["EXECUTE"]
    assert "ghost" in warn and "EXECUTE" in warn
    # corrupt file → fallback + warning
    p = (tmp_path / "config" / "prompt_templates" / "EXECUTE" / "terse.yaml")
    p.write_text("{{{{not yaml")
    text, warn = t.resolve_playbook("EXECUTE", "terse")
    assert text == DEFAULT_PLAYBOOKS["EXECUTE"] and "terse" in warn

    from devcake.config import AppConfig
    cfg = AppConfig()
    cfg.active_prompt_templates = {"EXECUTE": "ghost"}
    warns = t.template_warnings(cfg)
    assert len(warns) == 1 and "ghost" in warns[0]
    cfg.active_prompt_templates = {"EXECUTE": "default"}
    assert t.template_warnings(cfg) == []


# ── slice 5: dispatch-level wiring ───────────────────────────────────────────

def test_two_pmos_resolve_different_playbooks_for_same_mission_type(
        monkeypatch, tmp_path):
    """CAKE-150 Done-when: two PMO instances on one config can select
    different named templates for the same Mission Type via the chokepoint."""
    from devcake.config import (AppConfig, PMOInstance,
                                active_prompt_template_for)
    from devcake.prompts.customer_success import CS_PLAYBOOKS

    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    cfg = AppConfig(
        active_prompt_templates={"ONBOARD": "Development"},
        pmos=[
            PMOInstance(name="eng", team_key="ENG"),
            PMOInstance(name="cs", team_key="CS",
                        active_prompt_templates={"ONBOARD": "Customer Success"}),
        ],
    )
    eng, cs = cfg.pmos
    eng_name = active_prompt_template_for(cfg, eng, "ONBOARD")
    cs_name = active_prompt_template_for(cfg, cs, "ONBOARD")
    assert eng_name == "Development"
    assert cs_name == "Customer Success"
    eng_text, eng_warn = t.resolve_playbook("ONBOARD", eng_name)
    cs_text, cs_warn = t.resolve_playbook("ONBOARD", cs_name)
    assert eng_warn is None and cs_warn is None
    assert eng_text == DEFAULT_PLAYBOOKS["ONBOARD"]
    assert cs_text == CS_PLAYBOOKS["ONBOARD"]
    assert eng_text != cs_text


def test_template_warnings_cover_pmo_overrides(monkeypatch, tmp_path):
    """Health must check every PMO's effective selection, without duplicate
    spam when an override matches the global active."""
    from devcake.config import AppConfig, PMOInstance

    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    # ghost only on one PMO override → one warning naming the ghost
    cfg = AppConfig(
        active_prompt_templates={"EXECUTE": "Development"},
        pmos=[
            PMOInstance(name="eng", team_key="ENG"),
            PMOInstance(name="cs", team_key="CS",
                        active_prompt_templates={"EXECUTE": "ghost"}),
        ],
    )
    warns = t.template_warnings(cfg)
    assert sum(1 for w in warns if "ghost" in w) == 1
    # override == global → no duplicate missing-template spam for Development
    same = AppConfig(
        active_prompt_templates={"EXECUTE": "Development"},
        pmos=[PMOInstance(
            name="cs", team_key="CS",
            active_prompt_templates={"EXECUTE": "Development"})],
    )
    assert t.template_warnings(same) == []


def test_dispatch_uses_active_template_and_falls_back(monkeypatch, tmp_path):
    from test_transitions import make_mgr, mission
    from devcake.config import PMOInstance
    from devcake.domain.model import MissionType

    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "custom", "CUSTOM-MARKER {key}")

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    m.url = "https://linear.app/acme/issue/T-1"
    mgr, fake, _store = make_mgr(tmp_path, m, forge=_ForgeWithDescriptor())
    mgr.internal_forge = None
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    mgr.config.active_prompt_templates = {"EXECUTE": "custom"}
    launched = []

    async def launch(run, image):
        launched.append(run)

    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    dev = mgr.dev_types["senior-dev"]
    run_coro(mgr.dispatch(m, MissionType.EXECUTE, dev))
    assert launched and "CUSTOM-MARKER T-1" in launched[0].spec_prompt
    assert launched[0].mission_url == "https://linear.app/acme/issue/T-1"

    # active template file vanishes → dispatch falls back to the default
    (tmp_path / "config" / "prompt_templates" / "EXECUTE" / "custom.yaml").unlink()
    launched.clear()
    m.labels = {"DEVCAKE", "DEVCAKE-EXECUTE"}
    run_coro(mgr.dispatch(m, MissionType.EXECUTE, dev))
    assert launched and "Binding rules" in launched[0].spec_prompt


def test_dispatch_uses_pmo_override_over_global(monkeypatch, tmp_path):
    """Dispatch _pb must go through active_prompt_template_for so a per-PMO
    override beats the global active for that instance."""
    from test_transitions import make_mgr, mission
    from devcake.config import PMOInstance
    from devcake.domain.model import MissionType

    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "cs-playbook", "CS-OVERRIDE-MARKER {key}")

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    m.url = "https://linear.app/acme/issue/T-1"
    mgr, fake, _store = make_mgr(tmp_path, m, forge=_ForgeWithDescriptor())
    mgr.internal_forge = None
    mgr.instance = PMOInstance(
        name="cs", team_key="CS", repos=["main"],
        active_prompt_templates={"EXECUTE": "cs-playbook"})
    mgr.config.active_prompt_templates = {"EXECUTE": "Development"}
    launched = []

    async def launch(run, image):
        launched.append(run)

    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    run_coro(mgr.dispatch(m, MissionType.EXECUTE, mgr.dev_types["senior-dev"]))
    assert launched and "CS-OVERRIDE-MARKER T-1" in launched[0].spec_prompt


def test_delete_prompt_template_409_when_pmo_override_active(monkeypatch, tmp_path):
    """Cannot delete a template that any PMO override (or the global active)
    still selects — overrides must not dangle as the only reference."""
    from fastapi import HTTPException
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, PMOInstance

    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "cs-only", "just {key}")
    cfg = AppConfig(
        active_prompt_templates={"EXECUTE": "Development"},
        pmos=[PMOInstance(
            name="cs", team_key="CS",
            active_prompt_templates={"EXECUTE": "cs-only"})],
    )
    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.delete_prompt_template(
            "EXECUTE", "cs-only", config=cfg))
    assert e.value.status_code == 409
    assert "cs" in str(e.value.detail).lower() or "ACTIVE" in str(e.value.detail)
    # still on disk
    assert (tmp_path / "config" / "prompt_templates" / "EXECUTE"
            / "cs-only.yaml").exists()


class _ForgeWithDescriptor:
    descriptor = type("D", (), {
        "pr_instructions": GH_PR, "pr_noun": "pull request", "id": "github",
        "clone_user": "x-access-token", "git_user_name": "DevCake",
        "git_email": "d@x", "cli_token_envs": ["GITHUB_TOKEN"]})()


def test_onboard_repo_options_placement():
    """{repo_options} renders between the mission block and the rubric;
    empty (the default) leaves the playbook unchanged."""
    m = _mission()
    assert "several repositories" not in onboard_prompt("ID", m)
    out = onboard_prompt("ID", m,
                         repo_options="### This team works across several "
                                      "repositories\n- `alpha`\n\n")
    assert out.index("several repositories") < out.index("### Classify")


def test_devtype_prompt_store_seed_resolve_roundtrip(monkeypatch, tmp_path):
    """Item 4-6 (2026-07-15): each Dev Type's identifying prompt is
    template-backed — Development seeded ONCE from the live prompt (user
    data, never re-canonicalized), Customer Success preset for the seeded
    trio, resolution falls back Development → the stored field."""
    from devcake.config import DevType
    t = _tpl(monkeypatch, tmp_path)
    dts = {"judge": DevType(name="judge", harness_template="claude-code",
                            identifying_prompt="You are judge."),
           "customdev": DevType(name="customdev", harness_template="codex",
                                identifying_prompt="Custom prefix.")}
    t.seed_devtype_prompts(dts)
    listing = t.list_devtype_prompts(dts)
    assert {e["name"] for e in listing["judge"]} == {"Development",
                                                     "Customer Success"}
    assert {e["name"] for e in listing["customdev"]} == {"Development"}
    # seeded once from live prompt; NOT re-canonicalized on reseed
    t.save_devtype_prompt("judge", "Development", "Edited by operator.")
    t.seed_devtype_prompts(dts)
    assert t.resolve_devtype_prompt("judge", None, "fb")[0] == "Edited by operator."
    # CS resolves; a ghost name falls back to Development with a warning
    text, warn = t.resolve_devtype_prompt("judge", "Customer Success", "fb")
    assert "customer-success" in text and warn is None
    text, warn = t.resolve_devtype_prompt("judge", "ghost", "fb")
    assert text == "Edited by operator." and "ghost" in warn
    # unknown dev type dir → straight to the stored-field fallback
    assert t.resolve_devtype_prompt("nodir", "Development", "fb")[0] == "fb"
    from devcake.config import AppConfig
    cfg = AppConfig()
    cfg.active_devtype_prompts = {"judge": "ghost"}
    assert len(t.devtype_prompt_warnings(cfg, dts)) == 1


def test_rename_dev_type_moves_templates_and_refs(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.api import main as app_main
    from devcake.config import AppConfig, Assignment, DevType
    from fakes import make_services
    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    dt = DevType(name="olddev", harness_template="codex",
                 identifying_prompt="I am old.")
    # ADR-0028: a fresh per-test graph — the old try/finally that restored
    # the module-global config/dev_types is gone with the globals themselves
    # complete map: the global assignments field refuses partial maps since
    # SEC-3 (a partial map KeyErrors at dispatch for the missing types)
    cfg = AppConfig(assignments={
        mt: Assignment(dev_type="olddev")
        for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})
    cfg.active_devtype_prompts = {"olddev": "Customer Success"}
    monkeypatch.setattr(app_main, "services", make_services(
        config=cfg, dev_types={"olddev": dt}, shared_breakers={}))
    d = tmp_path / "config" / "devtype_prompt_templates" / "olddev"
    d.mkdir(parents=True)
    (d / "Development.yaml").write_text("name: Development\ntemplate: I am old.\n")
    out = run_coro(app_main.rename_dev_type("olddev", {"new_name": "newdev"}))
    assert out["renamed"] and "newdev" in app_main.services.dev_types
    assert "olddev" not in app_main.services.dev_types
    assert app_main.services.config.assignments["EXECUTE"].dev_type == "newdev"
    assert app_main.services.config.active_devtype_prompts == {"newdev": "Customer Success"}
    assert (tmp_path / "config" / "devtype_prompt_templates" / "newdev"
            / "Development.yaml").exists()


def test_remove_dev_type_clears_active_prompt_and_sidecar_dirs(monkeypatch, tmp_path):
    """Deleting a Dev Type must drop active_devtype_prompts keys and remove
    its prompt-template + credential dirs — otherwise Workflow Switcher /
    PUT /config 422s on deep_merge-preserved ghosts (2026-07-19)."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType

    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    saved: list = []
    monkeypatch.setattr(devtypes_service, "save_config",
                        lambda c: saved.append(dict(c.active_devtype_prompts)))
    monkeypatch.setattr(devtypes_service, "delete_dev_type",
                        lambda n: (tmp_path / "config" / "dev_types"
                                   / f"{n}.yaml").unlink(missing_ok=True))

    dt = DevType(name="junior-dev", harness_template="codex",
                 identifying_prompt="junior")
    dts = {"junior-dev": dt, "judgment": DevType(
        name="judgment", harness_template="claude-code")}
    cfg = AppConfig()
    cfg.assignments = {mt: config_mod.Assignment(dev_type="judgment")
                       for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}
    cfg.active_devtype_prompts = {
        "junior-dev": "Customer Success",
        "judgment": "Development",
    }
    (tmp_path / "config" / "dev_types").mkdir(parents=True)
    prompts = tmp_path / "config" / "devtype_prompt_templates" / "junior-dev"
    prompts.mkdir(parents=True)
    (prompts / "Development.yaml").write_text("template: x\n")
    secrets = tmp_path / "secrets" / "junior-dev"
    secrets.mkdir(parents=True)
    (secrets / "creds.json").write_text("{}")

    out = run_coro(devtypes_service.remove_dev_type(
        "junior-dev", config=cfg, dev_types=dts))
    assert out == {"deleted": "junior-dev"}
    assert "junior-dev" not in dts
    assert cfg.active_devtype_prompts == {"judgment": "Development"}
    assert saved and saved[-1] == {"judgment": "Development"}
    assert not prompts.exists()
    assert not secrets.exists()


def test_remove_dev_type_409_when_steward_enabled_binds_it(monkeypatch, tmp_path):
    """DELETE must 409 while the Relations Steward is enabled and points at
    this Dev Type — matching settings_bundle's enabled-gated validator."""
    from fastapi import HTTPException

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType, Steward

    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    dts = {
        "steward": DevType(name="steward", harness_template="claude-code"),
        "judgment": DevType(name="judgment", harness_template="claude-code"),
    }
    cfg = AppConfig(steward=Steward(enabled=True, dev_type="steward"))
    cfg.assignments = {mt: config_mod.Assignment(dev_type="judgment")
                       for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.remove_dev_type(
            "steward", config=cfg, dev_types=dts))
    assert e.value.status_code == 409
    assert "steward" in str(e.value.detail).lower()


def test_remove_dev_type_allowed_when_steward_disabled(monkeypatch, tmp_path):
    """Disabling the steward must clear the 409 — the message says
    'repoint or disable'; disable alone must work when unbound."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType, Steward

    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda dts: None)
    dts = {
        "steward": DevType(name="steward", harness_template="claude-code"),
        "judgment": DevType(name="judgment", harness_template="claude-code"),
    }
    cfg = AppConfig(steward=Steward(enabled=False, dev_type="steward"))
    cfg.assignments = {mt: config_mod.Assignment(dev_type="judgment")
                       for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}

    out = run_coro(devtypes_service.remove_dev_type(
        "steward", config=cfg, dev_types=dts))
    assert out == {"deleted": "steward"}
    assert "steward" not in dts


def test_instance_override_refs_block_delete_and_follow_rename(monkeypatch, tmp_path):
    """ADR-0019 reference hygiene: a Dev Type named only by a PMO instance's
    assignment override must refuse DELETE (409 naming the instance) and be
    remapped by RENAME — same contract as the global map."""
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, Assignment, DevType, PMOInstance

    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    dts = {"cs-agent": DevType(name="cs-agent", harness_template="codex"),
           "judgment": DevType(name="judgment", harness_template="claude-code")}
    cfg = AppConfig(pmos=[PMOInstance(
        name="cs", team_key="CS",
        assignments={"EXECUTE": Assignment(dev_type="cs-agent")})])
    cfg.assignments = {mt: config_mod.Assignment(dev_type="judgment")
                       for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.remove_dev_type(
            "cs-agent", config=cfg, dev_types=dts))
    assert e.value.status_code == 409 and "cs" in str(e.value.detail)

    out = run_coro(devtypes_service.rename_dev_type(
        "cs-agent", {"new_name": "cs-senior"}, config=cfg, dev_types=dts,
        shared_breakers={}))
    assert out["renamed"]
    assert cfg.pmos[0].assignments["EXECUTE"].dev_type == "cs-senior"


def test_template_warning_for_stale_executed_trivially(monkeypatch, tmp_path):
    # founder decision 2026-07-18: outcome removed outright — a pre-removal
    # custom ONBOARD template must warn in /health, not fail silently
    from devcake.config import AppConfig
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("ONBOARD", "old",
                    'stale: return "executed_trivially" {decomposition_rule}')
    cfg = AppConfig()
    cfg.active_prompt_templates = {"ONBOARD": "old"}
    assert any("executed_trivially" in w for w in t.template_warnings(cfg))
    cfg.active_prompt_templates = {"ONBOARD": "default"}
    assert not any("executed_trivially" in w for w in t.template_warnings(cfg))


def test_plan_approval_rule_placeholder_planning_stages_only(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    for mt in ("ONBOARD", "PLAN", "EXECUTE"):
        t.save_template(mt, "gated", "{plan_approval_rule} for {key}")
    with pytest.raises(ValueError, match="valid variables"):
        t.save_template("REVIEW", "bad", "{plan_approval_rule}")


def test_template_warning_when_gated_board_template_lacks_plan_approval_rule(
        monkeypatch, tmp_path):
    """A custom planning-stage template saved before plan approval existed
    cannot tell the Dev the board parks plans — the Dev then invents its
    own gate. /health must say so whenever any PMO gates plans; quiet when
    no board does, and for the placeholder-carrying builtins."""
    from devcake.config import AppConfig, PMOInstance
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("PLAN", "legacy", "plan {key}")
    gated = AppConfig(
        active_prompt_templates={"PLAN": "legacy"},
        pmos=[PMOInstance(name="b", team_key="T", plan_approval=True)])
    assert any("plan_approval_rule" in w for w in t.template_warnings(gated))
    quiet = AppConfig(active_prompt_templates={"PLAN": "legacy"},
                      pmos=[PMOInstance(name="b", team_key="T")])
    assert not any("plan_approval_rule" in w
                   for w in t.template_warnings(quiet))
    builtin = AppConfig(
        pmos=[PMOInstance(name="b", team_key="T", plan_approval=True)])
    assert not any("plan_approval_rule" in w
                   for w in t.template_warnings(builtin))
