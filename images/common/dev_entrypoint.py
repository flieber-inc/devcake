"""DevCake Dev entrypoint — shared across harness images (docs/07, docs/08).
Harness selected by the image-baked DEVCAKE_HARNESS env
(claude-code | grok-build | codex).

Exit codes per docs/07 §4: 0 ok · 10 harness crash · 11 bad result.json ·
12 auth · 13 clone/forge · 14 MCP setup · 20 entrypoint error.
"""

import base64
import hashlib
import json
import os
import pathlib
import re as _re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import redis

RUN_ID = os.environ["DEVCAKE_RUN_ID"]
REDIS_URL = os.environ["REDIS_URL"]
TRACEPARENT = os.environ.get("TRACEPARENT", "")
INGRESS = "devcake:ingress"
REPLY = f"devcake:reply:{RUN_ID}"
CHUNK_LIMIT, CHUNK_SIZE = 512 * 1024, 400 * 1024
WORKSPACE = pathlib.Path("/workspace")

r = redis.from_url(REDIS_URL, username=os.environ["REDIS_USER"],
                   password=os.environ["REDIS_PASSWORD"], decode_responses=True)


def send(kind: str, payload: dict) -> None:
    envelope = {"v": 1, "run_id": RUN_ID, "auth": os.environ["REDIS_PASSWORD"],
                "kind": kind, "ts": datetime.now(timezone.utc).isoformat(),
                "payload": payload}
    for attempt in range(4):
        try:
            r.xadd(INGRESS, {"m": json.dumps(envelope)})
            return
        except redis.RedisError:
            if attempt == 3:
                raise
            time.sleep(0.25 * (2 ** attempt))


MAX_ARTIFACT_BYTES = 50 * 1024 * 1024 - 256 * 1024  # headroom under ingress caps
SHRINKABLE_FIELDS = ("transcript_md", "plan_md", "last_message_md")  # never result/exit_code/token_report
TRUNCATE_FLOOR = 10_000


def _fit_payload(payload: dict) -> dict:
    """Shrink an oversized artifact instead of dying at the end of the run:
    halve the largest shrinkable text field (with an explicit notice) until
    the blob fits, so result/exit_code/token_report always ship."""
    if len(json.dumps(payload).encode("utf-8")) <= MAX_ARTIFACT_BYTES:
        return payload
    payload = dict(payload)
    while len(json.dumps(payload).encode("utf-8")) > MAX_ARTIFACT_BYTES:
        shrinkable = [f for f in SHRINKABLE_FIELDS
                      if isinstance(payload.get(f), str)
                      and len(payload[f]) > TRUNCATE_FLOOR]
        if not shrinkable:
            raise ValueError(
                "artifact payload exceeds Redis ingress limits even after truncation")
        field = max(shrinkable, key=lambda f: len(payload[f]))
        text = payload[field]
        keep = len(text) // 2
        payload[field] = (text[:keep] + f"\n\n[devcake] {field} truncated: kept "
                          f"{keep} of {len(text)} characters to fit ingress limits\n")
    return payload


def send_artifacts(payload: dict) -> None:
    payload = _fit_payload(payload)
    blob = json.dumps(payload)
    if len(blob) <= CHUNK_LIMIT:
        send("run.artifacts", payload)
        return
    parts = [blob[i:i + CHUNK_SIZE] for i in range(0, len(blob), CHUNK_SIZE)]
    if len(parts) > 128 or len(blob.encode("utf-8")) > 50 * 1024 * 1024:
        raise ValueError("artifact payload exceeds Redis ingress limits")
    chunk_id = uuid.uuid4().hex
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    for i, part in enumerate(parts, start=1):
        send("run.artifacts", {"chunk": i, "of": len(parts),
                               "chunk_id": chunk_id, "sha256": digest,
                               "data": part})


def clone_extra_repos(extras, repo_dir, runner=None):
    """Read-only sibling clones for multi-repo ONBOARD triage (item 2 full
    scope): each extra repo rides its OWN read token via the shared askpass
    script (per-clone env override). Shallow (--depth 1) — assessment only.
    A failed extra clone is deliberately NON-fatal: triage proceeds on what
    cloned, and the failures are returned for the transcript/log."""
    import re as _re
    runner = runner or subprocess.run
    notes = []
    for x in extras:
        url = x.get("url") or ""
        user = x.get("clone_user") or ""
        clone_url = (_re.sub(r"^(https?://)", rf"\g<1>{user}@", url)
                     if user else url)
        slug = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        env = {**os.environ, "DEVCAKE_FORGE_TOKEN": x.get("token") or ""}
        r = runner(["git", "clone", "--depth", "1", clone_url,
                    str(repo_dir / slug)],
                   capture_output=True, text=True, env=env)
        if r.returncode != 0:
            notes.append(f"extra repo {x.get('name', slug)}: clone failed "
                         f"({(r.stderr or '')[-200:]})")
        else:
            notes.append(f"extra repo {x.get('name', slug)}: cloned "
                         f"read-only at repo/{slug}")
    return notes


def install_skills(skills, home=None, skills_dir=".claude/skills"):
    """Skill-store files from the runspec → $HOME/<skills_dir>/<path> before
    the harness starts. The dir is the harness registry's skills_dir
    (harness.py), delivered as the runspec `skills_dir` key; the default is
    claude-code's dir so an older app that sends no key keeps today's
    behavior. Path-traversal-safe on BOTH the dir and every file path:
    store content is operator-editable, so absolute paths and `..` parts
    are refused. Per-file failures are NON-fatal — skills are additive; the
    notes land in the run log."""
    notes = []
    sd = pathlib.PurePosixPath(skills_dir or ".claude/skills")
    if not sd.parts or sd.is_absolute() or ".." in sd.parts:
        notes.append(f"skills: refused unsafe skills_dir {skills_dir!r} "
                     "— using default")
        sd = pathlib.PurePosixPath(".claude/skills")
    base = pathlib.Path(home or pathlib.Path.home()) / sd
    for sk in skills or []:
        name, wrote = sk.get("name", "?"), 0
        for f in sk.get("files") or []:
            rel = pathlib.PurePosixPath(f.get("path") or "")
            if not rel.parts or rel.is_absolute() or ".." in rel.parts:
                notes.append(f"skill {name}: refused unsafe path "
                             f"{f.get('path')!r}")
                continue
            try:
                target = base / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(f.get("content_b64") or "",
                                                    validate=True))
                wrote += 1
            except Exception as e:
                notes.append(f"skill {name}: {rel} failed ({e})")
        notes.append(f"skill {name}: installed {wrote} file(s)")
    return notes


MCP_SETUP_TIMEOUT_SECS = 300   # per command (docs/07 §5 step 5)


