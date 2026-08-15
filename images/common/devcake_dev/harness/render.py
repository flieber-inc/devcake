"""Live stdout relay + harness event renderers (docs/08 §1a, docs/09 §2)."""
from __future__ import annotations

import json
import threading
import time

from devcake_dev.adapters import bus as _bus
from devcake_dev.domain.fault import _dict, _one_line

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
                # Attribute lookup so tests can monkeypatch bus.send
                _bus.send("run.log", {"lines": batch})
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


def render_pi(raw: str):
    """Pi `--mode json` events → condensed lines (docs/08 §1a)."""
    try:
        ev = json.loads(raw)
    except Exception:  # noqa: BLE001 — non-JSON is printed as-is
        s = raw.strip()
        return s[:LINE_LIMIT] if s else None
    if not isinstance(ev, dict):
        return None
    kind = ev.get("type")
    if kind == "session":
        return f"[pi] session {str(ev.get('id') or '')[:8]}"
    if kind == "tool_execution_start":
        args = json.dumps(ev.get("args") or {})[:160]
        return f"→ {ev.get('toolName', '?')} {args}"
    if kind == "tool_execution_end" and ev.get("isError"):
        return f"[pi] tool error: {_one_line(str(ev.get('result') or ''), 160)}"
    if kind == "message_update":
        asst = ev.get("assistantMessageEvent") or {}
        if asst.get("type") == "text_delta" and asst.get("delta"):
            text = str(asst["delta"]).strip()
            return text[:200] or None
        return None
    if kind == "message_end":
        from .tokens import _pi_message_text
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if msg.get("role") != "assistant":
            return None
        text = _pi_message_text(msg).strip()
        return text[:200] or None
    if kind == "agent_end":
        return "[pi] done"
    if kind == "error":
        return f"[pi] error: {_one_line(str(ev.get('message') or ev.get('error') or ''), 200)}"
    return None


def render_stderr(raw: str):
    s = raw.strip()
    return f"! {s[:LINE_LIMIT]}" if s else None

