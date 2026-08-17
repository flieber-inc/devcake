"""Hermetic drift-gate receipt (PLAN_CLI_PINS Slice 0b).

Public seam: compile_receipt grades observations against the matrix.
Independent expected values are the fixture-meta literals, not fault()
internals and not “must be DEV_AUTH.”
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_PROBE_CANDIDATES = [
    Path(__file__).resolve().parents[2] / "scripts" / "harness_probe",
    Path("/srv/repo-scripts/harness_probe"),
]
PROBE = next((p for p in _PROBE_CANDIDATES if p.is_dir()), None)


def _load_probe():
    assert PROBE is not None, (
        "scripts/harness_probe missing — bind scripts → /srv/repo-scripts")
    root = str(PROBE.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def _compile():
    _load_probe()
    from harness_probe.receipt import compile_receipt
    return compile_receipt


# CAPTURES AUTH row for grok_http_401 — not the July sidecar measurement.
GROK_401 = {"exit": 12, "class": "DEV_AUTH", "reason": "terminal_error"}


def test_planted_mismatch_fails_the_row_and_the_receipt():
    compile_receipt = _compile()
    planted = {"exit": 15, "class": "DEV_HARNESS_FAULT", "reason": "empty_completion"}
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={"http_401": {"observed": planted}},
    )
    row = next(r for r in rec["rows"] if r["name"] == "http_401")
    assert row["required"] is True
    assert row["expected"] == GROK_401
    assert row["observed"] == planted
    assert row["status"] == "fail"
    assert rec["ok"] is False


def test_required_row_skipped_is_not_ok():
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={"http_401": {"skipped": "stub unreachable"}},
    )
    row = next(r for r in rec["rows"] if r["name"] == "http_401")
    assert row["required"] is True
    assert row["status"] == "skipped"
    assert row["detail"] == "stub unreachable"
    assert "observed" not in row
    assert rec["ok"] is False


def test_required_row_error_is_not_ok():
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={"http_401": {"error": "probe crashed mid-row"}},
    )
    row = next(r for r in rec["rows"] if r["name"] == "http_401")
    assert row["status"] == "error"
    assert row["detail"] == "probe crashed mid-row"
    assert "observed" not in row
    assert rec["ok"] is False


def test_matching_observation_passes_the_row_and_the_receipt():
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={
            "healthy": {"observed": {
                "exit": 11, "class": "", "reason": None}},
            "http_401": {"observed": dict(GROK_401)},
            "empty": {"observed": {
                "exit": 15, "class": "DEV_HARNESS_FAULT",
                "reason": "terminal_error"}},
            "plan_mode": {"flag_accepted": True},
            "resume": {"observed": {
                "exit": 11, "class": "", "reason": None}},
        },
    )
    row = next(r for r in rec["rows"] if r["name"] == "http_401")
    assert row["status"] == "pass"
    assert row["expected"] == GROK_401
    assert row["observed"] == GROK_401
    assert rec["ok"] is True
    # Host receipts must stamp gated so app staffing cannot green-wash
    # a fabricated {ok: True} without the probe path.
    assert rec["gated"] is True

def test_every_house_pin_has_the_same_probe_rows():
    """Compile+probe grades the same five names for every registry template."""
    _load_probe()
    from harness_probe.matrix import matrix_for, matrix_templates
    from devcake.house_pins import HOUSE_PINS

    assert matrix_templates() == frozenset(HOUSE_PINS)
    names = ("healthy", "http_401", "empty", "plan_mode", "resume")
    for template in HOUSE_PINS:
        got = tuple(spec.name for spec in matrix_for(template))
        assert got == names, template
        by = {spec.name: spec for spec in matrix_for(template)}
        assert by["healthy"].required is True
        assert by["empty"].required is True
        assert by["plan_mode"].required is True


def test_intentional_probe_skips_stay_visible_and_non_required():
    """Matrix honesty: optional rows must not silently pass as required."""
    _load_probe()
    from harness_probe.matrix import matrix_for

    claude = {s.name: s for s in matrix_for("claude-code")}
    assert claude["http_401"].required is False
    assert "claude_http_401" in claude["http_401"].skip_reason

    for template in ("pi", "opencode", "qwen-code"):
        resume = {s.name: s for s in matrix_for(template)}["resume"]
        assert resume.required is False, template
        assert "RESUME_SPECS" in resume.skip_reason, template


def test_loader_refuses_the_working_tree_entrypoint(tmp_path):
    """Capture rig is /srv-first. The probe must not grade that path."""
    _load_probe()
    from harness_probe.entrypoint import BAKED_ENTRYPOINT, load_baked_entrypoint
    assert BAKED_ENTRYPOINT == "/dev_entrypoint.py"
    srv = tmp_path / "srv" / "images" / "common" / "dev_entrypoint.py"
    srv.parent.mkdir(parents=True)
    srv.write_text("SENTINEL = 'srv'\n")
    with pytest.raises(ValueError, match="baked"):
        load_baked_entrypoint(path=str(srv))
    baked = tmp_path / "dev_entrypoint.py"
    baked.write_text("SENTINEL = 'baked'\n")
    mod = load_baked_entrypoint(path=str(baked))
    assert mod.SENTINEL == "baked"


def test_plan_mode_row_fails_when_the_binary_rejects_the_flag(tmp_path, monkeypatch):
    """Row 4 must exec the composed argv. argv() returning a list is not enough."""
    monkeypatch.setenv("DEVCAKE_RUN_ID", "PROBE-1")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    monkeypatch.setenv("REDIS_USER", "probe")
    monkeypatch.setenv("REDIS_PASSWORD", "probe")
    _load_probe()
    from harness_probe.plan_mode import composed_plan_argv, run_flag_accept
    fake = tmp_path / "grok"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'unknown option: --permission-mode' >&2\n"
        "exit 2\n"
    )
    fake.chmod(0o755)
    argv = composed_plan_argv("grok-build", prompt="probe")
    assert argv[0] == "grok"
    assert "--permission-mode" in argv and "plan" in argv
    argv = [str(fake), *argv[1:]]
    report = run_flag_accept(argv)
    assert report == {"flag_accepted": False}
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={"plan_mode": report},
    )
    row = next(r for r in rec["rows"] if r["name"] == "plan_mode")
    assert row["required"] is True
    assert row["status"] == "fail"
    assert rec["ok"] is False


def test_probe_writes_receipt_and_exits_nonzero_when_not_ok(tmp_path):
    _load_probe()
    from harness_probe.probe import run_probe

    def run_row(spec):
        if spec.name == "http_401":
            return {"observed": {
                "exit": 15, "class": "DEV_HARNESS_FAULT",
                "reason": "empty_completion"}}
        if spec.name == "plan_mode":
            return {"flag_accepted": True}
        return {"error": f"unexpected {spec.name}"}

    rec, code = run_probe(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        out_dir=tmp_path,
        run_row=run_row,
    )
    assert code != 0
    assert rec["ok"] is False
    written = tmp_path / "grok-build@0.2.112.json"
    assert written.is_file()
    assert written.read_text() == json.dumps(rec, indent=2) + "\n"
    # uid is the writer — host-side test cannot assert 1000; the in-image
    # verb does. File exists under the receipts dir.
    assert written.stat().st_uid == os.getuid()


def test_claude_http_401_is_not_required_and_does_not_block_ok():
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="claude-code",
        cli_version="2.1.229",
        reports={
            "healthy": {"observed": {"exit": 11, "class": "", "reason": None}},
            "empty": {"observed": {
                "exit": 15, "class": "DEV_HARNESS_FAULT",
                "reason": "empty_completion"}},
            "plan_mode": {"flag_accepted": True},
            "resume": {"observed": {"exit": 11, "class": "", "reason": None}},
        },
    )
    row = next(r for r in rec["rows"] if r["name"] == "http_401")
    assert row["required"] is False
    assert row["status"] == "skipped"
    assert "never been stub-driven" in row["detail"]
    assert rec["ok"] is True


def test_grok_matrix_expected_literals_match_captures_intended():
    """The matrix maps CAPTURES intended verdicts. NO_FAULT → exit 11
    (the entrypoint's no-fault classify). AUTH stays exit 12 DEV_AUTH."""
    _load_probe()
    from harness_probe.matrix import matrix_for
    from test_harness_captures import AUTH, CAPTURES, NO_FAULT, TERMINAL
    rows = {s.name: s for s in matrix_for("grok-build")}
    intended = {name: verdict for name, verdict in CAPTURES
                if name.startswith("grok_")}
    assert intended["grok_http_401"] is AUTH
    assert rows["http_401"].expected == {
        "exit": AUTH[1], "class": AUTH[2], "reason": "terminal_error"}
    assert rows["http_401"].expected == GROK_401
    assert intended["grok_healthy"] is NO_FAULT
    assert rows["healthy"].expected == {
        "exit": 11, "class": "", "reason": None}
    assert intended["grok_empty"] is TERMINAL
    assert rows["empty"].expected == {
        "exit": TERMINAL[1], "class": TERMINAL[2],
        "reason": "terminal_error"}


def test_required_row_absent_is_not_ok():
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={},
    )
    row = next(r for r in rec["rows"] if r["name"] == "http_401")
    assert row["required"] is True
    assert row["status"] == "error"
    assert rec["ok"] is False
    # Reader-side: a receipt that dropped the row is also not-ok.
    from harness_probe.receipt import receipt_ok
    stripped = {**rec, "rows": [r for r in rec["rows"] if r["name"] != "http_401"]}
    assert receipt_ok(stripped) is False


