"""ADR-0023 addendum — the code-owned section about this host's engine.

Every dispatched Dev's prompt ends its playbook with one section saying
what `docker` is inside the container; when the baker's newest
nested-engine receipt (`bake_status.nested`, docs/11) is red, the section
adds that containers will not work here — never a template placeholder an
operator override could drop.
"""
import json

from fakes import make_mission_manager

from devcake.domain.orchestrator import dispatch
from devcake.prompts import (HUMAN_HANDOFF, execute_prompt, onboard_prompt,
                             plan_prompt, review_prompt)

from test_transitions import mission  # noqa: F401


def _status(tmp_path, monkeypatch, nested):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    body = {"state": "ready", "jobs": [], "detail": ""}
    if nested is not None:
        body["nested"] = nested
    (tmp_path / "harness_bake_status.json").write_text(json.dumps(body))


def test_note_describes_the_engine_and_stays_green_without_a_red_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path / "nothing"))
    note = dispatch._environment_note()
    assert note == dispatch.ENVIRONMENT_NOTE
    assert "rootless engine (podman)" in note and "no\ndaemon socket" in note
    assert "unavailable" not in note
    _status(tmp_path, monkeypatch, None)
    assert dispatch._environment_note() == dispatch.ENVIRONMENT_NOTE
    _status(tmp_path, monkeypatch, {"rig_ok": True, "first_red": ""})
    assert dispatch._environment_note() == dispatch.ENVIRONMENT_NOTE
    assert "podman-compose" in dispatch.ENVIRONMENT_NOTE
    _status(tmp_path, monkeypatch, {"rig_ok": True, "first_red": "", "compose_ok": True})
    assert dispatch._environment_note() == dispatch.ENVIRONMENT_NOTE
    _status(tmp_path, monkeypatch, {"rig_ok": True, "first_red": "", "compose_ok": False})
    note = dispatch._environment_note()
    assert note.startswith(dispatch.ENVIRONMENT_NOTE) and "compose` is not working" in note


def test_note_names_the_first_red_step_in_plain_words(tmp_path, monkeypatch):
    _status(tmp_path, monkeypatch, {
        "rig_ok": False,
        "first_red": "the engine cannot create a user namespace (uid_map: EPERM)"})
    note = dispatch._environment_note()
    assert note.startswith(dispatch.ENVIRONMENT_NOTE)
    assert "**Nested containers are unavailable on this host**" in note
    assert "uid_map: EPERM" in note
    assert "not discoveries" in note                # the steward rule, restated
    assert "docker compose" in note
    _status(tmp_path, monkeypatch, {"rig_ok": False})
    assert "probe is red" in dispatch._environment_note()


def test_every_stage_prompt_carries_the_note_after_its_playbook(tmp_path):
    m = mission()
    note = dispatch.ENVIRONMENT_NOTE + dispatch.NESTED_ENGINE_UNAVAILABLE_NOTE.format(why="probe red")
    for build in (
        lambda **kw: onboard_prompt("ID", m, **kw),
        lambda **kw: plan_prompt("ID", m, **kw),
        lambda **kw: execute_prompt("ID", m, "repo", pr_instructions="", **kw),
        lambda **kw: review_prompt("ID", m, **kw),
    ):
        plain = build()
        assert "nested containers are unavailable" not in plain
        out = build(environment_note=note)
        assert note in out
        # after the mission brief, before the code-owned epilogues
        assert out.index(note) > out.index(m.title)
        if HUMAN_HANDOFF in out:
            assert out.index(note) < out.index(HUMAN_HANDOFF)
        # a per-PMO template override without any placeholder still gets it
        out = build(playbook="custom playbook {key}", environment_note=note)
        assert "custom playbook" in out and note in out


def test_size_report_counts_the_environment_note(tmp_path):
    from devcake.prompts import PROMPT_MAX_BYTES
    big = "x" * (PROMPT_MAX_BYTES + 10)
    line = dispatch.prompt_size_report(big, {"identity": "i", "environment note": "n" * 300})
    assert line and "environment note 300 B" in line


def test_dispatch_reads_the_status_without_a_manager(tmp_path, monkeypatch):
    mgr = make_mission_manager(tmp_path)
    _status(tmp_path, monkeypatch, {"rig_ok": False, "first_red": "x"})
    assert "unavailable" in dispatch._environment_note()
    assert mgr is not None
