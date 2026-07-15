"""Renderer/parsing units for the Dev entrypoint's live-output relay.

Sample events below were captured live in this workspace: claude 2.x
stream-json, codex 0.144 --json, grok 0.2.93 streaming-json.
"""

import importlib.util
import json
import os
from pathlib import Path

# host checkout: repo/app/tests → repo/images/common; app container: /srv/tests
# → /srv/images/common (read-only compose mount)
_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# module reads these at import; redis.from_url is lazy (no connection until use).
# Restore the env afterwards — leaking REDIS_URL here would repoint the live
# messaging tests (same pytest process) at a nonexistent server.
_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

spec = importlib.util.spec_from_file_location("dev_entrypoint", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ── claude stream-json ───────────────────────────────────────────────────────

CLAUDE_INIT = json.dumps({"type": "system", "subtype": "init",
                          "session_id": "6d388593-a204", "model": "claude-fable-5"})
CLAUDE_TOOL = json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "/workspace/x.py"}}]}})
CLAUDE_TEXT = json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "The failing test is caused by a missing fixture."}]}})
CLAUDE_THINKING = json.dumps({"type": "assistant", "message": {"content": [
    {"type": "thinking", "thinking": "private reasoning"}]}})
CLAUDE_RESULT = json.dumps({"type": "result", "subtype": "success", "result": "done",
                            "num_turns": 14, "total_cost_usd": 0.83,
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                            "duration_ms": 1386})
CLAUDE_NOISE = json.dumps({"type": "system", "subtype": "thinking_tokens",
                           "estimated_tokens": 5})


def test_render_claude_events():
    assert ep.render_claude(CLAUDE_INIT) == \
        "[claude] session 6d388593 · model=claude-fable-5"
    assert ep.render_claude(CLAUDE_TOOL) == \
        '→ Read {"file_path": "/workspace/x.py"}'
    assert "missing fixture" in ep.render_claude(CLAUDE_TEXT)
    assert ep.render_claude(CLAUDE_THINKING) is None
    assert ep.render_claude(CLAUDE_NOISE) is None
    assert ep.render_claude(CLAUDE_RESULT) == \
        "[claude] done: success · turns=14 · cost=$0.83"
    assert ep.render_claude("plain text line") == "plain text line"
    assert ep.render_claude("   \n") is None


def test_claude_result_event_extraction():
    out = "\n".join([CLAUDE_INIT, CLAUDE_TOOL, "not json", CLAUDE_RESULT])
    ev = ep.claude_result_event(out)
    assert ev["num_turns"] == 14 and ev["usage"]["input_tokens"] == 10
    assert ep.claude_result_event(CLAUDE_INIT) is None
    # plain-blob fallback path used by main(): result event absent → json.loads
    blob = json.dumps({"result": "x", "usage": {}})
    assert ep.claude_result_event(blob) is None
    assert json.loads(blob)["result"] == "x"


# ── codex --json ─────────────────────────────────────────────────────────────

CODEX_CMD = json.dumps({"type": "item.completed", "item": {
    "id": "item_0", "type": "command_execution",
    "command": "/bin/bash -lc 'echo hi'", "exit_code": 0, "status": "completed"}})
CODEX_MSG = json.dumps({"type": "item.completed", "item": {
    "id": "item_1", "type": "agent_message", "text": "Output:\n\nhi"}})
CODEX_TURN = json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 25247, "output_tokens": 88}})


def test_render_codex_events():
    assert ep.render_codex(CODEX_CMD) == "$ /bin/bash -lc 'echo hi' → exit 0"
    assert ep.render_codex(CODEX_MSG).startswith("Output:")
    assert ep.render_codex(CODEX_TURN) == "[codex] turn done · in=25247 out=88"
    assert ep.render_codex(json.dumps({"type": "turn.started"})) is None


# ── grok streaming-json ──────────────────────────────────────────────────────

def _grok(kind, **kw):
    return json.dumps({"type": kind, **kw})


def test_grok_coalescer_and_parse():
    co = ep.GrokCoalescer()
    assert co(_grok("thought", data="thinking...")) is None
    assert co(_grok("text", data="hello ")) is None
    assert co(_grok("text", data="world\npartial")) == "hello world"
    end = co(_grok("end", stopReason="EndTurn", sessionId="019f-aa"))
    assert end == "partial\n[grok] done: EndTurn"
    assert co.finish() is None

    out = "\n".join([_grok("thought", data="t"), _grok("text", data="hi "),
                     _grok("text", data="there"),
                     _grok("end", stopReason="EndTurn", sessionId="019f-aa")])
    assert ep.grok_stream_parse(out) == ("hi there", "019f-aa")
    assert ep.grok_stream_parse('{"text": "blob", "sessionId": "x"}') is None


