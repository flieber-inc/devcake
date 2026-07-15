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
    assert sorted(p.parent.name for p in base.glob("*/default.yaml")) == [
        "EXECUTE", "ONBOARD", "PLAN", "REVIEW"]
    # a drifted default is rewritten to canonical (default is API-read-only,
    # so overwrite never destroys operator data); user files stay untouched
    (base / "PLAN" / "default.yaml").write_text("mission_type: PLAN\n"
                                                "name: default\ntemplate: hacked\n")
    t.save_template("PLAN", "mine", "custom {key}")
    t.seed_default_templates()
    assert "hacked" not in (base / "PLAN" / "default.yaml").read_text()
    assert t.resolve_playbook("PLAN", "mine")[0] == "custom {key}"


def test_save_list_delete_roundtrip_and_validation(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "terse", "just {key} on {branch}")
    listing = t.list_templates()
    names = {e["name"]: e for e in listing["EXECUTE"]}
    assert names["default"]["builtin"] is True
    assert names["terse"]["builtin"] is False
    with pytest.raises(ValueError, match="reserved"):
        t.save_template("EXECUTE", "default", "x")
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
    with pytest.raises(ValueError, match="reserved"):
        t.delete_template("EXECUTE", "default")


# ── slice 4: resolve + fallback + warnings ───────────────────────────────────

def test_resolve_falls_back_to_default_with_warning(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "terse", "just {key}")
    text, warn = t.resolve_playbook("EXECUTE", "terse")
    assert text == "just {key}" and warn is None
    text, warn = t.resolve_playbook("EXECUTE", "default")
    assert text == DEFAULT_PLAYBOOKS["EXECUTE"] and warn is None
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
    mgr.instance = PMOInstance(name="linear", team_key="DEV",
                               default_repo="main")
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