def run_mcp_setup(commands, workdir, timeout=MCP_SETUP_TIMEOUT_SECS):
    """Run the Dev Type's admin-configured MCP setup commands in order.
    Returns (failed_cmd, detail) for the exit-14 artifact, or None when all
    pass. Each command gets a closed stdin, its own process group and a hard
    per-command cap: the heartbeat daemon is already beating when these run,
    so a hung install/interactive prompt would otherwise idle the run to the
    full wall-clock timeout without the watchdog ever firing."""
    import signal
    for cmd in commands:
        proc = subprocess.Popen(cmd, shell=True, cwd=workdir,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # pgid == pid (new session)
            except ProcessLookupError:
                pass
            proc.wait()
            return cmd, f"timed out after {timeout}s"
        if proc.returncode != 0:
            tail = (err or out or "")[-2000:]
            return cmd, f"exit {proc.returncode}: {tail}"
    return None


def clone_error_class(stderr: str) -> str:
    """DEV_FORGE_AUTH only on git's credential wording — a bare "403"/"401"
    can be a rate limit or an incidental URL fragment, and DEV_FORGE_AUTH
    latches the app's global forge breaker."""
    lowered = stderr.lower()
    auth_markers = ("returned error: 403", "returned error: 401",
                    "authentication failed", "repository not found",
                    "write access to repository not granted",
                    "could not read username", "could not read password",
                    "invalid credentials")
    return "DEV_FORGE_AUTH" if any(m in lowered for m in auth_markers) else "DEV_FORGE"


# Word-boundary regexes, NOT substrings (audit D5 #4): plain `"signed out" in
# text` matches "de|signed out|put" / "as|signed out|side", and `"log in"`
# matches "back|log in|spection" — a false 12 pauses the whole Dev Type until a
# human re-uploads credentials, so the asymmetry says: never over-match.
HARNESS_AUTH_MARKERS = tuple(_re.compile(r"\b" + p + r"\b") for p in (
    # generic credential wording (any harness)
    "authentication", "unauthorized", "log in",
    # grok CLI revoked/expired-session wording — the two DISTINCTIVE phrases
    # (audit D5 #5): "signed out"/"please sign in" are dropped because generic
    # SSO/proxy stderr uses them and would false-trip a breaker. Without these
    # two a revoked grok cred exits 10 (three burned attempts) not 12.
    "not signed in", r"grok login",
))


# ADR-0018 §1.4 — the DISTINCTIVE subset of HARNESS_AUTH_MARKERS. Exit 12
# latches a per-Dev-Type breaker and pauses every mission for that Dev Type, so
# when the fault predicate has already explained a failure, only unambiguous
# credential evidence may override it. Bare "authentication"/"unauthorized" are
# excluded: OpenAI-compatible gateways emit them for ordinary rejections.
DISTINCTIVE_AUTH_MARKERS = tuple(_re.compile(r"\b" + p + r"\b") for p in (
    "not signed in", r"grok login",
))


def auth_evidence_is_distinctive(err_text: str, api_error_status=None) -> bool:
    """True when the credential evidence is strong enough to justify exit 12
    over a harness fault. A structured 401/403 beats any stderr wording."""
    if api_error_status in (401, 403):
        return True
    lowered = (err_text or "").lower()
    return any(rx.search(lowered) for rx in DISTINCTIVE_AUTH_MARKERS)


def classify_harness_failure(err_text: str) -> int:
    """Exit code for a nonzero harness exit: 12 (DEV_AUTH) only on credential
    wording, else 10 (DEV_CRASH). Markers are word-boundary-anchored on
    purpose (see above): a false 12 pauses the whole Dev Type, a false 10
    merely burns one attempt (docs/15 §4)."""
    lowered = err_text.lower()
    return 12 if any(rx.search(lowered) for rx in HARNESS_AUTH_MARKERS) else 10


# ── live output relay (docs/08 §4, docs/09 §2) ──────────────────────────────
# The harness's stdout is pumped line-by-line: the raw line is accumulated for
# end-of-run parsing, and a condensed human-readable rendering is (a) printed
# to THIS process's stdout — Dagu's container executor captures it live into
# the step log — and (b) batched into run.log envelopes for the admin panel.

LINE_LIMIT = 2000            # per condensed line
BATCH_LINES = 50             # per run.log envelope (50×2000 ≈ 100KB « 512KB)
FLUSH_SECS = 2.0
MAX_RELAY_LINES = 20_000     # flood guard; Dagu's step log still has everything
SILENCE_NOTICE_SECS = 60.0   # make a live-but-quiet harness observable


class LogRelay:
    """Thread-safe batcher for condensed lines → run.log. Best-effort only:
    send failures are swallowed — logging must never kill a run."""

    def __init__(self) -> None:
        self.buf: list[str] = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.sent = 0
        now = time.monotonic()
        self.started_at = now
        self.last_visible_output_at = now
        self.last_silence_notice_at = now

    def add(self, text: str, *, visible_output: bool = True) -> None:
        if self.sent >= MAX_RELAY_LINES:
            return
        with self.lock:
            if visible_output:
                self.last_visible_output_at = time.monotonic()
            for line in text.splitlines() or [""]:
                self.buf.append(line[:LINE_LIMIT])

    def add_silence_notice(self, harness: str, now: float | None = None) -> bool:
        """Queue at most one progress line per silence interval.

        Some harnesses emit only hidden reasoning events until their final answer.
        The process and heartbeat remain healthy, but an empty terminal looks hung.
        These notices report process liveness without exposing hidden reasoning.
        """
        now = time.monotonic() if now is None else now
        with self.lock:
            quiet_for = now - self.last_visible_output_at
            if (quiet_for < SILENCE_NOTICE_SECS
                    or now - self.last_silence_notice_at < SILENCE_NOTICE_SECS):
                return False
            elapsed = max(0, int(now - self.started_at))
            self.last_silence_notice_at = now
            self.buf.append(
                f"[devcake] {harness} is still running; "
                f"no visible model output for {int(quiet_for)}s "
                f"({elapsed}s elapsed)"
            )
        return True

    def flush(self) -> None:
        while True:
            with self.lock:
                batch, self.buf = self.buf[:BATCH_LINES], self.buf[BATCH_LINES:]
            if not batch:
                return
            if self.sent >= MAX_RELAY_LINES:
                with self.lock:
                    self.buf = []
                return
            if self.sent + len(batch) >= MAX_RELAY_LINES:
                batch = batch[:MAX_RELAY_LINES - self.sent]
                batch.append("[output relay capped — see the Dagu step log]")
            try:
                send("run.log", {"lines": batch})
                self.sent += len(batch)
            except Exception:
                return  # drop the batch, keep the run alive

    def loop(self) -> None:
        while not self.stop.wait(FLUSH_SECS):
            self.flush()
        self.flush()  # final drain after the pumps finish


def _pump(stream, sink: list, render, relay: LogRelay, echo) -> None:
    """Reader thread: drain one pipe fully (deadlock guard), echo + relay the
    condensed rendering of each line."""
    for raw in iter(stream.readline, ""):
        sink.append(raw)
        try:
            text = render(raw)
        except Exception:
            text = None
        if text:
            print(text, file=echo, flush=True)
            relay.add(text)
    finish = getattr(render, "finish", None)  # stateful renderers flush here
    if finish and (text := finish()):
        print(text, file=echo, flush=True)
        relay.add(text)
    stream.close()


def _progress_loop(proc, relay: LogRelay, harness: str) -> None:
    """Add sparse liveness notices while a harness is alive but silent."""
    while not relay.stop.wait(min(5.0, SILENCE_NOTICE_SECS)):
        if proc.poll() is not None:
            return
        relay.add_silence_notice(harness)


def render_claude(raw: str):
    """Claude Code stream-json events → condensed lines (shape verified live)."""
    try:
        ev = json.loads(raw)
    except Exception:
        s = raw.strip()
        return s[:LINE_LIMIT] if s else None
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return f"[claude] session {str(ev.get('session_id', ''))[:8]} · " \
               f"model={ev.get('model', '?')}"
    if kind == "assistant":
        parts = []
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(block["text"].strip()[:200])
            elif block.get("type") == "tool_use":
                args = json.dumps(block.get("input") or {})[:160]
                parts.append(f"→ {block.get('name', '?')} {args}")
        return "\n".join(parts) or None
    if kind == "result":
        cost = ev.get("total_cost_usd")
        line = f"[claude] done: {ev.get('subtype', '?')} · " \
               f"turns={ev.get('num_turns', '?')}"
        return line + (f" · cost=${cost:.2f}" if isinstance(cost, (int, float))
                       else "")
    return None  # thinking_tokens, rate_limit_event, tool results: noise


def render_codex(raw: str):
    """Codex exec --json JSONL events → condensed lines."""
    try:
        ev = json.loads(raw)
    except Exception:
        s = raw.strip()
        return s[:LINE_LIMIT] if s else None
    kind = ev.get("type")
    if kind == "item.completed":
        item = ev.get("item") or {}
        it = item.get("item_type") or item.get("type")
        if it == "command_execution":
            return f"$ {str(item.get('command', ''))[:160]} → " \
                   f"exit {item.get('exit_code', '?')}"
        if it == "agent_message":
            return str(item.get("text", "")).strip()[:200] or None
        return None  # reasoning etc.
    if kind == "turn.completed":
        u = ev.get("usage") or {}
        return f"[codex] turn done · in={u.get('input_tokens', '?')} " \
               f"out={u.get('output_tokens', '?')}"
    if kind == "error":
        return f"[codex] error: {str(ev.get('message', ''))[:200]}"
    if kind == "turn.failed":
        # THE terminal event of every captured codex failure — `… → error →
        # turn.failed` (test_harness_captures: all eight failure rows) — so the
        # last thing an operator sees before the run dies rendered as nothing.
        # The message repeats the preceding `error` event verbatim in every
        # capture; it is echoed anyway because the two are separate events and
        # nothing guarantees the first one rendered.
        msg = _one_line(_dict(ev.get("error")).get("message") or "", 200)
        return f"[codex] turn failed: {msg}" if msg else "[codex] turn failed"
    return None


class GrokCoalescer:
    """grok streaming-json emits token-level {"type":"text","data":…} deltas
    (verified live on 0.2.93) — coalesce them into lines; thoughts skipped.

    The type values present across the eleven 0.2.112 captures are exactly
    {`text`, `end`, `max_turns_reached`, `error`} (docs/08 §1; `thought` is a
    0.2.93 record and unverified at 0.2.112). The last three each decide a
    run's fate, so each of them renders a line.
    """

    def __init__(self) -> None:
        self.buf = ""

    def __call__(self, raw: str):
        try:
            ev = json.loads(raw)
        except Exception:
            s = raw.strip()
            return s[:LINE_LIMIT] if s else None
        kind = ev.get("type")
        if kind == "text":
            self.buf += ev.get("data", "")
            if "\n" in self.buf:
                emit, self.buf = self.buf.rsplit("\n", 1)
                return emit.strip() or None
            if len(self.buf) >= 200:
                emit, self.buf = self.buf, ""
                return emit
            return None
        if kind == "end":
            tail = self.buf.strip()
            self.buf = ""
            done = f"[grok] done: {ev.get('stopReason', '?')}"
            return f"{tail}\n{done}" if tail else done
        if kind == "error":
            # grok's terminal verdict, and the ONLY event a failed run emits:
            # `error` and `end` never co-occur across the eleven captures. It is
            # what `grok_run_fault` fires terminal_error on, so it must not be
            # invisible in the transcript. Flushes `self.buf` exactly like the
            # `end` arm — otherwise the partial last line of a streaming answer
            # is drained by the pump's `finish()` and lands AFTER the error,
            # which is the one place its ordering carries meaning.
            tail = self.buf.strip()
            self.buf = ""
            msg = _one_line(ev.get("message") or "", 200)
            line = f"[grok] error: {msg}" if msg else "[grok] error"
            return f"{tail}\n{line}" if tail else line
        if kind == "max_turns_reached":
            # bare `{"type":"max_turns_reached"}` (grok_turn_budget) — the event
            # `grok_run_fault` reads for exit 16. No flush here: `end` with
            # stopReason "Cancelled" follows it in the capture and flushes.
            return "[grok] turn cap reached (--max-turns)"
        return None  # unrecognized event types: noise

    def finish(self):
        tail, self.buf = self.buf.strip(), ""
        return tail or None


def render_stderr(raw: str):
    s = raw.strip()
    return f"! {s[:LINE_LIMIT]}" if s else None


def claude_result_event(out: str):
    """Last {"type":"result"} event in a stream-json transcript, or None."""
    found = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            found = ev
    return found


def grok_stream_parse(out: str):
    """(result_text, session_id) from streaming-json deltas; None if the
    output isn't grok stream events (e.g. an EXTRA_ARGS format override)."""
    texts, sid, saw = [], "", False
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "text":
            texts.append(ev.get("data", ""))
            saw = True
        elif kind == "end":
            sid = ev.get("sessionId", "") or sid
            saw = True
        elif kind == "thought":
            saw = True
    return ("".join(texts), sid) if saw else None


def grok_end_event(out: str):
    """Last {"type":"end"} event in a grok streaming-json transcript, or None.

    The sibling of `claude_result_event`. At 0.2.112 this event carries the
    whole token report inline — `usage`, `num_turns`, `modelUsage` (docs/08 §1,
    measured across every `grok_*` capture that reaches a terminal turn) — so
    the report needs neither a session id nor a filesystem read."""
    found = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — one unparseable line must never cost the whole report
            continue
        if isinstance(ev, dict) and ev.get("type") == "end":
            found = ev
    return found


def grok_end_report(ev):
    """TokenReport from grok's terminal event, or None when it carries no usage.

    Takes the streaming `end` event and the `--output-format json` blob alike:
    both carry the same `{usage, num_turns, modelUsage}` keys (docs/08 §1).

    Every read is guarded (`_dict`): this is model-adjacent nested data reached
    on the failure path too, and a token report must never abort the artifact
    path — the caller falls back, and INV-5 posts "unavailable" at worst.

    NO COST. grok emits no `total_cost_usd` and no cost field of any kind at
    0.2.112 (measured across all eleven captures), so `cost_usd` stays None. A
    0.0 here would read as "this run was free" in the feed report and would be
    aggregated as real spend in `devcake.cost.usd` (docs/12 §4).

    The captured token *values* came from a stub backend; the presence and key
    names of these fields are the CLI's (fixtures README)."""
    ev = _dict(ev)
    usage = _dict(ev.get("usage"))
    if not usage:
        return None                     # nothing measured — let the caller fall back
    mu = _dict(ev.get("modelUsage"))
    # dominant model, as the claude arm picks it — but grok's inner keys are
    # camelCase (`outputTokens`) and carry no per-model cost to rank by first
    models = sorted(mu, key=lambda k: _dict(mu[k]).get("outputTokens") or 0,
                    reverse=True)
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": None,               # never 0 — see above
        "model": models[0] if models else "grok",
        "extraction_method": "end_event",
        "num_turns": ev.get("num_turns"),
        "notes": f"reasoning_tokens={usage.get('reasoning_tokens')}",
    }