def test_grok_coalescer_flushes_long_buffer():
    co = ep.GrokCoalescer()
    assert co(_grok("text", data="x" * 250)) == "x" * 250


# ── stderr + relay ───────────────────────────────────────────────────────────

def test_render_stderr():
    assert ep.render_stderr("boom\n") == "! boom"
    assert ep.render_stderr("   \n") is None


def test_logrelay_batches_truncates_and_caps(monkeypatch):
    sent = []
    monkeypatch.setattr(ep, "send", lambda kind, payload: sent.append((kind, payload)))
    relay = ep.LogRelay()
    relay.add("a" * 5000)                     # truncated to LINE_LIMIT
    relay.add("multi\nline")                  # split into two entries
    relay.flush()
    assert sent[0][0] == "run.log"
    lines = sent[0][1]["lines"]
    assert lines[0] == "a" * ep.LINE_LIMIT
    assert lines[1:] == ["multi", "line"]

    sent.clear()
    for _ in range(ep.BATCH_LINES + 10):
        relay.add("x")
    relay.flush()
    assert [len(p["lines"]) for _k, p in sent] == [ep.BATCH_LINES, 10]

    relay.sent = ep.MAX_RELAY_LINES           # flood guard: add() becomes a no-op
    relay.add("dropped")
    assert relay.buf == []


def test_logrelay_swallows_send_failures(monkeypatch):
    def boom(kind, payload):
        raise RuntimeError("redis down")
    monkeypatch.setattr(ep, "send", boom)
    relay = ep.LogRelay()
    relay.add("line")
    relay.flush()                             # must not raise
    assert relay.sent == 0


def test_logrelay_reports_silent_live_harness_without_flooding(monkeypatch):
    ticks = iter([100.0, 105.0])
    monkeypatch.setattr(ep.time, "monotonic", lambda: next(ticks))
    relay = ep.LogRelay()

    assert not relay.add_silence_notice("grok-build", now=159.9)
    assert relay.add_silence_notice("grok-build", now=165.0)
    assert not relay.add_silence_notice("grok-build", now=200.0)
    assert relay.add_silence_notice("grok-build", now=225.0)
    assert relay.buf == [
        "[devcake] grok-build is still running; no visible model output for 65s "
        "(65s elapsed)",
        "[devcake] grok-build is still running; no visible model output for 125s "
        "(125s elapsed)",
    ]


def test_visible_output_resets_silence_notice(monkeypatch):
    ticks = iter([100.0, 170.0])
    monkeypatch.setattr(ep.time, "monotonic", lambda: next(ticks))
    relay = ep.LogRelay()
    relay.add("model response")

    assert not relay.add_silence_notice("grok-build", now=220.0)
    assert relay.add_silence_notice("grok-build", now=230.0)


# ── forge dialect resolution (descriptor spec_env → clone bootstrap) ─────────

def test_forge_dialect_reads_descriptor_env():
    user, name, email, envs = ep.forge_dialect({
        "DEVCAKE_CLONE_USER": "gitea-bot", "DEVCAKE_GIT_NAME": "DevCake",
        "DEVCAKE_GIT_EMAIL": "bot@example.com",
        "DEVCAKE_FORGE_CLI_ENVS": "GITEA_TOKEN,TEA_TOKEN",
    })
    assert (user, name, email) == ("gitea-bot", "DevCake", "bot@example.com")
    assert envs == ["GITEA_TOKEN", "TEA_TOKEN"]


def test_forge_dialect_crashes_loudly_on_missing_descriptor():
    # app and images deploy in lockstep (docs/13 §8): a runspec without the
    # descriptor vars is a mismatched build and must not limp along
    import pytest
    with pytest.raises(KeyError):
        ep.forge_dialect({})


# ── clone failure classification (global forge breaker gate) ─────────────────

