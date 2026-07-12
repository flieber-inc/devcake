"""Renderer/parsing units for the Dev entrypoint's live-output relay.

Sample events below were captured live in this workspace: claude 2.x
stream-json, codex 0.144 --json, grok 0.2.93 streaming-json.
"""

import importlib.util
import json
import os
from pathlib import Path

ENTRYPOINT = Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py"

# module reads these at import; redis.from_url is lazy (no connection until use)
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

spec = importlib.util.spec_from_file_location("dev_entrypoint", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)


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
