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
        t.save_template("MAPPER", "x", "y")
    t.delete_template("EXECUTE", "terse")
    assert all(e["name"] != "terse" for e in t.list_templates()["EXECUTE"])
    with pytest.raises(FileNotFoundError):
        t.delete_template("EXECUTE", "terse")
    with pytest.raises(ValueError, match="built-in"):
        t.delete_template("EXECUTE", "Development")


# ── slice 4: resolve + fallback + warnings ───────────────────────────────────

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

def test_dispatch_uses_active_template_and_falls_back(monkeypatch, tmp_path):
    from test_transitions import make_mgr, mission
    from devcake.config import PMOInstance
    from devcake.domain.model import MissionType

    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "custom", "CUSTOM-MARKER {key}")

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
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

    # active template file vanishes → dispatch falls back to the default
    (tmp_path / "config" / "prompt_templates" / "EXECUTE" / "custom.yaml").unlink()
    launched.clear()
    m.labels = {"DEVCAKE", "DEVCAKE-EXECUTE"}
    run_coro(mgr.dispatch(m, MissionType.EXECUTE, dev))
    assert launched and "Binding rules" in launched[0].spec_prompt


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
    dts = {"senior-dev": DevType(name="senior-dev", harness_template="claude-code",
                                 identifying_prompt="You are senior."),
           "customdev": DevType(name="customdev", harness_template="codex",
                                identifying_prompt="Custom prefix.")}
    t.seed_devtype_prompts(dts)
    listing = t.list_devtype_prompts(dts)
    assert {e["name"] for e in listing["senior-dev"]} == {"Development",
                                                          "Customer Success"}
    assert {e["name"] for e in listing["customdev"]} == {"Development"}
    # seeded once from live prompt; NOT re-canonicalized on reseed
    t.save_devtype_prompt("senior-dev", "Development", "Edited by operator.")
    t.seed_devtype_prompts(dts)
    assert t.resolve_devtype_prompt("senior-dev", None, "fb")[0] == "Edited by operator."
    # CS resolves; a ghost name falls back to Development with a warning
    text, warn = t.resolve_devtype_prompt("senior-dev", "Customer Success", "fb")
    assert "customer-success" in text and warn is None
    text, warn = t.resolve_devtype_prompt("senior-dev", "ghost", "fb")
    assert text == "Edited by operator." and "ghost" in warn
    # unknown dev type dir → straight to the stored-field fallback
    assert t.resolve_devtype_prompt("nodir", "Development", "fb")[0] == "fb"
    from devcake.config import AppConfig
    cfg = AppConfig()
    cfg.active_devtype_prompts = {"senior-dev": "ghost"}
    assert len(t.devtype_prompt_warnings(cfg, dts)) == 1


def test_rename_dev_type_moves_templates_and_refs(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import main as app_main
    from devcake.config import DevType
    monkeypatch.setattr(app_main, "save_config", lambda c: None)
    monkeypatch.setattr(app_main, "save_dev_type", lambda d: None)
    monkeypatch.setattr(app_main, "delete_dev_type", lambda n: None)
    dt = DevType(name="olddev", harness_template="codex",
                 identifying_prompt="I am old.")
    app_main.dev_types["olddev"] = dt
    app_main.config.assignments["EXECUTE"].dev_type = "olddev"
    app_main.config.active_devtype_prompts["olddev"] = "Customer Success"
    d = tmp_path / "config" / "devtype_prompt_templates" / "olddev"
    d.mkdir(parents=True)
    (d / "Development.yaml").write_text("name: Development\ntemplate: I am old.\n")
    try:
        out = run_coro(app_main.rename_dev_type("olddev", {"new_name": "newdev"}))
        assert out["renamed"] and "newdev" in app_main.dev_types
        assert "olddev" not in app_main.dev_types
        assert app_main.config.assignments["EXECUTE"].dev_type == "newdev"
        assert app_main.config.active_devtype_prompts == {"newdev": "Customer Success"}
        assert (tmp_path / "config" / "devtype_prompt_templates" / "newdev"
                / "Development.yaml").exists()
    finally:
        app_main.dev_types.pop("newdev", None)
        app_main.config.assignments["EXECUTE"].dev_type = "main-dev"
        app_main.config.active_devtype_prompts.pop("newdev", None)