def _attribute():
    _load_probe()
    from harness_probe.cause import attribute
    return attribute


def test_empty_journal_is_an_aim_miss():
    """CLI never reached the stub lane — leftover env or a stale recipe."""
    attribute = _attribute()
    got = attribute(
        row="healthy",
        expected={"exit": 11, "class": "", "reason": None},
        observed={"exit": 15, "class": "DEV_HARNESS_FAULT",
                  "reason": "terminal_error"},
        journal_hits=(),
    )
    assert got == "aim"


def test_launch_refused_is_an_aim_miss():
    attribute = _attribute()
    got = attribute(
        row="healthy",
        expected={"exit": 11, "class": "", "reason": None},
        observed=None,
        journal_hits=(),
        launched=False,
    )
    assert got == "aim"


def test_hit_on_the_right_lane_with_wrong_classify_is_dialect():
    """We served the tape; fault/classify disagreed with the matrix."""
    attribute = _attribute()
    got = attribute(
        row="healthy",
        expected={"exit": 11, "class": "", "reason": None},
        observed={"exit": 15, "class": "DEV_HARNESS_FAULT",
                  "reason": "empty_completion"},
        journal_hits=({
            "scenario": "healthy",
            "path": "/v1/chat/completions",
        },),
    )
    assert got == "dialect"