def test_clone_error_class_matches_git_credential_wording():
    real_403 = ("remote: Write access to repository not granted.\n"
                "fatal: unable to access 'https://github.com/o/r.git/': "
                "The requested URL returned error: 403")
    assert ep.clone_error_class(real_403) == "DEV_FORGE_AUTH"
    assert ep.clone_error_class(
        "fatal: Authentication failed for 'https://gitlab.com/o/r.git/'"
    ) == "DEV_FORGE_AUTH"
    assert ep.clone_error_class(
        "fatal: could not read Username for 'https://github.com': "
        "terminal prompts disabled"
    ) == "DEV_FORGE_AUTH"


def test_clone_error_class_ignores_incidental_403():
    # "403" inside a URL/branch name is not a credential failure
    assert ep.clone_error_class(
        "fatal: unable to access 'https://forge/team-403/repo/': "
        "Connection timed out"
    ) == "DEV_FORGE"
    assert ep.clone_error_class("error: RPC failed; HTTP 500") == "DEV_FORGE"


# ── oversized-artifact shrinking (a finished run's result must always ship) ──

def test_fit_payload_truncates_oversized_transcript(monkeypatch):
    monkeypatch.setattr(ep, "MAX_ARTIFACT_BYTES", 1024 * 1024)
    result = {"schema_version": 1, "outcome": "pr_opened", "summary": "s"}
    payload = {"result": result, "token_report": {"model": "m"},
               "transcript_md": "x" * (3 * 1024 * 1024)}
    fitted = ep._fit_payload(payload)
    assert fitted["result"] == result
    assert fitted["token_report"] == {"model": "m"}
    assert "[devcake] transcript_md truncated" in fitted["transcript_md"]
    assert len(json.dumps(fitted).encode("utf-8")) <= 1024 * 1024
    assert len(payload["transcript_md"]) == 3 * 1024 * 1024   # input untouched


def test_fit_payload_small_payload_untouched():
    payload = {"result": {"outcome": "hello"}, "transcript_md": "short"}
    assert ep._fit_payload(payload) is payload


def test_fit_payload_raises_only_when_unshrinkable(monkeypatch):
    import pytest
    monkeypatch.setattr(ep, "MAX_ARTIFACT_BYTES", 1024)
    with pytest.raises(ValueError):
        ep._fit_payload({"result": {"outcome": "x" * 5000}})


def test_send_artifacts_chunks_carry_id_and_digest(monkeypatch):
    import hashlib
    sent = []
    monkeypatch.setattr(ep, "send", lambda kind, payload: sent.append((kind, payload)))
    ep.send_artifacts({"result": {"outcome": "hello"},
                       "transcript_md": "y" * 900_000})
    assert len(sent) > 1
    blob = "".join(p["data"] for _, p in sent)
    assert json.loads(blob)["result"] == {"outcome": "hello"}
    assert len({p["chunk_id"] for _, p in sent}) == 1
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    assert all(p["sha256"] == digest and p["of"] == len(sent) for _, p in sent)


def test_clone_extra_repos_per_repo_token_and_nonfatal():
    """Multi-repo ONBOARD triage (item 2 full scope): each sibling clone
    rides its OWN read token via the askpass env; failures are non-fatal
    and reported, successes land at repo/<slug>."""
    calls = []

    class R:
        def __init__(self, rc, stderr=""):
            self.returncode, self.stderr = rc, stderr

    def runner(cmd, capture_output, text, env):
        calls.append((cmd, env["DEVCAKE_FORGE_TOKEN"]))
        return R(1, "auth failed") if "bad" in cmd[-2] else R(0)

    extras = [
        {"name": "beta", "url": "https://github.com/o/beta.git",
         "clone_user": "x-access-token", "token": "ro-beta-token"},
        {"name": "gamma", "url": "http://gitea:3000/devcake-repos/bad.git",
         "clone_user": "svc", "token": "ro-bad-token"},
    ]
    notes = ep.clone_extra_repos(extras, Path("/tmp/ws/repo"), runner=runner)
    assert len(calls) == 2
    (cmd1, tok1), (cmd2, tok2) = calls
    assert cmd1[:4] == ["git", "clone", "--depth", "1"]
    assert cmd1[4] == "https://x-access-token@github.com/o/beta.git"
    assert cmd1[5].endswith("/repo/beta") and tok1 == "ro-beta-token"
    assert cmd2[4] == "http://svc@gitea:3000/devcake-repos/bad.git"
    assert tok2 == "ro-bad-token"
    assert any("beta: cloned read-only" in n for n in notes)
    assert any("gamma: clone failed" in n for n in notes)
