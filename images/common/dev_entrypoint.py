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
import shlex
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
    return None


class GrokCoalescer:
    """grok streaming-json emits token-level {"type":"text","data":…} deltas
    (verified live on 0.2.93) — coalesce them into lines; thoughts skipped."""

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
        return None  # thought deltas: noise

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


def claude_text_dump(out: str) -> str:
    """ADR-0014 D1: every assistant-visible text block, in order, UNTRUNCATED.
    Thinking blocks and tool calls are excluded — the dump is what the model
    said, not what it did or privately considered."""
    blocks = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict) or ev.get("type") != "assistant":
            continue
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                blocks.append(block["text"].strip())
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
        item = ev.get("item") or {}
        if (item.get("item_type") or item.get("type")) == "agent_message":
            text = str(item.get("text", "")).strip()
            if text:
                blocks.append(text)
    return "\n\n".join(blocks)


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

    act = request_reply("activity.get", "activity.result")
    (WORKSPACE / "activity" / "ACTIVITY.md").write_text(act.get("activity_md", ""))
    for a in act.get("attachments", []):
        target = WORKSPACE / "activity" / pathlib.Path(a["filename"]).name
        target.write_bytes(base64.b64decode(a["content_b64"]))

    repo_url = env["DEVCAKE_REPO_URL"]
    askpass = WORKSPACE / ".devcake" / "askpass.sh"
    askpass.write_text("#!/bin/sh\necho \"$DEVCAKE_FORGE_TOKEN\"\n")
    askpass.chmod(0o700)
    clone_user, git_name, git_email, cli_envs = forge_dialect(env)
    clone_url = repo_url.replace("https://", f"https://{clone_user}@")
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo_dir = WORKSPACE / "repo"  # canonical path; dir inside named after the repo
    # git auth for clone AND the harness's own push (docs/03 §3); CLI auth for PRs
    os.environ["GIT_ASKPASS"] = str(askpass)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
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
    if harness == "grok-build":
        mode = ["--permission-mode", "plan"] if plan_mode else ["--always-approve"]
        pin = ["--model", model] if model else []
        cmd = ["grok", "-p", prompt, "--output-format", "streaming-json",
               *mode, *pin, *extra]
    elif harness == "codex":
        mode = ["--sandbox", "read-only"] if plan_mode \
            else ["--dangerously-bypass-approvals-and-sandbox"]
        pin = ["-m", model] if model else []
        cmd = ["codex", "exec", prompt, "--json",
               "-o", str(WORKSPACE / "out" / "last_message.txt"),
               "--skip-git-repo-check", *mode, *pin, *extra]
    else:
        mode = ["--permission-mode", "plan"] if plan_mode             else ["--dangerously-skip-permissions"]
        pin = ["--model", model] if model else []
        # --verbose is REQUIRED with -p + stream-json (CLI errors out without it)
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json",
               "--verbose", *mode, *pin, *extra]
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

    # ── token extraction + result text (docs/08 §5) ──────────────────────────
    token_report = {"extraction_method": "unavailable", "model": harness}
    result_text, transcript_body = "", ""
    if harness == "codex":
        try:
            last = WORKSPACE / "out" / "last_message.txt"
            result_text = last.read_text() if last.exists() else ""
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
        try:
            parsed = grok_stream_parse(out)
            if parsed is not None:
                result_text, sid = parsed
            else:  # EXTRA_ARGS overrode the format back to a plain json blob
                j = json.loads(out)
                result_text = j.get("text") or ""
                sid = j.get("sessionId") or ""
            sig = None
            for p in pathlib.Path.home().glob(f".grok/sessions/*/{sid}/signals.json"):
                sig = json.loads(p.read_text())
            if sig:
                token_report = {
                    "total_tokens": sig.get("contextTokensUsed") or sig.get("totalTokens"),
                    "model": (sig.get("modelsUsed") or ["grok"])[0],
                    "extraction_method": "session_json",
                    "num_turns": sig.get("turnCount"),
                }
            exp = subprocess.run(["grok", "export", sid], capture_output=True, text=True)
            if exp.returncode == 0 and exp.stdout.strip():
                transcript_body = exp.stdout
        except Exception:
            result_text = out[-4000:]
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
    # (grok: the `grok export` session already includes every message)
    if harness == "codex":
        dump = codex_text_dump(out)
    elif harness == "grok-build":
        dump = transcript_body
    else:
        dump = claude_text_dump(out)

    if harness_exit != 0:
        err = err_text[-1500:]
        auth_fail = "authentication" in err.lower() or "unauthorized" in err.lower() \
            or "log in" in err.lower()
        code = 12 if auth_fail else 10
        send_artifacts({"result": None, "exit_code": code,
                        "transcript_md": f"harness exited {harness_exit}\n\n```\n{err}\n```"
                        + (f"\n\n## Session transcript\n\n{dump}" if dump else ""),
                        "token_report": token_report})
        stop.set()
        sys.exit(code)

    # ── result.json (docs/03 §6) ─────────────────────────────────────────────
    # Plan mode is read-only — the harness cannot write files, so the entrypoint
    # materializes PLAN.md and result.json from the returned plan text (docs/08 §3)
    if plan_mode:
        if len((result_text or "").strip()) < 200:  # a real plan is never this short
            send_artifacts({"result": None, "exit_code": 11,
                            "transcript_md": f"plan mode returned no usable plan "
                                             f"({len(result_text or '')} chars):\n\n{result_text}"
                            + (f"\n\n## Session transcript\n\n{dump}" if dump else ""),
                            "token_report": token_report})
            stop.set()
            sys.exit(11)  # DEV_BAD_OUTPUT — fail the attempt, never advance an empty plan
        (WORKSPACE / "out" / "PLAN.md").write_text(result_text)
        (WORKSPACE / "out" / "result.json").write_text(json.dumps({
            "schema_version": 1, "outcome": "planned",
            "summary": result_text.strip().splitlines()[0][:300]}))
    result_path = WORKSPACE / "out" / "result.json"
    # per-type legality (docs/03 §6). First-line defense only — the app enforces
    # the same table authoritatively at finalization (missions.LEGAL_OUTCOMES).
    legal_outcomes = {
        "ONBOARD": {"plan_needed", "executed_trivially", "decomposed", "human_needed"},
        "PLAN": {"planned"},
        "EXECUTE": {"executed", "human_needed"},
        "REVIEW": {"reviewed", "human_needed"},
        "MAPPER": {"relations_mapped"},
    }
    legal = legal_outcomes.get(env.get("DEVCAKE_MISSION_TYPE", ""),
                               set().union(*legal_outcomes.values()))
    try:
        result = json.loads(result_path.read_text())
        assert result.get("outcome") in legal, \
            f"outcome {result.get('outcome')!r} illegal for {env.get('DEVCAKE_MISSION_TYPE')}"
        assert isinstance(result.get("summary"), str)
    except Exception as e:
        send_artifacts({"result": None, "exit_code": 11,
                        "transcript_md": f"result.json missing/invalid: {e}\n\n---\n\n{result_text}"
                        + (f"\n\n## Session transcript\n\n{dump}" if dump else ""),
                        "token_report": token_report})
        stop.set()
        sys.exit(11)

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
    if plan_path.exists():
        payload["plan_md"] = plan_path.read_text()
    send_artifacts(payload)
    stop.set()
    print(f"dev {RUN_ID} done: {result.get('outcome')}")


if __name__ == "__main__":
    main()
