"""Harness fault detection, failure forensics and misplaced-result recovery
(ADR-0018) — the container half.

Claude cases run against REAL captured streams in fixtures/harness_streams/.
The codex and grok cases here stay synthetic on purpose — they vary one field at
a time to pin an arm's boundary; the real codex-cli 0.146.0 and grok 0.2.112
captures are asserted end-to-end in test_harness_captures.py.
"""

import importlib.util
import json
import os
import signal
import stat
import time
from pathlib import Path

import pytest

# host checkout: repo/app/tests → repo/images/common; app container: /srv/tests
# → /srv/images/common (read-only compose mount)
_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

# distinct module name — test_entrypoint_render/_classify/_mcp load the same
# file under their own names in this pytest process
spec = importlib.util.spec_from_file_location("dev_entrypoint_fault", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

FIXTURES = Path(__file__).parent / "fixtures" / "harness_streams"


def fx(name):
    return (FIXTURES / name).read_text()


def J(obj):
    return json.dumps(obj)


def reason(fault):
    return fault["reason"] if fault else None


# ── claude: the real captures ────────────────────────────────────────────────

@pytest.mark.parametrize("name,harness_exit,expected", [
    ("claude_empty_completion.jsonl", 0, ep.FAULT_EMPTY_COMPLETION),   # empty completion
    ("claude_api_error_400.jsonl", 1, ep.FAULT_TERMINAL_ERROR),
    ("claude_max_turns.jsonl", 1, ep.FAULT_TURN_BUDGET),
    ("claude_aborted_streaming.jsonl", 1, ep.FAULT_TERMINAL_ERROR),
])
def test_real_captures_classify(name, harness_exit, expected):
    out = fx(name)
    assert reason(ep.harness_fault("claude-code", out, harness_exit,
                                   dump=ep.claude_text_dump(out))) == expected


def test_empty_completion_exits_zero_and_is_a_fault():
    """Harness exited 0 with success subtype but no content → fault (was exit 11)."""
    out = fx("claude_empty_completion.jsonl")
    ev = ep.claude_result_event(out)
    assert ev["subtype"] == "success" and not ev.get("is_error")
    assert reason(ep.harness_fault("claude-code", out, 0)) == ep.FAULT_EMPTY_COMPLETION


def test_turn_budget_wins_over_the_error_flags_it_cooccurs_with():
    """claude_max_turns carries is_error:true AND subtype:error_max_turns, so
    ordering is what keeps it out of the correlation-eligible bucket."""
    ev = ep.claude_result_event(fx("claude_max_turns.jsonl"))
    assert ev["is_error"] is True
    assert reason(ep.harness_fault("claude-code", fx("claude_max_turns.jsonl"), 1)) \
        == ep.FAULT_TURN_BUDGET


def test_api_error_is_a_fault_despite_subtype_success():
    """subtype is matched on the "error" PREFIX only — the 400 capture proves
    subtype:"success" can accompany is_error:true."""
    ev = ep.claude_result_event(fx("claude_api_error_400.jsonl"))
    assert ev["subtype"] == "success" and ev["is_error"] is True
    assert reason(ep.harness_fault("claude-code", fx("claude_api_error_400.jsonl"), 1)) \
        == ep.FAULT_TERMINAL_ERROR


# ── claude: conservatism (the failures that would do real damage) ────────────

def test_refusal_is_not_a_fault():
    out = "\n".join([
        J({"type": "assistant",
           "message": {"content": [{"type": "text", "text": "I can't help with that."}]}}),
        J({"type": "result", "subtype": "success", "is_error": False,
           "terminal_reason": "completed", "result": "I can't help with that.",
           "num_turns": 2, "usage": {"output_tokens": 0}})])
    assert ep.harness_fault("claude-code", out, 0, dump="I can't help with that.") is None


def test_tool_only_run_with_empty_final_message_is_not_a_fault():
    """Token threshold would misclassify this; structural tool_use does not."""
    out = "\n".join(
        [J({"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                     "input": {}}]}})] * 15
        + [J({"type": "result", "subtype": "success", "is_error": False,
              "terminal_reason": "completed", "result": "", "num_turns": 16,
              "usage": {"output_tokens": 0}})])
    assert ep.harness_fault("claude-code", out, 0, dump="") is None


def test_stderr_noise_never_changes_the_verdict():
    """stderr is not an input to the fault predicate."""
    out = fx("claude_empty_completion.jsonl")
    assert reason(ep.harness_fault("claude-code", out, 0)) == \
        reason(ep.harness_fault("claude-code", out, 0, dump=""))


def test_truncated_stream_is_no_terminal_event_not_empty_completion():
    out = J({"type": "assistant",
             "message": {"content": [{"type": "text", "text": "working"}]}})
    assert reason(ep.harness_fault("claude-code", out, 0)) == ep.FAULT_NO_TERMINAL_EVENT


def test_blob_output_format_override_is_still_parsed():
    """EXTRA_ARGS can override stream-json back to a single JSON object."""
    out = J({"type": "result", "subtype": "success", "result": "done",
             "usage": {"output_tokens": 40}})
    assert ep.harness_fault("claude-code", out, 0, dump="done") is None


@pytest.mark.parametrize("usage", ["none", [1, 2], 7])
def test_non_dict_usage_does_not_abort_the_predicate(usage):
    """`x or {}` rescues null/[]/{} but not a truthy non-dict; an AttributeError
    here would kill fault detection for healthy runs too."""
    out = J({"type": "result", "subtype": "success", "result": "hi", "usage": usage})
    assert ep.harness_fault("claude-code", out, 0, dump="hi") is None


def _claude_ok(**result):
    """A healthy one-turn stream, with the result event's fields overridable —
    so a single field can be varied without any other arm firing."""
    ev = {"type": "result", "subtype": "success", "is_error": False,
          "terminal_reason": "completed", "result": "done", "num_turns": 2,
          "usage": {"output_tokens": 12}}
    ev.update(result)
    return "\n".join([
        J({"type": "assistant",
           "message": {"content": [{"type": "text", "text": "done"}]}}),
        J(ev)])


@pytest.mark.parametrize("status", [None, 200, 204, "401"])
def test_benign_api_error_status_is_not_a_terminal_error(status):
    """A VALUE test, not a presence test: every capture carries the field (null
    on the healthy incident), and a 2xx or a stringified status from a future CLI
    version must not manufacture a fault."""
    assert ep.harness_fault("claude-code", _claude_ok(api_error_status=status), 1,
                            dump="done") is None


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_http_error_api_status_is_a_terminal_error(status):
    assert reason(ep.harness_fault("claude-code", _claude_ok(api_error_status=status),
                                   1, dump="done")) == ep.FAULT_TERMINAL_ERROR


def test_unknown_terminal_reason_is_not_a_fault():
    """Bad-value allowlist, never a good-value denylist — the terminal_reason
    enum is not fully known, so an unrecognized value stays "not a fault"."""
    assert ep.CLAUDE_FAULT_TERMINAL_REASONS == frozenset({"api_error"})
    assert ep.harness_fault("claude-code", _claude_ok(terminal_reason="wrapped_up"),
                            0, dump="done") is None


def test_whitespace_only_completion_is_an_empty_completion():
    """The case no token threshold catches: the stop token costs tokens, so
    modelUsage is non-zero (fixtures README). Only the structural signal —
    `.strip()` on the final message AND on every text block — sees that nothing
    was actually said."""
    out = "\n".join([
        J({"type": "assistant",
           "message": {"content": [{"type": "text", "text": "  \n "}]}}),
        J({"type": "result", "subtype": "success", "is_error": False,
           "terminal_reason": "completed", "result": " \n", "num_turns": 1,
           "usage": {"output_tokens": 1}})])
    assert reason(ep.harness_fault("claude-code", out, 0, dump=" ")) == \
        ep.FAULT_EMPTY_COMPLETION


def test_fault_detail_is_single_line_and_bounded():
    for name in ("claude_empty_completion.jsonl", "claude_api_error_400.jsonl",
                 "claude_max_turns.jsonl"):
        f = ep.harness_fault("claude-code", fx(name), 1)
        assert "\n" not in f["detail"] and len(f["detail"]) <= ep.FAULT_DETAIL_MAX


def test_turn_budget_detail_is_actionable():
    f = ep.harness_fault("claude-code", fx("claude_max_turns.jsonl"), 1)
    assert "--max-turns" in f["detail"] and "Assignments" in f["detail"]


# ── codex (synthetic) ────────────────────────────────────────────────────────

def _codex(*events):
    return "\n".join(J(e) for e in events)


TURN_OK = {"type": "turn.completed", "usage": {"output_tokens": 120}}
MSG = {"type": "item.completed", "item": {"item_type": "agent_message", "text": "d"}}
TOOL = {"type": "item.completed",
        "item": {"item_type": "command_execution", "command": "ls"}}


def test_codex_tool_only_run_is_not_a_fault():
    out = _codex({"type": "turn.started"}, *([TOOL] * 15),
                 {"type": "turn.completed", "usage": {"output_tokens": 0}})
    assert ep.harness_fault("codex", out, 0) is None


def test_codex_trailing_error_after_a_completed_turn_is_not_a_fault():
    """turn.completed IS codex's success signal — a later error ("failed to
    persist rollout file") did not stop the agent working."""
    out = _codex(MSG, TURN_OK, {"type": "error", "message": "failed to persist rollout"})
    assert ep.harness_fault("codex", out, 0) is None


def test_codex_error_with_no_completed_turn_is_a_fault():
    out = _codex({"type": "turn.started"}, {"type": "error", "message": "stream 500"})
    assert reason(ep.harness_fault("codex", out, 1)) == ep.FAULT_TERMINAL_ERROR


def test_codex_unknown_terminal_turn_event_is_a_fault():
    out = _codex({"type": "turn.started"}, {"type": "turn.failed", "error": {}})
    assert reason(ep.harness_fault("codex", out, 1)) == ep.FAULT_TERMINAL_ERROR


def test_codex_empty_completion_uses_the_raw_last_message_not_the_stdout_fallback():
    """`result_text` is overwritten with out[-4000:] on any parse failure, so
    the predicate must read the raw `-o` file instead."""
    out = _codex({"type": "turn.completed", "usage": {"output_tokens": 0}})
    assert reason(ep.harness_fault("codex", out, 0)) == ep.FAULT_EMPTY_COMPLETION
    assert ep.harness_fault("codex", out, 0, last_message="the answer") is None


# ── grok (synthetic) ─────────────────────────────────────────────────────────

def test_grok_activity_is_a_closed_set_of_event_types():
    """REVERSAL, on measured evidence: this used to assert that a `thought`-only
    run is not a fault. There is no `thought` event at 0.2.112 — the catalogue is
    exactly {text, end, max_turns_reached, error} (docs/08 §1, enumerated over
    all eleven grok captures) — so the counter was dead weight. Activity is now a
    closed set, like `_claude_activity`'s: counting an unrecognized event as work
    is exactly what made codex's `empty_completion` arm unreachable. grok emits
    no tool-call events at all, so the `grok export` transcript is what rescues a
    silent-but-productive run."""
    out = "\n".join([J({"type": "thought", "data": "thinking"}),
                     J({"type": "end", "stopReason": "stop"})])
    assert reason(ep.harness_fault("grok-build", out, 0)) == ep.FAULT_EMPTY_COMPLETION
    assert ep.harness_fault("grok-build", out, 0, prompt="P",
                            dump="## User\n\nP\n\n## Tools\n\n- Execute: ls") is None


def test_grok_silent_run_with_a_session_transcript_is_not_a_fault():
    out = J({"text": "", "stopReason": "stop"})
    assert ep.harness_fault("grok-build", out, 0, dump="a full export") is None


def test_grok_stop_reason_never_decides():
    """GROK_FAULT_STOP_REASONS is empty by design: the enum is unverified and
    calling a refusal a fault is the mistake that must not happen."""
    assert ep.GROK_FAULT_STOP_REASONS == frozenset()
    out = J({"text": "I can't help with that", "stopReason": "refusal"})
    assert ep.harness_fault("grok-build", out, 0) is None


def test_grok_producing_nothing_at_all_is_a_fault():
    out = J({"text": "", "stopReason": "stop"})
    assert reason(ep.harness_fault("grok-build", out, 0)) == ep.FAULT_EMPTY_COMPLETION


def test_unknown_harness_fault_fails_closed():
    """docs/16 H1: a typo must not silently run the Claude predicate."""
    out = fx("claude_empty_completion.jsonl")
    with pytest.raises(ValueError, match="unknown harness"):
        ep.harness_fault("something-new", out, 0)
    with pytest.raises(ValueError, match="unknown harness"):
        ep.harness_api_error_status("something-new", out)


# ── forensics ────────────────────────────────────────────────────────────────

def test_forensics_lists_the_out_dir(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "PLAN.md").write_text("x" * 10)
    (out_dir / "result.json").write_text("")
    info = ep.workspace_forensics(out_dir, 0, 100, 5, "warn")
    assert info["out_error"] is None and info["out_writable"] is True
    assert any(e.startswith("result.json:0") for e in info["out_listing"])
    assert info["harness_exit"] == 0 and info["stdout_bytes"] == 100


def test_forensics_reports_errno_instead_of_raising(tmp_path):
    info = ep.workspace_forensics(tmp_path / "nope", -9, 0, 0, "")
    assert "2" in info["out_error"] and info["harness_exit"] == -9


def test_forensics_payload_stays_small_under_pathological_input(tmp_path):
    """An entry COUNT cap is not enough: json.dumps escapes non-ASCII to
    \\uXXXX, so 20 accented 100-char names would serialize to ~12 KB."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for n in range(50):
        (out_dir / (f"{n:02d}" + "é" * 100)).write_text("x")
    info = ep.workspace_forensics(out_dir, 0, 0, 0, "e" * 5000)
    assert len(json.dumps(info)) < 2048
    assert any("more" in e for e in info["out_listing"])


# ── terminal evidence (ADR-0022 PR-1) ────────────────────────────────────────
# What the exit-11 artifact names about HOW the stream ended: "ended cleanly
# but early" (EndTurn) vs "just stopped" (null). Driven by the committed
# captures wherever one carries the shape.

def test_grok_terminal_evidence_names_the_narrate_and_stop_shape():
    """grok_loop_nocap is the measured exit-11 shape: exit 0, clean end event,
    stopReason EndTurn — the evidence must surface exactly that."""
    for name in ("grok_loop_nocap.jsonl", "grok_loop_cap30.jsonl"):
        ev = ep.terminal_evidence("grok-build", fx(name))
        assert ev["event"] == "end" and ev["stop_reason"] == "EndTurn"
        assert ev["num_turns"] == 16
    healthy = ep.terminal_evidence("grok-build", fx("grok_healthy.jsonl"))
    assert healthy["event"] == "end" and healthy["session_id"]


def test_grok_terminal_evidence_error_and_truncated_streams():
    err = ep.terminal_evidence("grok-build", fx("grok_empty.jsonl"))
    assert err["event"] == "error" and "empty response" in err["message"]
    # a stream that just stopped (text deltas, no terminal event) → None,
    # which serializes as `"terminal": null` — that absence IS the evidence
    assert ep.terminal_evidence(
        "grok-build", J({"type": "text", "data": "working…"})) is None
    assert ep.terminal_evidence("grok-build", "") is None


def test_claude_terminal_evidence_reads_the_result_event():
    ev = ep.terminal_evidence("claude-code", fx("claude_empty_completion.jsonl"))
    assert ev["event"] == "result" and ev["subtype"] == "success"
    assert ep.terminal_evidence("claude-code", J({"type": "assistant"})) is None


def test_codex_terminal_evidence_reads_turn_completed():
    ev = ep.terminal_evidence("codex", fx("codex_healthy.jsonl"))
    assert ev["event"] == "turn.completed"
    assert ep.terminal_evidence("codex", J({"type": "turn.started"})) is None


def test_terminal_evidence_never_raises_on_hostile_shapes():
    hostile = "\n".join([
        J({"type": "end", "stopReason": ["not", "a", "string"], "num_turns": {}}),
        J({"type": "turn.completed", "usage": "none"}),
        J({"type": "result", "subtype": 7, "usage": []}),
        "not json at all", J([1, 2, 3])])
    for harness in ("grok-build", "codex", "claude-code"):
        ep.terminal_evidence(harness, hostile)  # must not raise
    with pytest.raises(ValueError, match="unknown harness"):
        ep.terminal_evidence("unknown", hostile)


# ── misplaced result.json ────────────────────────────────────────────────────

def _ws(tmp_path):
    ws = tmp_path / "workspace"
    wd = ws / "repo" / "r"
    (ws / "out").mkdir(parents=True)
    wd.mkdir(parents=True)
    return ws, wd


def test_canonical_path_wins(tmp_path):
    ws, wd = _ws(tmp_path)
    (ws / "out" / "result.json").write_text("{}")
    (wd / "result.json").write_text("{}")
    path, note = ep.find_result_json(ws, wd, time.time() - 5)
    assert path == ws / "out" / "result.json" and note == ""


def test_recovers_a_stray_written_during_this_run(tmp_path):
    ws, wd = _ws(tmp_path)
    (wd / "result.json").write_text("{}")
    path, note = ep.find_result_json(ws, wd, time.time() - 5,
                                     runner=lambda *a, **k: type("R", (), {"returncode": 1})())
    assert path == wd / "result.json"
    assert "not at /workspace/out/result.json" in note


def test_ignores_a_result_json_that_predates_the_harness(tmp_path):
    """THE anti-forgery gate: a fixture already in the clone must never be
    adopted as this run's outcome."""
    ws, wd = _ws(tmp_path)
    stale = wd / "result.json"
    stale.write_text("{}")
    os.utime(stale, (time.time() - 3600, time.time() - 3600))
    assert ep.find_result_json(ws, wd, time.time())[0] is None


def test_does_not_walk_the_repo_tree(tmp_path):
    ws, wd = _ws(tmp_path)
    deep = wd / "src" / "x"
    deep.mkdir(parents=True)
    (deep / "result.json").write_text("{}")
    assert ep.find_result_json(ws, wd, time.time() - 5)[0] is None


def test_symlink_is_not_adopted(tmp_path):
    """stat() follows links, so a symlink would be a way out of the fixed
    candidate list and out of the workspace entirely."""
    ws, wd = _ws(tmp_path)
    outside = tmp_path / "planted.json"
    outside.write_text('{"outcome": "executed", "summary": "planted"}')
    (ws / "result.json").symlink_to(outside)
    assert ep.find_result_json(ws, wd, time.time() - 5)[0] is None


def test_directory_named_result_json_is_not_adopted(tmp_path):
    ws, wd = _ws(tmp_path)
    (ws / "result.json").mkdir()
    assert ep.find_result_json(ws, wd, time.time() - 5)[0] is None


def test_unreadable_out_dir_does_not_raise(tmp_path):
    """Python 3.12's Path.exists() lets EACCES propagate; this helper runs on
    the failure path and must never raise."""
    ws, wd = _ws(tmp_path)
    os.chmod(ws / "out", 0o000)
    try:
        ep.find_result_json(ws, wd, time.time())     # must not raise
    finally:
        os.chmod(ws / "out", stat.S_IRWXU)


def test_git_tracked_note_warns_about_the_pr(tmp_path):
    """EXECUTE commits at the end, so a stray inside the clone may already be
    in the PR — a different question from "was it written this run"."""
    ws, wd = _ws(tmp_path)
    (wd / "result.json").write_text("{}")
    _, note = ep.find_result_json(
        ws, wd, time.time() - 5,
        runner=lambda *a, **k: type("R", (), {"returncode": 0})())
    assert "tracked by git" in note and "PR" in note


# ── bad_output_reason ────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (FileNotFoundError(2, "No such file"), "missing"),
    (json.JSONDecodeError("bad", "{", 0), "invalid_json"),
    (AssertionError("outcome 'reviewed' illegal for EXECUTE"), "illegal_outcome"),
    (AssertionError(), "bad_summary"),
    (PermissionError(13, "Permission denied"), "unreadable"),
    (IsADirectoryError(21, "Is a directory"), "unreadable"),
    (ValueError("something else"), "invalid"),
])
def test_bad_output_reason(exc, expected):
    assert ep.bad_output_reason(exc) == expected


# ── the 12-vs-15 precedence guard ────────────────────────────────────────────

def test_generic_auth_wording_does_not_outrank_a_fault():
    """A false 12 pauses an entire Dev Type, and OpenAI-compatible gateways emit
    bare "authentication"/"unauthorized" on ordinary rejections."""
    assert ep.classify_harness_failure("authentication failed") == 12
    assert ep.auth_evidence_is_distinctive("authentication failed") is False


@pytest.mark.parametrize("err,status,distinctive", [
    ("not signed in", None, True),
    ("please run grok login", None, True),
    ("unauthorized", None, False),
    ("authentication error", 401, True),
    ("authentication error", 403, True),
    ("authentication error", 500, False),
])
def test_distinctive_auth_evidence(err, status, distinctive):
    assert ep.auth_evidence_is_distinctive(err, status) is distinctive


# ── the composed nonzero-exit rule (ADR-0018 §3) ─────────────────────────────
# The arms above were each covered in isolation; the ORDER they compose in was
# not, which is how a structured 401 with an empty stderr reached 15 instead of
# 12 — the operator was never told to refresh the credential.

FAULT_TERMINAL = {"reason": ep.FAULT_TERMINAL_ERROR,
                  "detail": "claude reported a terminal error: 401"}
FAULT_BUDGET = {"reason": ep.FAULT_TURN_BUDGET, "detail": "stopped at the turn cap"}


@pytest.mark.parametrize("err,fault,status,expected", [
    # THE regression: a revoked key answers 401 with an EMPTY stderr, and its
    # result event trips terminal_error too — the status must still win.
    ("", FAULT_TERMINAL, 401, (12, "DEV_AUTH")),
    ("", None, 403, (12, "DEV_AUTH")),
    # generic wording loses to the predicate, but still wins without one
    ("Error: authentication failed", FAULT_TERMINAL, None, (15, "DEV_HARNESS_FAULT")),
    ("Error: authentication failed", None, None, (12, "DEV_AUTH")),
    # distinctive wording outranks the predicate on stderr alone
    ("Error: not signed in", FAULT_TERMINAL, None, (12, "DEV_AUTH")),
    # turn budget first, always: it is deterministic and must never latch a
    # breaker or become correlation-eligible
    ("", FAULT_BUDGET, 401, (16, "DEV_TURN_BUDGET")),
    ("please run grok login", FAULT_BUDGET, None, (16, "DEV_TURN_BUDGET")),
    # no credential evidence at all
    ("Segmentation fault", FAULT_TERMINAL, None, (15, "DEV_HARNESS_FAULT")),
    ("Segmentation fault", None, None, (10, "DEV_CRASH")),
    ("", None, 500, (10, "DEV_CRASH")),
])
def test_classify_nonzero_exit(err, fault, status, expected):
    assert ep.classify_nonzero_exit(err, fault, status) == expected


# ── harness_argv: one definition, shared with the capture rig ────────────────
# Extracted from main() so scripts/harness_capture builds argv through
# production's own code path. A fixture captured with different flags silently
# stops corresponding to what the predicate sees on a real run.

@pytest.mark.parametrize("harness,head,doc_flags", [
    # the invocations docs/08 §1 documents, flag-for-flag
    ("claude-code", ["claude", "-p"],
     ["--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]),
    ("grok-build", ["grok", "-p"],
     ["--output-format", "streaming-json", "--always-approve"]),
    ("codex", ["codex", "exec"],
     ["--json", "-o", "--skip-git-repo-check",
      "--dangerously-bypass-approvals-and-sandbox"]),
    ("pi", ["pi"],
     ["--mode", "json", "--no-approve"]),
    ("opencode", ["opencode", "run"],
     ["--format", "json", "--auto"]),
    ("qwen-code", ["qwen", "-p"],
     ["--output-format", "stream-json", "--yolo"]),
])
def test_argv_matches_the_documented_invocation(harness, head, doc_flags):
    argv = ep.harness_argv(harness, "PROMPT")
    assert argv[:len(head)] == head
    for flag in doc_flags:
        assert flag in argv, f"{harness}: docs/08 documents {flag}"
    assert "PROMPT" in argv


def test_argv_verbose_is_mandatory_for_claude():
    """The CLI errors out on -p + stream-json without --verbose, so this is a
    behavioural requirement rather than a preference."""
    assert "--verbose" in ep.harness_argv("claude-code", "P")


@pytest.mark.parametrize("harness,plan_flags", [
    ("claude-code", ["--permission-mode", "plan"]),
    ("grok-build", ["--permission-mode", "plan"]),
    ("codex", ["--sandbox", "read-only"]),          # codex's read-only substitute
    ("pi", ["--tools", "read,grep,find,ls"]),       # no native plan mode
    ("opencode", ["--agent", "plan"]),
    ("qwen-code", ["--approval-mode", "plan"]),
])
def test_argv_plan_mode_is_read_only_per_harness(harness, plan_flags):
    argv = ep.harness_argv(harness, "P", plan_mode=True)
    for f in plan_flags:
        assert f in argv
    for f in ("--dangerously-skip-permissions", "--always-approve",
              "--dangerously-bypass-approvals-and-sandbox", "--auto",
              "--yolo"):
        assert f not in argv, f"{harness}: plan mode must not grant writes"


@pytest.mark.parametrize("harness,pin", [
    ("claude-code", "--model"), ("grok-build", "--model"), ("codex", "-m"),
    ("pi", "--model"), ("opencode", "--model"),
    ("qwen-code", "--model")])
def test_argv_model_pin_is_omitted_when_empty(harness, pin):
    """An empty DEVCAKE_MODEL means "harness default" — passing an empty pin
    would make every CLI reject the invocation."""
    assert pin not in ep.harness_argv(harness, "P", model="")
    argv = ep.harness_argv(harness, "P", model="some-model")
    assert argv[argv.index(pin) + 1] == "some-model"


def test_argv_extra_args_come_last_so_they_can_override():
    """docs/08 §1: extra args come last, so a Mission Type can override the pin."""
    argv = ep.harness_argv("codex", "P", model="a", extra=["-m", "b"])
    assert argv[-2:] == ["-m", "b"]


def test_argv_codex_out_dir_is_redirectable():
    """The capture rig runs outside /workspace and must not write there."""
    assert "/tmp/cap/last_message.txt" in ep.harness_argv(
        "codex", "P", out_dir="/tmp/cap")


def test_argv_unknown_harness_fails_closed():
    """A typo must not silently run the Claude argv on a grok/codex image."""
    with pytest.raises(ValueError, match="unknown harness"):
        ep.harness_argv("something-new", "P")


def test_sigterm_flushes_artifacts_once(monkeypatch):
    sent = []

    def fake_send(payload):
        sent.append(payload)

    monkeypatch.setattr(ep, "send_artifacts", fake_send)
    import devcake_dev.adapters.bus as bus
    monkeypatch.setattr(bus, "ARTIFACTS_SENT", False)
    with pytest.raises(SystemExit) as ei:
        ep._on_term(signal.SIGTERM, None)
    assert ei.value.code == 20
    assert sent and sent[0]["error_class"] == "DEV_CRASH"
    assert sent[0]["exit_code"] == 20


def test_opencode_tool_calls_step_finish_is_not_terminal():
    stream = json.dumps({
        "type": "step_finish",
        "part": {"reason": "tool-calls", "tokens": {"total": 1}},
    })
    fault = ep.opencode_run_fault(stream, 0)
    assert fault and fault["reason"] == ep.FAULT_NO_TERMINAL_EVENT


def test_opencode_retry_then_healthy_is_not_a_fault():
    """A recovered in-process retry must not latch an earlier error event."""
    stream = "\n".join([
        json.dumps({"type": "error", "error": {
            "name": "APIError",
            "data": {"message": "rate limited", "statusCode": 429,
                     "isRetryable": True}}}),
        json.dumps({"type": "text", "part": {"text": "ACKNOWLEDGED"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "stop"}}),
    ])
    assert ep.opencode_run_fault(stream, 0) is None


def test_opencode_length_step_finish_is_terminal():
    stream = "\n".join([
        json.dumps({"type": "text", "part": {"text": "partial"}}),
        json.dumps({"type": "step_finish", "part": {"reason": "length"}}),
    ])
    assert ep.opencode_run_fault(stream, 0) is None


def test_opencode_status_prefix_is_an_exact_three_digit_token():
    def _oc_err(msg):
        return json.dumps({"type": "error", "error": {
            "name": "APIError", "data": {"message": msg}}})

    assert ep.harness_api_error_status(
        "opencode", _oc_err("40100ms deadline exceeded")) is None
    assert ep.harness_api_error_status(
        "opencode", _oc_err("120s timeout waiting: HTTP 401 from upstream")) == 401
    assert ep.harness_api_error_status(
        "opencode", _oc_err("401 Incorrect API key provided")) == 401


def test_pi_retry_then_healthy_is_not_a_fault():
    """A recovered in-process retry must not latch the earlier stopReason=error."""
    stream = "\n".join([
        json.dumps({"type": "message_end", "message": {
            "role": "assistant", "content": [],
            "stopReason": "error", "errorMessage": "429: stub"}}),
        json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant", "content": [], "stopReason": "error",
             "errorMessage": "429: stub"}], "willRetry": True}),
        json.dumps({"type": "auto_retry_end", "success": True}),
        json.dumps({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ACKNOWLEDGED"}],
            "stopReason": "stop"}}),
        json.dumps({"type": "agent_end", "messages": [
            {"role": "assistant",
             "content": [{"type": "text", "text": "ACKNOWLEDGED"}],
             "stopReason": "stop"}], "willRetry": False}),
    ])
    assert ep.pi_run_fault(stream, 0) is None


