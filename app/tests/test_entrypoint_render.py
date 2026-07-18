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


def test_claude_text_dump_collects_text_blocks_untruncated():
    # ADR-0014 D1: the full dump keeps every assistant-visible text block in
    # order, UNTRUNCATED (the live relay's 200-char cap must not apply), and
    # excludes thinking + tool_use
    long_text = ("deep analysis " * 40).strip()            # ≫ 200-char relay cap
    second = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": long_text}]}})
    subagent = json.dumps({"type": "assistant", "parent_tool_use_id": "tu_1",
                           "message": {"content": [
                               {"type": "text", "text": "subagent chatter"}]}})
    indented = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "    indented\n    code"}]}})
    malformed = [json.dumps({"type": "assistant", "message": "oops"}),
                 json.dumps({"type": "assistant", "message": {"content": "s"}}),
                 json.dumps({"type": "assistant",
                             "message": {"content": ["notdict"]}}),
                 json.dumps({"type": "assistant", "message": {"content": [
                     {"type": "text", "text": None}]}})]
    out = "\n".join([CLAUDE_INIT, CLAUDE_TOOL, CLAUDE_TEXT, CLAUDE_THINKING,
                     subagent, *malformed, second, indented, "not json",
                     CLAUDE_RESULT])
    dump = ep.claude_text_dump(out)
    first = dump.find("missing fixture")
    assert first != -1
    assert dump.find(long_text) > first                    # order preserved
    assert dump.count("missing fixture") == 1              # no duplication
    assert dump.count(long_text) == 1
    assert "private reasoning" not in dump                 # thinking excluded
    assert "file_path" not in dump                         # tool_use excluded
    assert "done" not in dump              # result-event text never ingested
    assert "subagent chatter" not in dump  # tool-internal (parent_tool_use_id)
    assert "    indented\n    code" in dump                # indentation kept


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


def test_codex_text_dump_collects_agent_messages():
    # ADR-0014 D1: agent_message texts in order, untruncated; command
    # executions and usage events excluded; non-JSON lines tolerated
    long_msg = ("regression detail " * 30).strip()
    second = json.dumps({"type": "item.completed", "item": {
        "id": "item_2", "type": "agent_message", "text": long_msg}})
    via_item_type = json.dumps({"type": "item.completed", "item": {
        "id": "item_3", "item_type": "agent_message", "text": "via item_type"}})
    malformed = [json.dumps({"type": "item.completed", "item": "oops"}),
                 json.dumps({"type": "item.completed",
                             "item": {"type": "agent_message", "text": None}})]
    out = "\n".join([CODEX_CMD, CODEX_MSG, "garbage line", *malformed,
                     second, via_item_type, CODEX_TURN])
    dump = ep.codex_text_dump(out)
    assert dump.startswith("Output:")                      # first message first
    assert dump.count(long_msg) == 1                       # once, untruncated
    assert "via item_type" in dump                         # item_type key arm
    assert "/bin/bash" not in dump                         # command excluded
    assert "25247" not in dump                             # usage excluded
    assert "None" not in dump                              # no repr injection


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


def test_assemble_transcript_full_dump_shape():
    # ADR-0014 D1: attachment doc = header + full dump + outcome; the Agent
    # report section survives only as the no-dump fallback
    tr = {"num_turns": 3, "duration_ms": 42}
    result = {"schema_version": 1, "outcome": "executed", "summary": "s"}
    doc = ep.assemble_transcript(seq="2", mtype="EXECUTE", run_id="R-1",
                                 dev_type="senior", harness="claude-code",
                                 token_report=tr, dump="FULL DUMP TEXT",
                                 result_text="last msg", result=result)
    assert doc.startswith("# 2_EXECUTE — run R-1")
    assert "**turns:** 3" in doc and "**duration:** 42 ms" in doc
    assert "## Session transcript\n\nFULL DUMP TEXT" in doc
    assert "## Agent report" not in doc                    # dump supersedes it
    assert '"outcome": "executed"' in doc                  # Outcome JSON rides
    doc2 = ep.assemble_transcript(seq="1", mtype="PLAN", run_id="R",
                                  dev_type="d", harness="h", token_report={},
                                  dump="", result_text="only the last message",
                                  result=result)
    assert "## Agent report\n\nonly the last message" in doc2
    doc3 = ep.assemble_transcript(seq="1", mtype="PLAN", run_id="R",
                                  dev_type="d", harness="h", token_report={},
                                  dump="D", result_text="x", result=None)
    assert "## Outcome" not in doc3                        # failure use


def test_write_activity_payload_writes_folder(tmp_path):
    # ADR-0014 D3: MISSION.md written when the app sent one; an OLD app's
    # payload (no mission_md) writes no MISSION.md; filenames basename-
    # sanitized as before
    import base64
    act = {"mission_md": "# T-1: brief", "activity_md": "# feed",
           "attachments": [{"filename": "../evil/report.md",
                            "content_b64": base64.b64encode(b"data").decode()}]}
    ep.write_activity_payload(act, tmp_path / "activity")
    assert (tmp_path / "activity" / "MISSION.md").read_text() == "# T-1: brief"
    assert (tmp_path / "activity" / "ACTIVITY.md").read_text() == "# feed"
    assert (tmp_path / "activity" / "report.md").read_bytes() == b"data"
    assert not (tmp_path / "evil").exists()

    old = {"activity_md": "old shape", "attachments": []}
    ep.write_activity_payload(old, tmp_path / "old")
    assert (tmp_path / "old" / "ACTIVITY.md").read_text() == "old shape"
    assert not (tmp_path / "old" / "MISSION.md").exists()