def grok_signals_report(session_id: str, home=None):
    """TokenReport from `signals.json` in grok's session directory, or None.

    The 0.2.93-verified path, kept as the FALLBACK for a stream with no usable
    `end` event. Its survival at 0.2.112 is a capture-campaign note with no
    committed fixture (docs/08 §1), so this asserts nothing either way: it
    reports what it finds on disk, or nothing. Totals only — no input/output
    split, no cost."""
    if not session_id:
        return None                     # an `error` event carries no sessionId
    root = home if home is not None else pathlib.Path.home()
    sig = None
    for p in root.glob(f".grok/sessions/*/{session_id}/signals.json"):
        sig = json.loads(p.read_text())
    sig = _dict(sig)
    if not sig:
        return None
    models = sig.get("modelsUsed")
    return {
        "total_tokens": sig.get("contextTokensUsed") or sig.get("totalTokens"),
        "model": models[0] if isinstance(models, list) and models else "grok",
        "extraction_method": "session_json",
        "num_turns": sig.get("turnCount"),
    }


def claude_text_dump(out: str) -> str:
    """ADR-0014 D1: every assistant-visible text block, in order, UNTRUNCATED.
    Thinking blocks, tool calls, and subagent messages (parent_tool_use_id —
    tool-internal chatter) are excluded: the dump is what the Dev said, not
    what it did or privately considered. Defensive on inner shapes — one odd
    line must never abort the artifact path."""
    blocks = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant" \
                or ev.get("parent_tool_use_id"):
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(text.strip("\n"))    # keep indentation intact
    return "\n\n".join(blocks)


def codex_text_dump(out: str) -> str:
    """ADR-0014 D1: every agent_message text, in order, untruncated."""
    blocks = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "item.completed":
            continue
        item = ev.get("item")
        if not isinstance(item, dict):
            continue
        if (item.get("item_type") or item.get("type")) == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(text.strip("\n"))
    return "\n\n".join(blocks)


# ── harness fault detection (ADR-0018) ───────────────────────────────────────
# In-band stream events decide failure; exit status alone is insufficient.
# See ADR-0018 and fixtures/harness_streams/README.md.

FAULT_TURN_BUDGET = "turn_budget"            # deterministic — never correlated
FAULT_TERMINAL_ERROR = "terminal_error"
FAULT_EMPTY_COMPLETION = "empty_completion"
FAULT_NO_TERMINAL_EVENT = "no_terminal_event"
FAULT_DETAIL_MAX = 400

# Bad-value allowlist: unknown terminal_reason must stay "not a fault".
CLAUDE_FAULT_TERMINAL_REASONS = frozenset({"api_error"})
# Empty by design: grok's stopReason enum is unverified, so it only ever
# annotates the detail — it must never decide (a refusal must not be a fault).
GROK_FAULT_STOP_REASONS = frozenset()


def _one_line(text: str, limit: int = FAULT_DETAIL_MAX) -> str:
    return " ".join(str(text).split())[:limit]


def _fault(reason: str, summary: str, evidence: str = "") -> dict:
    return {"reason": reason,
            "detail": _one_line(f"{summary} {evidence}" if evidence else summary)}


def _claude_activity(out: str) -> tuple:
    """(non-blank assistant text blocks, tool_use blocks). Structural — token
    counts cannot separate empty completion from tool-only work (fixtures README).
    Counts subagent blocks (more activity ⇒ more conservative).
    """
    texts = tools = 0
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — one unparseable line must never decide a run's fate
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        msg = ev.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tools += 1
            elif block.get("type") == "text" and str(block.get("text") or "").strip():
                texts += 1
    return texts, tools


def _dict(value) -> dict:
    """Defensive accessor for model-controlled nested shapes. `x or {}` rescues
    null/[]/{} but NOT a truthy non-dict, and a `usage: "none"` would then
    AttributeError out of the predicate — aborting fault detection for every
    run, including ones that were about to be judged healthy."""
    return value if isinstance(value, dict) else {}


def _claude_evidence(ev: dict, out: str, activity=None) -> str:
    usage = _dict(ev.get("usage"))
    mu = _dict(ev.get("modelUsage"))
    mu_out = sum(v.get("outputTokens") or 0 for v in mu.values() if isinstance(v, dict))
    texts, tools = activity if activity is not None else _claude_activity(out)
    return (f"(is_error={ev.get('is_error')} subtype={ev.get('subtype')!r} "
            f"terminal_reason={ev.get('terminal_reason')!r} "
            f"api_error_status={ev.get('api_error_status')} "
            f"num_turns={ev.get('num_turns')} duration_ms={ev.get('duration_ms')} "
            f"output_tokens={usage.get('output_tokens')} model_output_tokens={mu_out} "
            f"text_blocks={texts} tool_calls={tools})")


