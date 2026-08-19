"""Dev Type entrypoint script (plan slice 3) + launch composition (CAKE-63).

Public seams:
  DevType.dev_entrypoint — complete script; empty = dialect argv.
  composed_launch(...) — argv composition only; additive setup is NOT
    inlined here (that hole swallowed failures without set -e). Additive
    lines run via run_mcp_setup before launch; override scripts are the
    process and must be fail-closed (set -e). Empty-script + session_id
    yields resume dialect argv (CAKE-62 honesty without the prelude).
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


def test_additive_script_must_not_be_inlined_into_bash():
    """CAKE-63: embedding setup into bash -c without set -e let a failing
    line reach exec. Additive lines belong to run_mcp_setup; composed_launch
    takes dialect-only argv when override is off."""
    launch = _composed_launch()
    with pytest.raises(ValueError, match="run_mcp_setup"):
        launch(
            "grok-build", "Reply ACK\n", plan_mode=False,
            model="stub-model", extra=(),
            script="false\ntouch MARKER", override=False)


def test_additive_empty_script_is_dialect_only():
    """After run_mcp_setup succeeds, entrypoint launches with script=''."""
    launch = _composed_launch()
    got = launch(
        "grok-build", "Reply ACK\n", plan_mode=False,
        model="stub-model", extra=(), script="", override=False)
    assert got[0] == "grok"
    assert got[:4] != ["bash", "--noprofile", "--norc", "-c"]


def test_override_uses_only_the_operator_script_fail_closed():
    """Override: operator script is the process; set -e so a failing line
    aborts instead of continuing."""
    launch = _composed_launch()
    script = "my-cli --serve"
    got = launch(
        "grok-build", "Reply ACK\n", plan_mode=False,
        model="stub-model", extra=(), script=script, override=True)
    assert got[:4] == ["bash", "--noprofile", "--norc", "-c"]
    body = got[-1]
    assert body.startswith("set -e\n") or "\nset -e\n" in f"\n{body}"
    assert "my-cli --serve" in body
    assert "grok" not in body


def test_override_without_a_script_is_refused():
    launch = _composed_launch()
    with pytest.raises(ValueError, match="override"):
        launch(
            "grok-build", "Reply ACK\n", plan_mode=False,
            model="stub-model", extra=(), script="", override=True)


def test_override_failing_line_exits_nonzero(tmp_path):
    """Fail-closed override: a false line must not reach later commands."""
    import subprocess
    launch = _composed_launch()
    marker = tmp_path / "marker"
    got = launch(
        "grok-build", "x", plan_mode=False, model="", extra=(),
        script=f"false\ntouch {marker}", override=True)
    proc = subprocess.run(
        got, cwd=tmp_path, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=15)
    assert proc.returncode != 0
    assert not marker.exists()


# ── CAKE-62 + CAKE-63: resume through empty-script composed_launch ───────────


def test_additive_with_session_id_still_refuses_inline_prelude():
    """Resume must not reintroduce the no-set-e bash prelude around setup."""
    launch = _composed_launch()
    with pytest.raises(ValueError, match="run_mcp_setup"):
        launch(
            "grok-build", "NUDGE", plan_mode=False, model="stub-model",
            extra=(), script="claude mcp add x -- y", override=False,
            session_id="SID")


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
    body = got[-1]
    assert body.startswith("set -e\n") or "\nset -e\n" in f"\n{body}"
    assert "my-cli --serve" in body
    assert "grok" not in body
    assert "-r" not in body
    assert "SID" not in body


def test_entrypoint_continuations_pass_empty_additive_script():
    """Once-before-loop setup: relaunches must use launch_script (empty when
    not override), never re-embed the operator script into composed_launch."""
    src = (_images_common_root() / "dev_entrypoint.py").read_text()
    assert 'launch_script = script if override else ""' in src
    assert src.count("script=launch_script") >= 2  # initial + continuation(s)
    # No composed_launch call may pass the raw operator script (would either
    # double-run setup or raise on the additive refuse).
    assert "script=script," not in src


# ── CAKE-123: missing resume dialect degrades to fresh (never crash) ─────────


def test_composed_launch_raises_when_dialect_has_no_resume():
    """Chokepoint stays fail-closed for direct callers (pi has no resume)."""
    launch = _composed_launch()
    with pytest.raises(ValueError, match="no resume dialect"):
        launch(
            "pi", "NUDGE", plan_mode=False, model="", extra=(),
            script="", override=False, session_id="SID")


def test_missing_resume_dialect_degrades_to_none():
    """Resume arm helper: missing resume dialect → None (then fresh relaunch)."""
    helper = _composed_launch_resume_or_none()
    assert helper(
        "pi", "NUDGE", plan_mode=False, model="", extra=(),
        script="", override=False, session_id="SID") is None


def test_resume_or_none_still_composes_supported_resume():
    helper = _composed_launch_resume_or_none()
    resume = _harness_resume_argv()
    prompt = "NUDGE"
    sid = "SID"
    got = helper(
        "grok-build", prompt, plan_mode=False, model="stub-model",
        extra=(), script="", override=False, session_id=sid)
    assert got == resume(
        "grok-build", sid, prompt, model="stub-model")


def test_resume_or_none_reraises_unrelated_valueerror():
    helper = _composed_launch_resume_or_none()
    with pytest.raises(ValueError, match="run_mcp_setup"):
        helper(
            "grok-build", "NUDGE", plan_mode=False, model="stub-model",
            extra=(), script="false", override=False, session_id="SID")


def test_entrypoint_resume_arm_wires_missing_dialect_to_fresh():
    """Resume arm must use the degrade helper and still fall through to fresh."""
    src = (_images_common_root() / "dev_entrypoint.py").read_text()
    assert "composed_launch_resume_or_none" in src
    assert 'mode = "fresh"' in src
    assert "degrade to fresh, never crash" in src


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


def _composed_launch_resume_or_none():
    import os
    os.environ.setdefault("DEVCAKE_RUN_ID", "T-LAUNCH")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6399/0")
    os.environ.setdefault("REDIS_USER", "t")
    os.environ.setdefault("REDIS_PASSWORD", "t")
    _images_common_root()
    from devcake_dev.harness.launch import composed_launch_resume_or_none
    return composed_launch_resume_or_none


def _harness_resume_argv():
    _images_common_root()
    from devcake_dev.harness.argv import harness_resume_argv
    return harness_resume_argv