def test_hit_on_the_wrong_protocol_is_stub():
    attribute = _attribute()
    got = attribute(
        row="healthy",
        expected={"exit": 11, "class": "", "reason": None},
        observed={"exit": 15, "class": "DEV_HARNESS_FAULT",
                  "reason": "terminal_error"},
        journal_hits=({
            "scenario": "healthy",
            "path": "/v1/embeddings",
        },),
    )
    assert got == "stub"


def test_http_401_tape_classified_auth_is_auth():
    """The 401 lane proving classify still works — not a failure cause."""
    attribute = _attribute()
    got = attribute(
        row="http_401",
        expected={"exit": 12, "class": "DEV_AUTH", "reason": "terminal_error"},
        observed={"exit": 12, "class": "DEV_AUTH", "reason": "terminal_error"},
        journal_hits=({
            "scenario": "http_401",
            "path": "/v1/chat/completions",
        },),
    )
    assert got == "auth"


def test_failed_classify_row_persists_cause_on_the_receipt():
    compile_receipt = _compile()
    rec = compile_receipt(
        digest="sha256:test",
        template="grok-build",
        cli_version="0.2.112",
        reports={"healthy": {"observed": {
            "exit": 15, "class": "DEV_HARNESS_FAULT",
            "reason": "terminal_error"}}},
        journal_hits=(),
    )
    row = next(r for r in rec["rows"] if r["name"] == "healthy")
    assert row["status"] == "fail"
    assert row["cause"] == "aim"
    assert row["evidence"]["journal_hits"] == 0
