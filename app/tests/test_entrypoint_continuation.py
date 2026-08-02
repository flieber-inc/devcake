"""In-container continuation (ADR-0022) — the pure decision helpers.

Resume mechanics run against the REAL capture pairs in fixtures/harness_streams/
(`*_resume_nudge` / `*_resume_nudge_resume`): each pair is one rig run whose
second leg went through the production `harness_resume_argv` in the same
workspace, so these tests assert what the CLIs actually did, not what their
help text promises.
"""

import importlib.util
import json
import os
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

# distinct module name — the other entrypoint suites load the same file under
# their own names in this pytest process
spec = importlib.util.spec_from_file_location("dev_entrypoint_continuation",
                                              ENTRYPOINT)
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


def meta(name):
    return json.loads(fx(f"{name}.meta.json"))


def J(obj):
    return json.dumps(obj)


# ── session_identity — the neutral per-harness session handle ────────────────

@pytest.mark.parametrize("harness,capture", [
    ("grok-build", "grok_resume_nudge"),
    ("claude-code", "claude_resume_nudge"),
    ("codex", "codex_resume_nudge"),
])
def test_session_identity_matches_the_rig_measurement(harness, capture):
    """The handle the loop would resume with is the one the rig measured and
    actually resumed with (meta.session_id fed the second leg's argv)."""
    assert ep.session_identity(harness, fx(f"{capture}.jsonl")) \
        == meta(capture)["session_id"]


@pytest.mark.parametrize("harness,capture", [
    ("grok-build", "grok_resume_nudge"),
    ("claude-code", "claude_resume_nudge"),
    ("codex", "codex_resume_nudge"),
])
def test_resume_leg_kept_the_first_legs_session(harness, capture):
    """No fork on any harness: the resumed stream reports the SAME handle the
    first leg exposed — the fact export_sids' last-per-chain rule rests on."""
    resumed = meta(f"{capture}_resume")
    assert resumed["first_session_id"] == meta(capture)["session_id"]
    assert ep.session_identity(harness, fx(f"{capture}_resume.jsonl")) \
        == resumed["first_session_id"]


def test_session_identity_absent_is_empty_string():
    assert ep.session_identity("grok-build", J({"type": "error", "message": "x"})) == ""
    assert ep.session_identity("codex", J({"type": "turn.started"})) == ""
    assert ep.session_identity("claude-code", J({"type": "assistant"})) == ""
    assert ep.session_identity("grok-build", "") == ""


def test_session_identity_never_raises_on_hostile_shapes():
    hostile = "\n".join([
        J({"type": "end", "sessionId": {"nested": True}}),
        J({"type": "thread.started", "thread_id": ["a"]}),
        "not json", J([1, 2])])
    for harness in ("grok-build", "codex", "claude-code", "unknown"):
        assert isinstance(ep.session_identity(harness, hostile), str)


# ── harness_resume_argv — the per-harness resume dialects ────────────────────

def test_grok_resume_argv_matches_the_captured_composition():
    """The builder reproduces the argv the capture proved (modulo the ids),
    keeping the same output format and approval mode as harness_argv so the
    stream parsers and fault predicate see the identical shape."""
    argv = ep.harness_resume_argv("grok-build", "SID", "NUDGE",
                                  model="stub-model")
    captured = meta("grok_resume_nudge_resume")["argv"]
    assert argv == ["grok", "-p", "NUDGE", "-r", "SID",
                    "--output-format", "streaming-json", "--always-approve",
                    "--model", "stub-model"]
    assert captured[:2] == ["grok", "-p"]
    assert captured[3] == "-r" and "--always-approve" in captured


def test_claude_resume_argv_matches_the_captured_composition():
    argv = ep.harness_resume_argv("claude-code", "SID", "NUDGE")
    assert argv == ["claude", "-p", "NUDGE", "--resume", "SID",
                    "--output-format", "stream-json", "--verbose",
                    "--dangerously-skip-permissions"]
    assert meta("claude_resume_nudge_resume")["argv"][3] == "--resume"


def test_codex_resume_argv_matches_the_captured_composition():
    argv = ep.harness_resume_argv("codex", "SID", "NUDGE", out_dir="/o")
    assert argv[:5] == ["codex", "exec", "resume", "SID", "NUDGE"]
    assert "--json" in argv and "/o/last_message.txt" in argv
    assert meta("codex_resume_nudge_resume")["argv"][1:3] == ["exec", "resume"]


def test_resume_argv_extras_come_last_so_they_can_override():
    for harness in ("grok-build", "claude-code", "codex"):
        argv = ep.harness_resume_argv(harness, "S", "P", extra=["--zz", "1"])
        assert argv[-2:] == ["--zz", "1"]


def test_resume_argv_unknown_harness_is_none_not_the_claude_fallback():
    """harness_argv falls back to the claude dialect for unknown names; the
    resume builder deliberately must NOT — an unverified resume flag on the
    wrong CLI is a crash, not a default."""
    assert ep.harness_resume_argv("other", "S", "P") is None


# ── RESUME_SPECS — capture-verified facts only ───────────────────────────────

def test_resume_specs_cover_exactly_the_captured_harnesses():
    assert set(ep.RESUME_SPECS) == {"grok-build", "claude-code", "codex"}


def test_codex_usage_is_cumulative_and_the_others_are_not():
    """The measured per-harness fact behind ResumeSpec.usage_cumulative: the
    codex resume leg's turn.completed reports BOTH calls' tokens (240/48 vs
    the first leg's 120/24); grok and claude report one invocation's worth.
    Token VALUES are the stub's; the aggregation behaviour is the CLI's own
    (fixtures README)."""
    def usage(name, kind, key):
        for line in fx(f"{name}.jsonl").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict) and ev.get("type") == kind:
                return (ev.get("usage") or {}).get(key)
        return None
    assert usage("codex_resume_nudge_resume", "turn.completed", "input_tokens") \
        == 2 * usage("codex_resume_nudge", "turn.completed", "input_tokens")
    assert usage("grok_resume_nudge_resume", "end", "input_tokens") \
        == usage("grok_resume_nudge", "end", "input_tokens")
    assert ep.RESUME_SPECS["codex"].usage_cumulative is True
    assert ep.RESUME_SPECS["grok-build"].usage_cumulative is False
    assert ep.RESUME_SPECS["claude-code"].usage_cumulative is False


def test_resumed_streams_do_not_replay_history():
    """The parser-safety fact: a resumed stream carries ONLY the new turn's
    events (one text/assistant burst, one terminal event) — no re-emission of
    the first leg's messages that would pollute result_text or the token
    report."""
    grok_kinds = [json.loads(l).get("type")
                  for l in fx("grok_resume_nudge_resume.jsonl").splitlines()]
    assert grok_kinds.count("end") == 1
    claude_kinds = [json.loads(l).get("type")
                    for l in fx("claude_resume_nudge_resume.jsonl").splitlines()]
    assert claude_kinds.count("assistant") == 1 and claude_kinds.count("result") == 1
    codex_kinds = [json.loads(l).get("type")
                   for l in fx("codex_resume_nudge_resume.jsonl").splitlines()]
    assert codex_kinds.count("turn.completed") == 1