def claude_run_fault(out: str, harness_exit: int, *, dump: str = "",
                     result_event=None):
    """Claude Code: fault dict or None. Conservative by construction — a model
    REFUSAL is an ordinary assistant turn and must never be called a fault."""
    ev = result_event if result_event is not None else claude_result_event(out)
    if ev is None:                      # EXTRA_ARGS may override back to a blob
        try:
            blob = json.loads(out)
        except Exception:  # noqa: BLE001 — not JSON at all; that IS the no-terminal-event case
            blob = None
        # Must LOOK like a result blob. A stream truncated after a single
        # assistant event also parses as a dict, and treating that as the
        # terminal event would silently mask a truncated run.
        if isinstance(blob, dict) and any(
                k in blob for k in ("result", "subtype", "usage", "total_cost_usd")):
            ev = blob
    if ev is None:
        return _fault(FAULT_NO_TERMINAL_EVENT,
                      "claude stream ended without a result event",
                      f"(harness_exit={harness_exit} stdout={len(out)}B/"
                      f"{len(out.splitlines())}L)")

    subtype = str(ev.get("subtype") or "")
    terminal = str(ev.get("terminal_reason") or "")
    activity = _claude_activity(out)          # parsed ONCE — `out` can be tens
    evidence = _claude_evidence(ev, out, activity)   # of MB on the failure path

    # Turn budget first — never correlation-eligible.
    if terminal == "max_turns" or subtype == "error_max_turns":
        return _fault(FAULT_TURN_BUDGET,
                      f"claude stopped at the configured turn cap after "
                      f"{ev.get('num_turns', '?')} turns — raise --max-turns on this "
                      f"Mission Type's assignment in Config -> Assignments, or assign "
                      f"a stronger Dev Type", evidence)

    # subtype "error" prefix only (400 can carry subtype success + is_error true).
    status = ev.get("api_error_status")
    if (ev.get("is_error") or (isinstance(status, int) and status >= 400)
            or subtype.startswith("error") or terminal in CLAUDE_FAULT_TERMINAL_REASONS):
        return _fault(FAULT_TERMINAL_ERROR,
                      f"claude reported a terminal error: "
                      f"{str(ev.get('result') or terminal or subtype)[:120]}", evidence)

    # Empty success: no text, no tools, empty result/dump.
    texts, tools = activity
    if (not str(ev.get("result") or "").strip() and not (dump or "").strip()
            and texts == 0 and tools == 0):
        return _fault(FAULT_EMPTY_COMPLETION,
                      "claude returned no assistant output at all (no text, no tool "
                      "calls) and an empty final message", evidence)
    return None


def codex_run_fault(out: str, harness_exit: int, *, last_message: str = ""):
    """Codex: fault dict or None. `last_message` is the raw `-o` file (not
    `result_text`, which may be a stdout-tail fallback)."""
    completed = error_msg = None
    messages = items = item_errors = 0
    out_tokens = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — skip unparseable lines
            continue
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("type") or "")
        if kind == "turn.completed":
            completed = ev
            out_tokens = _dict(ev.get("usage")).get("output_tokens")
        elif kind == "error":
            error_msg = str(ev.get("message") or "")[:120]
        elif kind.startswith("turn.") and kind not in ("turn.started", "turn.completed"):
            # Unrecognized terminal turn.* (e.g. turn.failed) → failure signal.
            error_msg = error_msg or f"unrecognized terminal event {kind!r}"
        elif kind == "item.completed":
            item = _dict(ev.get("item"))
            item_type = item.get("item_type") or item.get("type")  # 0.144.4: type
            if item_type == "agent_message":
                if str(item.get("text") or "").strip():
                    messages += 1
            elif item_type == "error":
                # Metadata warning with -m (unknown model id) is NOT tool work.
                item_errors += 1
            elif item:
                # command_execution / file_change / … — tool activity
                items += 1
    tail = (f"(harness_exit={harness_exit} output_tokens={out_tokens} "
            f"agent_messages={messages} tool_items={items} "
            f"error_items={item_errors})")
    # turn.completed wins over error events before/after it.
    if completed is None:
        if error_msg is not None:
            return _fault(FAULT_TERMINAL_ERROR,
                          f"codex reported an error and never completed a turn: "
                          f"{error_msg}", tail)
        return _fault(FAULT_NO_TERMINAL_EVENT,
                      "codex stream ended without a turn.completed event", tail)
    if not messages and not items and not (last_message or "").strip():
        return _fault(FAULT_EMPTY_COMPLETION,
                      "codex completed a turn with no agent message, no tool "
                      "activity and an empty last-message file", tail)
    return None


_MARKDOWN_HEADING = _re.compile(r"^#{1,6}\s")


def grok_export_activity(dump: str, prompt: str = "") -> bool:
    """True if `grok export` shows content the run produced (not just prompt echo).

    Export always re-prints the prompt under `## User`. Locate the prompt
    verbatim, keep what follows, drop `#` headings. Fails safe toward "activity
    found" (no false exit 15). Heading-name heuristics are wrong: DevCake
    prompts embed `##` / `###` inside the User section.
    """
    body = dump or ""
    if not body.strip():
        return False
    anchor = (prompt or "").strip()
    if not anchor:
        return True         # nothing to anchor on — assume the run worked
    at = body.find(anchor)
    if at < 0:
        return True         # echo not located: the ambiguity must not become a fault
    tail = body[at + len(anchor):]
    return any(line.strip() and not _MARKDOWN_HEADING.match(line)
               for line in tail.splitlines())


def grok_run_fault(out: str, harness_exit: int, *, dump: str = "",
                   prompt: str = ""):
    """Grok: fault dict or None. Closed event set: text / end / max_turns_reached
    / error. Unrecognized types are not activity. `stopReason` only annotates
    (GROK_FAULT_STOP_REASONS empty by design — refusals must not be faults).
    """
    texts, stop, error_msg = [], "", ""
    budget = saw_event = False
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — one bad line must not decide the run
            continue
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("type") or "")
        if not kind:
            continue
        saw_event = True
        if kind == "text":
            texts.append(str(ev.get("data") or ""))
        elif kind == "end":
            stop = str(ev.get("stopReason") or "")
        elif kind == "max_turns_reached":
            budget = True
        elif kind == "error" and not error_msg:
            error_msg = str(ev.get("message") or "")
    text = "".join(texts)
    if not saw_event:
        if not out.strip():
            # CLI never ran (e.g. duplicate --output-format) → not a harness fault
            return None
        try:
            blob = json.loads(out)
        except Exception:  # noqa: BLE001 — neither stream nor blob
            blob = None
        if not isinstance(blob, dict):
            return _fault(FAULT_NO_TERMINAL_EVENT,
                          "grok stream ended without an end event",
                          f"(harness_exit={harness_exit} stdout={len(out)}B)")
        text, stop = str(blob.get("text") or ""), str(blob.get("stopReason") or "")
    tail = (f"(harness_exit={harness_exit} stopReason={stop!r} stdout={len(out)}B "
            f"text={len(text.strip())}B transcript={len(dump or '')}B)")

    if budget:
        return _fault(FAULT_TURN_BUDGET,
                      "grok stopped at the configured turn cap — raise --max-turns "
                      "on this Mission Type's assignment in Config -> Assignments, "
                      "or assign a stronger Dev Type", tail)
    if error_msg:
        return _fault(FAULT_TERMINAL_ERROR,
                      f"grok reported a terminal error: {error_msg[:120]}", tail)
    if stop and stop in GROK_FAULT_STOP_REASONS:      # empty set today, by design
        return _fault(FAULT_TERMINAL_ERROR, f"grok stopped with {stop!r}", tail)
    if not text.strip() and not grok_export_activity(dump, prompt):
        return _fault(FAULT_EMPTY_COMPLETION,
                      "grok produced nothing at all — no text deltas, and nothing "
                      "but the prompt echo in the session transcript", tail)
    return None


def harness_fault(harness: str, out: str, harness_exit: int, *, dump: str = "",
                  last_message: str = "", prompt: str = ""):
    """Did the harness actually work? Fault dict or None. An unknown harness
    name falls through to the claude predicate, mirroring main()'s renderer
    dispatch."""
    if harness == "codex":
        return codex_run_fault(out, harness_exit, last_message=last_message)
    if harness == "grok-build":
        # `dump` is the `grok export` transcript for this harness — forwarding
        # it is what keeps a silent-but-productive run from reading as empty,
        # and `prompt` is what separates the run's own output from the echo of
        # that prompt the export opens with (grok_export_activity).
        return grok_run_fault(out, harness_exit, dump=dump, prompt=prompt)
    return claude_run_fault(out, harness_exit, dump=dump)


# HTTP status from CLI transport wording (not model prose). Precision over
# recall: false 12 pauses a Dev Type; missed 401 falls through to 15.
HARNESS_STATUS_PATTERNS = {
    "codex": (_re.compile(r"unexpected status (\d{3})"),
              _re.compile(r"last status: (\d{3})")),
    "grok-build": (_re.compile(r"Unauthorized \((\d{3})\)"),
                   _re.compile(r"\(status (\d{3})")),
}


def harness_error_messages(out: str) -> list:
    """Messages from CLI error / turn.failed events only (not assistant text)."""
    messages = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — a bad line cannot be an error event we can read
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "error":
            messages.append(str(ev.get("message") or ""))
        elif ev.get("type") == "turn.failed":
            messages.append(str(_dict(ev.get("error")).get("message") or ""))
    return messages


