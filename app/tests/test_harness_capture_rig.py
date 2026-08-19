"""Capture rig must fail closed — no Claude default, no `other` alias."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DEVCAKE_RUN_ID", "test-capture-rig")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

_CAPTURE_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "scripts" / "harness_capture",
    Path("/srv/repo-scripts/harness_capture"),
]
CAPTURE = next((p for p in _CAPTURE_CANDIDATES if p.is_dir()), None)

_COMMON_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "images" / "common",
    Path("/srv/images/common"),
]
COMMON = next((p for p in _COMMON_CANDIDATES if p.is_dir()), None)


def test_capture_script_has_no_claude_fall_through():
    assert CAPTURE is not None, "scripts/harness_capture missing"
    src = (CAPTURE / "in_container.py").read_text()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "claude"):
            offenders.append(f"line {node.lineno}: .get(..., 'claude')")
        if (isinstance(node, ast.Attribute)
                and node.attr == "claude_text_dump"):
            offenders.append(f"line {node.lineno}: claude_text_dump")
        if (isinstance(node, ast.Constant)
                and node.value == "other"):
            offenders.append(f"line {node.lineno}: harness choice 'other'")
    assert not offenders, (
        "capture rig still falls through to Claude:\n  "
        + "\n  ".join(offenders))


def test_capture_cli_name_and_dump_fail_closed_on_unknown_id():
    assert CAPTURE is not None and COMMON is not None
    for root in (str(COMMON), str(CAPTURE)):
        if root not in sys.path:
            sys.path.insert(0, root)
    import in_container as rig
    with pytest.raises(ValueError, match="unknown harness"):
        rig.cli_name("something-new")
    with pytest.raises(ValueError, match="unknown harness"):
        rig.capture_dump("something-new", "", workspace=Path("/tmp"))


def _load_rig():
    assert CAPTURE is not None and COMMON is not None
    for root in (str(COMMON), str(CAPTURE)):
        if root not in sys.path:
            sys.path.insert(0, root)
    import in_container as rig
    return rig


def test_grade_capture_exit0_auth_fault_wires_classify_nonzero_exit():
    """Exit-0 + in-band fault + distinctive api_status must match production.

    Independent expected value: classify_nonzero_exit("", fault, 401) →
    (12, "DEV_AUTH") — pinned in test_entrypoint_fault. The capture rig must
    wire through that chokepoint, not hand-roll (15, "").
    """
    rig = _load_rig()
    ep, _path = rig.load_entrypoint()
    fault = {
        "reason": ep.FAULT_TERMINAL_ERROR,
        "detail": "harness reported a terminal error: 401",
    }
    observed = rig.grade_capture(
        ep, exit_code=0, stderr="", fault=fault, api_status=401)
    assert observed == (12, "DEV_AUTH")
    assert observed == ep.classify_nonzero_exit("", fault, 401)