def test_with_session_appends_dump_only_when_present():
    assert ep.with_session("err", "DUMP") == \
        "err\n\n## Session transcript\n\nDUMP"
    assert ep.with_session("err", "") == "err"


def test_fit_payload_shrinks_transcript_before_last_message(monkeypatch):
    # last_message_md is shrinkable, but the (larger) dump always halves first
    monkeypatch.setattr(ep, "MAX_ARTIFACT_BYTES", 64 * 1024)
    payload = {"result": {"outcome": "x"}, "token_report": {},
               "transcript_md": "t" * (200 * 1024),
               "last_message_md": "m" * (20 * 1024)}
    fitted = ep._fit_payload(payload)
    assert fitted["last_message_md"] == "m" * (20 * 1024)   # untouched
    assert "[devcake] transcript_md truncated" in fitted["transcript_md"]
    monkeypatch.setattr(ep, "MAX_ARTIFACT_BYTES", 32 * 1024)
    fitted2 = ep._fit_payload({"result": {}, "last_message_md": "z" * (100 * 1024)})
    assert "[devcake] last_message_md truncated" in fitted2["last_message_md"]


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


# ── skill install (runspec skills → the harness's registry skills_dir) ───────

def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode()


def test_install_skills_writes_files_under_home(tmp_path):
    notes = ep.install_skills(
        [{"name": "tdd", "files": [
            {"path": "tdd/SKILL.md", "content_b64": _b64(b"body")},
            {"path": "tdd/ref/extra.md", "content_b64": _b64(b"x")}]}],
        home=tmp_path)
    assert (tmp_path / ".claude/skills/tdd/SKILL.md").read_bytes() == b"body"
    assert (tmp_path / ".claude/skills/tdd/ref/extra.md").read_bytes() == b"x"
    assert any("tdd: installed 2 file(s)" in n for n in notes)


def test_install_skills_refuses_traversal_and_absolute(tmp_path):
    notes = ep.install_skills(
        [{"name": "bad", "files": [
            {"path": "../evil.md", "content_b64": _b64(b"evil")},
            {"path": "/abs/evil.md", "content_b64": _b64(b"evil")},
            {"path": "", "content_b64": _b64(b"evil")},
            {"path": "ok/fine.md", "content_b64": _b64(b"fine")}]}],
        home=tmp_path)
    assert not (tmp_path / "evil.md").exists()
    assert not Path("/abs/evil.md").exists()
    assert (tmp_path / ".claude/skills/ok/fine.md").read_bytes() == b"fine"
    assert sum("refused unsafe path" in n for n in notes) == 3


def test_install_skills_bad_base64_nonfatal(tmp_path):
    notes = ep.install_skills(
        [{"name": "x", "files": [
            {"path": "x/SKILL.md", "content_b64": "!!!"},
            {"path": "x/good.md", "content_b64": _b64(b"ok")}]}],
        home=tmp_path)
    assert not (tmp_path / ".claude/skills/x/SKILL.md").exists()
    assert (tmp_path / ".claude/skills/x/good.md").exists()
    assert any("installed 1 file(s)" in n for n in notes)


def test_install_skills_empty_spec_is_noop(tmp_path):
    assert ep.install_skills([], home=tmp_path) == []
    assert ep.install_skills(None, home=tmp_path) == []
    assert not (tmp_path / ".claude").exists()


def test_install_skills_honors_skills_dir(tmp_path):
    """grok/codex runs deliver skills_dir='.agents/skills' via the runspec —
    files must land there and nowhere near .claude."""
    ep.install_skills(
        [{"name": "tdd", "files": [
            {"path": "tdd/SKILL.md", "content_b64": _b64(b"body")}]}],
        home=tmp_path, skills_dir=".agents/skills")
    assert (tmp_path / ".agents/skills/tdd/SKILL.md").read_bytes() == b"body"
    assert not (tmp_path / ".claude").exists()


def test_install_skills_rejects_unsafe_skills_dir(tmp_path):
    """skills_dir crosses the protocol, so it gets the same home-relative
    guard as file paths: absolute/../empty values fall back to the default
    with a note — never a write outside $HOME, never a refused run."""
    for bad in ("/abs/skills", "../up", ""):
        notes = ep.install_skills(
            [{"name": "x", "files": [
                {"path": "x/SKILL.md", "content_b64": _b64(b"y")}]}],
            home=tmp_path, skills_dir=bad)
        if bad:      # "" silently maps to the default, no refusal note
            assert any("refused unsafe skills_dir" in n for n in notes), bad
        assert (tmp_path / ".claude/skills/x/SKILL.md").exists(), bad
        (tmp_path / ".claude/skills/x/SKILL.md").unlink()
    assert not (tmp_path.parent / "up").exists()