def harness_api_error_status(harness: str, out: str):
    """HTTP status the harness reported (int), or None. Feeds auth precedence."""
    patterns = HARNESS_STATUS_PATTERNS.get(harness)
    if patterns is None:  # claude-code has api_error_status on the result event
        status = _dict(claude_result_event(out)).get("api_error_status")
        return status if isinstance(status, int) else None
    for message in harness_error_messages(out):
        for rx in patterns:
            hit = rx.search(message)
            if hit:
                return int(hit.group(1))
    return None


def classify_nonzero_exit(err_text: str, fault, api_error_status=None) -> tuple:
    """(exit code, error_class) for a nonzero harness exit — ADR-0018 §3.

    Order: turn_budget → distinctive auth → predicate → generic auth → crash.
    Distinctive 401/403 outranks the fault predicate so keys latch DEV_AUTH (12)
    instead of becoming correlation-eligible 15.
    """
    if fault and fault["reason"] == FAULT_TURN_BUDGET:
        return 16, "DEV_TURN_BUDGET"
    if auth_evidence_is_distinctive(err_text, api_error_status):
        return 12, "DEV_AUTH"
    if fault:
        return 15, "DEV_HARNESS_FAULT"
    if classify_harness_failure(err_text) == 12:
        return 12, "DEV_AUTH"
    return 10, "DEV_CRASH"


# ── failure evidence and result recovery (ADR-0018) ──────────────────────────

FORENSIC_MAX_ENTRIES = 20
FORENSIC_STDERR_TAIL = 500
# An entry-count cap alone does not bound the payload: json.dumps escapes each
# non-ASCII character to \uXXXX, so 20 accented 100-char names serialize to ~12 KB.
# Budget the total instead.
FORENSIC_LISTING_BUDGET = 800


def workspace_forensics(out_dir, harness_exit, out_bytes=0, out_lines_n=0,
                        stderr_tail="") -> dict:
    """Cheap, bounded post-mortem shipped on EVERY failure artifact: three
    syscalls, no recursion, under ~1 KB. Answers what a human previously could
    not answer from the mission feed alone — did the harness die on a signal,
    was anything written, was the directory writable, was the disk full, and
    was the channel we classify on (stderr) empty."""
    info = {"harness_exit": harness_exit,
            "stdout_bytes": out_bytes, "stdout_lines": out_lines_n,
            "stderr_bytes": len(stderr_tail or "")}
    listing, err = [], None
    try:
        with os.scandir(out_dir) as it:
            entries = sorted(it, key=lambda e: e.name)
        spent = 0
        for i, entry in enumerate(entries):
            if i >= FORENSIC_MAX_ENTRIES or spent >= FORENSIC_LISTING_BUDGET:
                listing.append(f"+{len(entries) - i} more")
                break
            try:
                row = f"{entry.name[:100]}:{entry.stat().st_size}"
            except OSError as e:
                row = f"{entry.name[:100]}:?({e.errno})"
            listing.append(row)
            spent += len(json.dumps(row))      # escaped length, not raw length
    except OSError as e:
        err = f"{getattr(e, 'errno', '?')}: {e.strerror or e}"
    info["out_listing"] = listing
    info["out_error"] = err
    info["out_writable"] = bool(os.access(str(out_dir), os.W_OK))
    try:
        info["workspace_free_mb"] = shutil.disk_usage(str(WORKSPACE)).free // (1024 * 1024)
    except OSError:
        info["workspace_free_mb"] = None
    if stderr_tail:
        info["stderr_tail"] = _one_line(stderr_tail, FORENSIC_STDERR_TAIL)
    return info


def bad_output_reason(exc: BaseException) -> str:
    """Split the single blanket `except` on the result.json read into causes a
    human can act on. FileNotFoundError is tested before OSError (it is a
    subclass), JSONDecodeError before the generic fallback."""
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, AssertionError):
        return "illegal_outcome" if "outcome" in str(exc) else "bad_summary"
    if isinstance(exc, OSError):
        return "unreadable"
    return "invalid"


