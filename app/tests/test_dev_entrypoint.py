"""Dev Type entrypoint script (plan slice 3).

Public seams:
  DevType.dev_entrypoint — complete script; empty = dialect argv.
  composed_launch(...) — prod and probe exec the same argv list.
Legacy YAML key mcp_setup_commands joins into the script on read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from devcake.config import DevType


def test_legacy_mcp_setup_commands_become_the_entrypoint_script():
    dt = DevType(
        name="d", harness_template="grok-build",
        mcp_setup_commands=["claude mcp add x -- y", "export PATH"])
    assert dt.dev_entrypoint == "claude mcp add x -- y\nexport PATH"
    assert dt.mcp_setup_commands == [
        "claude mcp add x -- y", "export PATH"]


def test_explicit_dev_entrypoint_wins_over_legacy_key():
    dt = DevType(
        name="d", harness_template="grok-build",
        dev_entrypoint="exec grok",
        mcp_setup_commands=["ignored"])
    assert dt.dev_entrypoint == "exec grok"


def test_override_defaults_off():
    dt = DevType(name="d", harness_template="grok-build")
    assert dt.override_harness_adapter is False


def test_empty_entrypoint_is_the_dialect_argv():
    launch = _composed_launch()
    got = launch(
        "grok-build", "Reply ACK\n", plan_mode=False,
        model="stub-model", extra=(), script="")
    assert got[0] == "grok"
    assert "Reply ACK\n" in got


def test_additive_script_runs_then_execs_the_dialect():
    """Unchecked override: operator lines are setup, then the harness CLI."""
    launch = _composed_launch()
    got = launch(
        "grok-build", "Reply ACK\n", plan_mode=False,
        model="stub-model", extra=(),
        script="claude mcp add x -- y", override=False)
    assert got[:4] == ["bash", "--noprofile", "--norc", "-c"]
    body = got[-1]
    assert "claude mcp add x -- y" in body
    assert "exec " in body
    assert " grok " in f" {body} " or body.startswith("exec grok") or "\nexec grok" in body


def test_override_uses_only_the_operator_script():
    launch = _composed_launch()
    script = "my-cli --serve"
    got = launch(
        "grok-build", "Reply ACK\n", plan_mode=False,
        model="stub-model", extra=(), script=script, override=True)
    assert got[:4] == ["bash", "--noprofile", "--norc", "-c"]
    assert got[-1] == script
    assert "grok" not in got[-1]


def test_override_without_a_script_is_refused():
    launch = _composed_launch()
    with pytest.raises(ValueError, match="override"):
        launch(
            "grok-build", "Reply ACK\n", plan_mode=False,
            model="stub-model", extra=(), script="", override=True)


# ── CAKE-62: resume through composed_launch (session_id) ─────────────────────


def test_additive_prelude_with_session_id_execs_resume_argv():
    """MCP/entrypoint prelude must wrap resume dialect argv, not fresh argv."""
    import shlex

    launch = _composed_launch()
    resume = _harness_resume_argv()
    prompt = "NUDGE"
    sid = "SID"
    script = "claude mcp add x -- y"
    got = launch(
        "grok-build", prompt, plan_mode=False, model="stub-model",
        extra=(), script=script, override=False, session_id=sid)
    expected_resume = resume(
        "grok-build", sid, prompt, model="stub-model")
    fresh = launch(
        "grok-build", prompt, plan_mode=False, model="stub-model",
        extra=(), script=script, override=False)
    assert got[:4] == ["bash", "--noprofile", "--norc", "-c"]
    quoted = " ".join(shlex.quote(p) for p in expected_resume)
    assert got[-1] == f"{script}\nexec {quoted}\n"
    # must not equal a fresh argv composition (no -r SID)
    assert got != fresh
    assert f"-r {shlex.quote(sid)}" not in fresh[-1]
    assert "-r" in got[-1] and sid in got[-1]


def test_empty_script_with_session_id_equals_harness_resume_argv():
    launch = _composed_launch()
    resume = _harness_resume_argv()
    prompt = "NUDGE"
    sid = "SID"
    got = launch(
        "grok-build", prompt, plan_mode=False, model="stub-model",
        extra=(), script="", override=False, session_id=sid)
    assert got == resume(
        "grok-build", sid, prompt, model="stub-model")


def test_override_with_session_id_still_uses_only_operator_script():
    """Under override the opaque script is the process; session_id is ignored."""
    launch = _composed_launch()
    script = "my-cli --serve"
    got = launch(
        "grok-build", "NUDGE", plan_mode=False, model="stub-model",
        extra=(), script=script, override=True, session_id="SID")
    assert got[:4] == ["bash", "--noprofile", "--norc", "-c"]
    assert got[-1] == script
    assert "grok" not in got[-1]
    assert "-r" not in got[-1]
    assert "SID" not in got[-1]


def _images_common_root() -> Path:
    roots = [
        Path(__file__).resolve().parents[2] / "images" / "common",
        Path("/srv/images/common"),
    ]
    root = next((p for p in roots if (p / "devcake_dev").is_dir()), None)
    assert root is not None, "images/common missing"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def _composed_launch():
    import os
    os.environ.setdefault("DEVCAKE_RUN_ID", "T-LAUNCH")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    os.environ.setdefault("REDIS_USER", "t")
    os.environ.setdefault("REDIS_PASSWORD", "t")
    _images_common_root()
    from devcake_dev.harness.launch import composed_launch
    return composed_launch


def _harness_resume_argv():
    _images_common_root()
    from devcake_dev.harness.argv import harness_resume_argv
    return harness_resume_argv