def test_qwen_quoted_api_error_in_assistant_text_is_not_a_fault():
    """Model prose that mentions [API Error: 401 …] must not trip DEV_AUTH."""
    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text":
                         'the log said "[API Error: 401 Incorrect API key provided]"'}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "result": "fixed the auth check", "num_turns": 1}),
    ])
    assert ep.qwen_run_fault(stream, 0) is None
    assert ep.harness_api_error_status("qwen-code", stream) is None


def test_qwen_quoted_api_error_in_result_text_is_not_a_fault():
    """The result field is the model's final answer — a quote is not a wrapper."""
    stream = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": ('fixed the check that logged '
                   '"[API Error: 401 Incorrect API key provided]"'),
        "num_turns": 1,
    })
    assert ep.qwen_run_fault(stream, 0) is None
    assert ep.harness_api_error_status("qwen-code", stream) is None


def test_qwen_result_api_error_wrapper_is_still_a_fault():
    stream = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "[API Error: 401 stub injected HTTP 401]", "num_turns": 1,
    })
    fault = ep.qwen_run_fault(stream, 0)
    assert fault and fault["reason"] == ep.FAULT_TERMINAL_ERROR
    assert ep.harness_api_error_status("qwen-code", stream) == 401


def test_qwen_status_prefix_is_an_exact_three_digit_token():
    def _qwen_err(msg):
        return json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "result": f"[API Error: {msg}]", "num_turns": 1,
        })

    assert ep.harness_api_error_status(
        "qwen-code", _qwen_err("40100ms deadline exceeded")) is None
    assert ep.harness_api_error_status(
        "qwen-code", _qwen_err("120s timeout waiting: HTTP 401 from upstream")) == 401
    assert ep.harness_api_error_status(
        "qwen-code", _qwen_err("401 Incorrect API key provided")) == 401


def test_pi_status_prefix_is_an_exact_three_digit_token():
    """message[:3].isdigit() is not a status: a timeout must not be 401."""
    def _pi_err(msg):
        return json.dumps({"type": "message_end", "message": {
            "role": "assistant", "content": [],
            "stopReason": "error", "errorMessage": msg}})

    assert ep.harness_api_error_status(
        "pi", _pi_err("40100ms deadline exceeded")) is None
    assert ep.harness_api_error_status(
        "pi", _pi_err("1234 things failed")) is None
    assert ep.harness_api_error_status(
        "pi", _pi_err("120s timeout waiting: HTTP 401 from upstream")) == 401
    assert ep.harness_api_error_status(
        "pi", _pi_err("401 Incorrect API key provided")) == 401