def _git_tracked(workdir, path, runner=None) -> bool:
    """True when `path` is tracked by the repo clone's git index. EXECUTE tells
    the Dev to commit at the end, so a stray result.json in the work tree may
    already have been swept into the PR — a different question from "was it
    written during this run", which the mtime gate answers."""
    runner = runner or subprocess.run
    try:
        rel = pathlib.Path(path).relative_to(workdir)
    except ValueError:
        return False
    try:
        r = runner(["git", "-C", str(workdir), "ls-files", "--error-unmatch", str(rel)],
                   capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 — advisory annotation; a missing git must never fail a run
        return False
    return r.returncode == 0


def find_result_json(workspace, workdir, started_at: float, runner=None):
    """(path, note) — the canonical path first, then a FIXED candidate list.

    Deliberately no traversal: there is no depth parameter to get wrong and no
    way to enumerate the repo tree. A non-canonical hit is reported only when
    its mtime is at or after harness start, so a result.json that was already
    in the clone (a fixture, a project's own artifact) can never be adopted."""
    workspace, workdir = pathlib.Path(workspace), pathlib.Path(workdir)
    canonical = workspace / "out" / "result.json"
    try:
        # NOT `canonical.exists()`: on Python 3.12 (the pinned image base) that
        # swallows only ENOENT/ENOTDIR/EBADF/ELOOP and lets EACCES propagate —
        # an unreadable out/ would raise straight out of the recovery helper
        # that exists to enrich the failure path.
        if canonical.is_file():
            return canonical, ""
    except OSError:
        return canonical, ""            # unreadable: let the caller's read report it
    for cand in (workspace / "result.json", workspace / "repo" / "result.json",
                 workdir / "result.json", workdir / "out" / "result.json"):
        try:
            if cand.is_symlink():
                # `stat()` follows links, so both the freshness gate and the
                # content would come from the TARGET — a symlink is a way out of
                # the fixed candidate list and out of the workspace entirely.
                continue
            st = cand.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue                    # a directory named result.json is not one
        if st.st_mtime < started_at:
            continue                    # predates the harness — not this run's
        ts = datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()
        note = (f"[devcake] result.json is not at /workspace/out/result.json — found "
                f"one at {cand} (mtime {ts}). The playbook requires the canonical "
                f"path; fix the prompt.")
        if _git_tracked(workdir, cand, runner=runner):
            note += (" This file is ALSO tracked by git — check the PR for a stray "
                     "result.json.")
        return cand, note
    return None, ""


def _safe_activity_relpath(path: str):
    """Mirror of the app's safe_activity_relpath: reject zip-slip / absolute
    / empty paths. Returns a posix-relative string or None."""
    if not path or not isinstance(path, str):
        return None
    raw = path.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("~"):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if len(parts) > 20 or any(len(p) > 200 for p in parts):
        return None
    return "/".join(parts)


def write_activity_payload(act: dict, dest: pathlib.Path) -> None:
    """ADR-0014 D3: materialize the activity payload into the folder —
    MISSION.md (when the app sent one; old apps don't), ACTIVITY.md, and
    every attachment. Paths may be nested (zip extracts under `{stem}/`);
    unsafe / escaping paths fall back to a basename or `attachment.bin`."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_res = dest.resolve()
    if act.get("mission_md"):
        (dest / "MISSION.md").write_text(act["mission_md"])
    (dest / "ACTIVITY.md").write_text(act.get("activity_md", ""))
    for a in act.get("attachments", []):
        raw = a.get("filename") or "attachment.bin"
        rel = _safe_activity_relpath(raw)
        if rel is None:
            rel = pathlib.Path(str(raw).replace("\\", "/")).name
            if not rel or rel in (".", ".."):
                rel = "attachment.bin"
        target = (dest / rel).resolve()
        # zip-slip: must stay under dest
        try:
            target.relative_to(dest_res)
        except ValueError:
            target = dest_res / "attachment.bin"
        data = base64.b64decode(a["content_b64"])
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError:
            # file-vs-directory collision (an old app can still send a flat
            # name and a same-named extraction dir) or any other tree
            # conflict: flatten — the mirror is advisory and must never
            # kill the run
            try:
                (dest_res / ("conflict-" + rel.replace("/", "__"))
                 ).write_bytes(data)
            except OSError:
                print(f"activity attachment skipped (unwritable): {rel}",
                      file=sys.stderr)


def clone_activity_repo(activity, dest, runner=None):
    """ADR-0014 D4: clone the mission's activity repo — FULL history (the
    step-by-step evolution IS the payload: `git log -p ACTIVITY.md` works
    in-container) with the shared RO token via the askpass env override.
    Non-fatal on every failure; (ok, note)."""
    import re as _re
    if not activity or not activity.get("url"):
        return False, "activity repo: no clone spec (Redis fallback)"
    runner = runner or subprocess.run
    url = activity["url"]
    user = activity.get("clone_user") or ""
    clone_url = (_re.sub(r"^(https?://)", rf"\g<1>{user}@", url)
                 if user else url)
    env = {**os.environ, "DEVCAKE_FORGE_TOKEN": activity.get("token") or ""}
    r = runner(["git", "clone", clone_url, str(dest)],
               capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return False, ("activity repo: clone failed "
                       f"({(r.stderr or '')[-200:]}) — Redis fallback")
    return True, "activity repo: cloned with history"


def materialize_activity(spec, dest, request_reply, runner=None):
    """Clone-first activity materialization (ADR-0014 D4), Redis fallback:
    activity.get + payload write when the clone failed OR left no
    ACTIVITY.md (empty repo — the first push failed; cloning an empty repo
    succeeds). Never fatal, never exit 13 (that's the primary repo's)."""
    notes = []
    ok, note = clone_activity_repo(spec.get("activity_repo"), dest,
                                   runner=runner)
    notes.append(note)
    if not ok or not (dest / "ACTIVITY.md").exists():
        if ok:
            notes.append("activity repo: empty clone — Redis fallback")
        # drop any zero-commit .git so `git log` inside the folder fails
        # honestly instead of confusingly ("no commits yet" over real files)
        shutil.rmtree(dest / ".git", ignore_errors=True)
        act = request_reply("activity.get", "activity.result")
        write_activity_payload(act, dest)
    return notes


def with_session(text: str, dump: str) -> str:
    """Failure-path transcripts: append the session dump when one exists."""
    return text + (f"\n\n## Session transcript\n\n{dump}" if dump else "")


def assemble_transcript(seq, mtype, run_id, dev_type, harness, token_report,
                        dump, result_text, result) -> str:
    """ADR-0014 D1: the attachment doc — header, the FULL session dump (all
    assistant-visible text), the outcome JSON. `## Agent report` (last message
    alone) appears only when no dump exists; the feed comment carries the last
    message, so the attachment need not repeat it. result=None (failure paths)
    drops the Outcome section."""
    body = (f"## Session transcript\n\n{dump}\n\n" if dump
            else f"## Agent report\n\n{result_text}\n\n")
    return (
        f"# {seq}_{mtype} — run {run_id}\n\n"
        f"**Dev:** {dev_type} ({harness}) · "
        f"**turns:** {token_report.get('num_turns', '—')} · "
        f"**duration:** {token_report.get('duration_ms', '—')} ms\n\n"
        + body
        + (f"## Outcome\n\n```json\n{json.dumps(result, indent=2)}\n```\n"
           if result is not None else ""))


def request_reply(kind: str, want: str, timeout: int = 90) -> dict:
    send(kind, {})
    last_id, deadline = "0", time.time() + timeout
    while time.time() < deadline:
        for _s, msgs in r.xread({REPLY: last_id}, block=5000, count=10) or []:
            for entry_id, fields in msgs:
                last_id = entry_id
                env = json.loads(fields["m"])
                if env.get("kind") == want:
                    return env["payload"]
                if env.get("kind") == "runspec.error":
                    print(env.get("payload", {}).get("error", "run spec unavailable"),
                          file=sys.stderr)
                    sys.exit(20)
    print(f"{kind} timed out", file=sys.stderr)
    sys.exit(20)


def heartbeat_loop(stop: threading.Event) -> None:
    send("run.heartbeat", {"phase": "starting"})   # immediate first beat: a kill in the
    while not stop.wait(30):                       # first 30s must still be detectable
        try:
            send("run.heartbeat", {"phase": "working"})
        except Exception:
            pass


def forge_dialect(env: dict) -> tuple:
    """(clone_user, git_name, git_email, cli_token_envs) for the clone
    bootstrap. Values come from the app's ForgeDescriptor via spec_env
    (docs/06, docs/07). App and images deploy in lockstep (docs/13 §8), so
    every var is always present — a KeyError here means a mismatched build
    and should crash the run loudly."""
    cli_envs = [e for e in env.get("DEVCAKE_FORGE_CLI_ENVS", "").split(",") if e]
    return (env["DEVCAKE_CLONE_USER"], env["DEVCAKE_GIT_NAME"],
            env["DEVCAKE_GIT_EMAIL"], cli_envs)


def harness_argv(harness: str, prompt: str, *, plan_mode: bool = False,
                 model: str = "", extra=(), out_dir=None) -> list:
    """The harness command line (docs/08 §1) — ONE definition.

    Extracted from `main()` so the capture rig (`scripts/harness_capture/`)
    builds argv through the SAME code path production uses. A fixture captured
    with even slightly different flags silently stops corresponding to what the
    predicate sees on a real run, and that divergence is invisible in review —
    the stream still looks plausible.

    `out_dir` defaults to /workspace/out (codex's `-o` target); the capture rig
    points it at a throwaway directory.
    """
    extra = list(extra)
    out = pathlib.Path(out_dir) if out_dir is not None else WORKSPACE / "out"
    if harness == "grok-build":
        mode = ["--permission-mode", "plan"] if plan_mode else ["--always-approve"]
        pin = ["--model", model] if model else []
        return ["grok", "-p", prompt, "--output-format", "streaming-json",
                *mode, *pin, *extra]
    if harness == "codex":
        mode = (["--sandbox", "read-only"] if plan_mode
                else ["--dangerously-bypass-approvals-and-sandbox"])
        pin = ["-m", model] if model else []
        return ["codex", "exec", prompt, "--json",
                "-o", str(out / "last_message.txt"),
                "--skip-git-repo-check", *mode, *pin, *extra]
    mode = (["--permission-mode", "plan"] if plan_mode
            else ["--dangerously-skip-permissions"])
    pin = ["--model", model] if model else []
    # --verbose is REQUIRED with -p + stream-json (the CLI errors out without it)
    return ["claude", "-p", prompt, "--output-format", "stream-json",
            "--verbose", *mode, *pin, *extra]


def main() -> None:
    spec = request_reply("runspec.get", "runspec.result")
    send("runspec.ack", {})
    env = spec.get("env", {})
    os.environ.update(env)
    for f in spec.get("credential_files", []):
        p = pathlib.Path(os.path.expanduser(f["path_hint"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"])
        p.chmod(0o600)
    prompt = spec.get("prompt", "")

    send("run.started", {"container_hostname": os.uname().nodename})
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()

    # ── OAuth helper mode (docs/08 §4): device-code login, not a mission run ──
    if env.get("DEVCAKE_OAUTH_MODE"):
        import re as _re
        cmd = env["DEVCAKE_OAUTH_LOGIN_CMD"].split()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
        ansi = _re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        url = code = None
        for raw in proc.stdout:
            print(raw, end="")
            line = ansi.sub("", raw)                 # harness CLIs colorize output
            if url is None:
                m = _re.search(r"https://[^\s\x1b]+", line)
                if m and ("user_code=" in m.group(0) or "device" in m.group(0)):
                    url = m.group(0).rstrip(".,)")
                    cm = _re.search(r"user_code=([A-Z0-9-]+)", url)
                    code = cm.group(1) if cm else None
            if code is None:                         # codex prints the code on its own line
                cm = _re.search(r"\b([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\b", line)
                if cm and "http" not in line:
                    code = cm.group(1)
            if url and (code or "user_code=" in url) and not getattr(main, "_sent", False):
                main._sent = True
                send("run.log", {"oauth_url": url, "code": code})
        proc.wait()
        if proc.returncode != 0:
            send("run.log", {"oauth_error": f"login exited {proc.returncode}"})
            stop.set()
            sys.exit(12)
        auth = pathlib.Path(os.path.expanduser(env["DEVCAKE_OAUTH_AUTH_PATH"]))
        send("oauth.result", {"content": auth.read_text()})
        stop.set()
        print("oauth login captured")
        return

    # ── workspace prep (docs/07 §1) ──────────────────────────────────────────
    (WORKSPACE / "out").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "activity").mkdir(parents=True, exist_ok=True)
    (WORKSPACE / ".devcake").mkdir(parents=True, exist_ok=True)

    repo_url = env["DEVCAKE_REPO_URL"]
    askpass = WORKSPACE / ".devcake" / "askpass.sh"
    askpass.write_text("#!/bin/sh\necho \"$DEVCAKE_FORGE_TOKEN\"\n")
    askpass.chmod(0o700)
    # git auth set up BEFORE the activity clone (ADR-0014 D4) — its per-clone
    # token env override rides the same askpass
    os.environ["GIT_ASKPASS"] = str(askpass)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"

    for note in materialize_activity(spec, WORKSPACE / "activity",
                                     request_reply):
        print(note)

    clone_user, git_name, git_email, cli_envs = forge_dialect(env)
    clone_url = repo_url.replace("https://", f"https://{clone_user}@")
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo_dir = WORKSPACE / "repo"  # canonical path; dir inside named after the repo
    # git auth for clone AND the harness's own push (docs/03 §3, askpass set
    # above); CLI auth for PRs
    if env.get("DEVCAKE_FORGE_TOKEN"):
        for var in cli_envs:
            os.environ[var] = env["DEVCAKE_FORGE_TOKEN"]
    subprocess.run(["git", "config", "--global", "user.name", git_name],
                   capture_output=True)
    subprocess.run(["git", "config", "--global", "user.email", git_email],
                   capture_output=True)
    clone = subprocess.run(
        ["git", "clone", clone_url, str(repo_dir / repo_name)],
        capture_output=True, text=True)
    if clone.returncode != 0:
        detail = clone.stderr[-2000:]
        error_class = clone_error_class(detail)
        print("clone failed:", detail[-500:], file=sys.stderr)
        send_artifacts({"result": None, "exit_code": 13,
                        "error_class": error_class, "error_detail": detail,
                        "transcript_md": f"clone failed:\n{detail}",
                        "token_report": {"extraction_method": "unavailable", "model": None}})
        sys.exit(13)
    workdir = repo_dir / repo_name

    # multi-repo ONBOARD triage (item 2): sibling read-only clones — the
    # playbook's repo_options section names them; failures are non-fatal
    for note in clone_extra_repos(spec.get("extra_repos") or [], repo_dir):
        print(note)

    # skill store: materialize selected skills into the harness's registry-
    # declared skills dir — NOT into the repo clone (the Dev would commit
    # them, and codex scans repo .agents/skills)
    for note in install_skills(spec.get("skills") or [],
                               skills_dir=spec.get("skills_dir")
                               or ".claude/skills"):
        print(note)

    failed = run_mcp_setup(spec.get("mcp_setup_commands", []), workdir)  # docs/07 §5 step 5
    if failed:
        cmd, detail = failed
        # `cmd` is the raw config string ($VAR unexpanded — no secret can
        # appear); artifacts mirror the exit-13 clone block so the app maps
        # the failure to a visible DEV_MCP_SETUP run error
        print("mcp setup failed:", cmd, detail[-300:], file=sys.stderr)
        send_artifacts({"result": None, "exit_code": 14,
                        "error_class": "DEV_MCP_SETUP",
                        "error_detail": f"{cmd}: {detail}",
                        "transcript_md": (f"MCP setup command failed:\n`{cmd}`"
                                          f"\n\n```\n{detail}\n```"),
                        "token_report": {"extraction_method": "unavailable", "model": None}})
        sys.exit(14)

    # ── telemetry (stage-2 creds — docs/07 §3) ───────────────────────────────
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "devcake-dev"}))
    # unauthenticated: the endpoint is the stack's otel-collector, which alone
    # holds the OpenObserve credentials — Devs carry none (ISSUES #13)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=env["OTEL_EXPORTER_OTLP_ENDPOINT"])))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("devcake-dev")
    ctx = extract({"traceparent": TRACEPARENT}) if TRACEPARENT else None

    # ── harness (docs/08 §§1,3) ──────────────────────────────────────────────
    harness = os.environ.get("DEVCAKE_HARNESS", "claude-code")
    plan_mode = env.get("DEVCAKE_MISSION_TYPE") == "PLAN"
    extra = shlex.split(env.get("DEVCAKE_EXTRA_ARGS", ""))
    model = env.get("DEVCAKE_MODEL", "").strip()  # per-DevType pin; "" = harness default
    cmd = harness_argv(harness, prompt, plan_mode=plan_mode, model=model,
                       extra=extra)
    harness_exit = 1
    out_lines: list[str] = []
    err_lines: list[str] = []
    relay = LogRelay()
    render = {"codex": render_codex,
              "grok-build": GrokCoalescer()}.get(harness, render_claude)
    with tracer.start_as_current_span("dev.run", context=ctx) as span:
        span.set_attribute("devcake.run.id", RUN_ID)
        span.set_attribute("devcake.dev_type", env.get("DEVCAKE_DEV_TYPE", ""))
        span.set_attribute("devcake.harness", harness)
        with tracer.start_as_current_span("harness.exec"):
            # the freshness gate for misplaced-result recovery: anything older
            # than this was in the clone before the harness ran (ADR-0018)
            harness_started_at = time.time()
            proc = subprocess.Popen(cmd, cwd=workdir, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
            relay.add(f"[devcake] {harness} started; waiting for model output",
                      visible_output=False)
            pumps = [threading.Thread(target=_pump, daemon=True, args=(
                         proc.stdout, out_lines, render, relay, sys.stdout)),
                     threading.Thread(target=_pump, daemon=True, args=(
                         proc.stderr, err_lines, render_stderr, relay, sys.stderr))]
            flusher = threading.Thread(target=relay.loop, daemon=True)
            progress = threading.Thread(target=_progress_loop, daemon=True,
                                        args=(proc, relay, harness))
            for t in (*pumps, flusher, progress):
                t.start()
            harness_exit = proc.wait()
            for t in pumps:
                t.join(timeout=10)
            relay.stop.set()
            flusher.join(timeout=10)
            progress.join(timeout=10)
        span.set_attribute("devcake.outcome", "harness_exit_%d" % harness_exit)
    provider.force_flush()
    out, err_text = "".join(out_lines), "".join(err_lines)
    out_bytes, out_lines_n = len(out), len(out_lines)
    # The list holds the same bytes as `out` PLUS per-line object overhead and a
    # pointer array — the larger of the two live copies, and this is the moment
    # the artifact path starts allocating. The pumps were joined above.
    out_lines.clear()
    err_lines.clear()

    # ── token extraction + result text (docs/08 §5) ──────────────────────────
    token_report = {"extraction_method": "unavailable", "model": harness}
    result_text, transcript_body = "", ""
    codex_last = ""      # RAW `-o` content: result_text is overwritten with a
    #                      stdout tail on any parse failure, so the fault
    #                      predicate must not read it (fixtures README)
    if harness == "codex":
        try:
            last = WORKSPACE / "out" / "last_message.txt"
            codex_last = last.read_text() if last.exists() else ""
            result_text = codex_last
            for line in out.splitlines():           # JSONL events (verified 0.144.1)
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("type") == "turn.completed":
                    u = ev.get("usage") or {}
                    token_report = {
                        "input_tokens": u.get("input_tokens"),
                        "output_tokens": u.get("output_tokens"),
                        "cache_read_tokens": u.get("cached_input_tokens"),
                        "model": "codex",
                        "extraction_method": "session_json",
                        "notes": f"reasoning_output_tokens={u.get('reasoning_output_tokens')}",
                    }
            if not result_text:
                result_text = out[-4000:]
        except Exception:
            result_text = out[-4000:]
    elif harness == "grok-build":
        sid, terminal = "", None    # `terminal`: the event carrying usage/turns
        try:
            parsed = grok_stream_parse(out)
            if parsed is not None:
                result_text, sid = parsed
                terminal = grok_end_event(out)
            else:  # EXTRA_ARGS overrode the format back to a plain json blob
                j = json.loads(out)
                result_text = j.get("text") or ""
                sid = j.get("sessionId") or ""
                terminal = j       # same {usage, num_turns, modelUsage} keys
        except Exception:
            result_text = out[-4000:]
        # Token report — its own guard, because a failure here must cost only
        # the report (INV-5 then posts "unavailable"), never the result text or
        # the transcript. The `end` event is PREFERRED: at 0.2.112 it carries
        # the full split inline, needing no session id and no filesystem read
        # (docs/08 §5). `signals.json` stays as the fallback — its survival at
        # this version is an uncommitted campaign note, so dropping it would be
        # as much of a guess as relying on it.
        try:
            token_report = (grok_end_report(terminal)
                            or grok_signals_report(sid) or token_report)
        except Exception:  # noqa: BLE001 — the artifact path outranks its own token report
            print("token extraction failed; reporting unavailable", file=sys.stderr)
        try:
            # no sessionId ⇒ nothing to export: an `error` event never carries
            # one, and the export is the only grok dump source (docs/08 §6)
            exp = (subprocess.run(["grok", "export", sid], capture_output=True,
                                  text=True) if sid else None)
            if exp is not None and exp.returncode == 0 and exp.stdout.strip():
                transcript_body = exp.stdout
        except Exception:  # noqa: BLE001 — no export ⇒ no dump; the fault predicate handles an empty one
            print("grok export failed; transcript falls back to the agent report",
                  file=sys.stderr)
    else:
        try:
            # stream-json: the final result event carries the exact fields of
            # the old --output-format json blob (verified live); blob fallback
            # covers an EXTRA_ARGS format override
            j = claude_result_event(out) or json.loads(out)
            usage = j.get("usage") or {}
            mu = j.get("modelUsage") or {}
            def _weight(v):  # dominant model = the one that cost/produced the most
                return (v.get("costUSD") or 0, v.get("outputTokens") or 0)                     if isinstance(v, dict) else (0, 0)
            models = sorted(mu, key=lambda k: _weight(mu[k]), reverse=True)
            token_report = {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "cost_usd": j.get("total_cost_usd"),
                "model": models[0] if models else "claude-code",
                "extraction_method": "session_json",
                "num_turns": j.get("num_turns"),
                "duration_ms": j.get("duration_ms"),
            }
            result_text = j.get("result") or ""
        except Exception:
            result_text = out[-4000:]

    # ADR-0014 D1: the full dump of assistant-visible text, per harness
    # (grok: the `grok export` session already includes every message).
    # Guarded like every other parse of `out` — a dump failure must never
    # abort the artifact path (the no-dump fallback handles "").
    try:
        if harness == "codex":
            dump = codex_text_dump(out)
        elif harness == "grok-build":
            dump = transcript_body
        else:
            dump = claude_text_dump(out)
    except Exception:
        dump = ""

    # ── harness verdict (ADR-0018) ───────────────────────────────────────────
    # Did the harness actually work? The process exit status alone cannot say:
    # a saturated backend answering 200-with-nothing exits 0, and stderr — the
    # channel classify_harness_failure reads — is empty on every failure we
    # measured. Compute this BEFORE `out` is released.
    fault = harness_fault(harness, out, harness_exit, dump=dump,
                          last_message=codex_last, prompt=prompt)
    # Every harness, not just claude: codex and grok expose no structured status
    # field, so without this a 401 on either lands on 15 (excusable, correlation-
    # eligible) instead of 12 (latch the auth breaker, tell the operator).
    api_status = harness_api_error_status(harness, out)
    forensics = workspace_forensics(WORKSPACE / "out", harness_exit, out_bytes,
                                    out_lines_n, err_text[-FORENSIC_STDERR_TAIL:])
    # `out` is not read again below; releasing the joined copy here keeps the
    # peak off the artifact path, where the payload is serialized repeatedly.
    out = ""

    def fail(code: int, error_class: str, detail: str, transcript: str,
             **extra) -> None:
        payload = {"result": None, "exit_code": code, "error_class": error_class,
                   "error_detail": _one_line(detail, 500), "evidence": forensics,
                   "transcript_md": with_session(
                       f"{transcript}\n\n```json\n"
                       f"{json.dumps(forensics, indent=2)}\n```", dump),
                   "token_report": token_report}
        payload.update(extra)
        send_artifacts(payload)
        stop.set()
        sys.exit(code)

    if harness_exit != 0:
        err = err_text[-1500:]
        # the whole rule lives in the pure helper (docs/15 §4 asymmetry: a false
        # 12 pauses an entire Dev Type) — this only renders it
        code, error_class = classify_nonzero_exit(err, fault, api_status)
        if code == 16:
            fail(16, error_class, fault["detail"],
                 f"harness exited {harness_exit} — turn budget exhausted\n\n"
                 f"{fault['detail']}\n\n```\n{err}\n```")
        if code == 12:
            # a revoked credential leaves stderr EMPTY, so the in-band status is
            # the only detail there is to name
            fail(12, error_class, err or f"api_error_status={api_status}",
                 f"harness exited {harness_exit}\n\n```\n{err}\n```")
        if code == 15:
            fail(15, error_class, fault["detail"],
                 f"harness exited {harness_exit} — {fault['reason']}\n\n"
                 f"{fault['detail']}\n\n```\n{err}\n```")
        fail(10, error_class, err or f"harness exited {harness_exit}",
             f"harness exited {harness_exit}\n\n```\n{err}\n```")

    # ── result.json (docs/03 §6) ─────────────────────────────────────────────
    # Plan mode is read-only — the harness cannot write files, so the entrypoint
    # materializes PLAN.md and result.json from the returned plan text (docs/08 §3)
    if plan_mode:
        # The fault check runs BEFORE materialization: plan mode's result.json
        # is synthesized from `result_text`, so a backend that returned junk
        # would otherwise be laundered into a "planned" outcome.
        if fault:
            code = 16 if fault["reason"] == FAULT_TURN_BUDGET else 15
            cls = "DEV_TURN_BUDGET" if code == 16 else "DEV_HARNESS_FAULT"
            fail(code, cls, fault["detail"],
                 f"plan mode: {fault['reason']}\n\n{fault['detail']}")
        if len((result_text or "").strip()) < 200:  # a real plan is never this short
            fail(11, "DEV_BAD_OUTPUT",
                 f"plan mode returned {len(result_text or '')} chars",
                 f"plan mode returned no usable plan "
                 f"({len(result_text or '')} chars):\n\n{result_text}",
                 bad_output_reason="empty_plan")
        (WORKSPACE / "out" / "PLAN.md").write_text(result_text)
        (WORKSPACE / "out" / "result.json").write_text(json.dumps({
            "schema_version": 1, "outcome": "planned",
            "summary": result_text.strip().splitlines()[0][:300]}))
    result_path = WORKSPACE / "out" / "result.json"
    # per-type legality (docs/03 §6). First-line defense only — the app enforces
    # the same table authoritatively at finalization (missions.LEGAL_OUTCOMES).
    legal_outcomes = {
        "ONBOARD": {"plan_needed", "decomposed", "human_needed"},
        "PLAN": {"planned"},
        "EXECUTE": {"executed", "human_needed"},
        "REVIEW": {"reviewed", "human_needed"},
        "MAPPER": {"relations_mapped"},
    }
    legal = legal_outcomes.get(env.get("DEVCAKE_MISSION_TYPE", ""),
                               set().union(*legal_outcomes.values()))
    def load_result(path):
        loaded = json.loads(pathlib.Path(path).read_text())
        assert loaded.get("outcome") in legal, \
            f"outcome {loaded.get('outcome')!r} illegal for {env.get('DEVCAKE_MISSION_TYPE')}"
        assert isinstance(loaded.get("summary"), str)
        return loaded

    recovered_path, recovery_note = None, ""
    try:
        result = load_result(result_path)          # row 5 — canonical wins
    except Exception as e:
        reason = bad_output_reason(e)
        # Diagnosis is UNCONDITIONAL: whether or not recovery is enabled, a
        # misplaced result.json is named in the artifact, the transcript and the
        # live run terminal, so the prompt can be fixed.
        stray, note = find_result_json(WORKSPACE, workdir, harness_started_at)
        if note:
            print(note, file=sys.stderr)
            try:
                send("run.log", {"lines": [note]})
            except Exception:  # noqa: BLE001 — advisory relay line; never fail a run over it
                pass
            reason = "misplaced"
        # rows 6/7 — the harness's own verdict beats any recovered file, so a
        # backend fault plus a stray can never manufacture a PMO transition
        if fault:
            code = 16 if fault["reason"] == FAULT_TURN_BUDGET else 15
            cls = "DEV_TURN_BUDGET" if code == 16 else "DEV_HARNESS_FAULT"
            fail(code, cls, f"{fault['detail']} | {note}" if note else fault["detail"],
                 f"{fault['reason']}: no usable result.json ({e})\n\n"
                 f"{note}\n\n{fault['detail']}" if note
                 else f"{fault['reason']}: no usable result.json ({e})\n\n"
                      f"{fault['detail']}")
        # row 8 — recovery, opt-out via config (default on). `stray` can be the
        # canonical path itself when that file exists but is unreadable/invalid;
        # re-reading it would only reproduce the same error.
        if (stray is not None and stray != result_path
                and env.get("DEVCAKE_RECOVER_MISPLACED_RESULT")):
            try:
                result = load_result(stray)
                recovered_path, recovery_note = str(stray), note
                print(f"[devcake] recovered result.json from {stray}", file=sys.stderr)
            except Exception as e2:                # the stray is no better
                fail(11, "DEV_BAD_OUTPUT", f"{note} | recovered file invalid: {e2}",
                     f"result.json missing/invalid: {e}\n\n{note}\n\n"
                     f"recovered file also invalid: {e2}\n\n---\n\n{result_text}",
                     bad_output_reason=bad_output_reason(e2))
        else:                                      # row 9
            fail(11, "DEV_BAD_OUTPUT", f"{e}{' | ' + note if note else ''}",
                 f"result.json missing/invalid: {e}"
                 + (f"\n\n{note}" if note else "")
                 + f"\n\n---\n\n{result_text}",
                 bad_output_reason=reason)

    plan_path = WORKSPACE / "out" / "PLAN.md"
    transcript = assemble_transcript(
        seq=env.get("DEVCAKE_SEQ"), mtype=env.get("DEVCAKE_MISSION_TYPE"),
        run_id=RUN_ID, dev_type=env.get("DEVCAKE_DEV_TYPE"), harness=harness,
        token_report=token_report, dump=dump, result_text=result_text,
        result=result)
    # ADR-0014 D1: the last message rides separately for the inline feed
    # comment; the app treats a missing/empty key as "post the pointer only"
    payload = {"result": result, "transcript_md": transcript,
               "last_message_md": result_text, "token_report": token_report}
    if recovered_path:
        # The run succeeds, so there is no failure artifact to carry the
        # evidence — the transcript is the only durable surface, and it is
        # always posted (INV-5). A live relay line alone would vanish.
        payload["recovered_result_path"] = recovered_path
        payload["transcript_md"] = (
            f"> {recovery_note}\n\n" + payload["transcript_md"])
    if plan_path.exists():
        payload["plan_md"] = plan_path.read_text()
    send_artifacts(payload)
    stop.set()
    print(f"dev {RUN_ID} done: {result.get('outcome')}")


if __name__ == "__main__":
    main()
