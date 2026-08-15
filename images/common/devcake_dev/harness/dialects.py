"""The three in-tree dialects. Parsers stay in tokens/fault/render — this
module only binds identity to those functions (docs/16 H1)."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from . import dialect as _reg
from .dialect import ResumeSpec, WORKSPACE
from .render import GrokCoalescer, render_claude, render_codex, render_pi
from .tokens import (
    claude_text_dump, claude_token_report,
    codex_text_dump, codex_token_report,
    grok_end_event, grok_end_report, grok_signals_report, grok_stream_parse,
    pi_agent_end, pi_session_event, pi_text_dump, pi_token_report,
    unavailable_report,
)
from ..domain.fault import (
    HARNESS_STATUS_PATTERNS, _dict, _one_line, claude_result_event,
    claude_run_fault, codex_run_fault, grok_run_fault, pi_run_fault,
    harness_error_messages,
)


class _Claude:
    id = "claude-code"
    resume_spec = ResumeSpec(usage_cumulative=False)
    dump_cumulative_on_resume = False

    def argv(self, prompt, *, plan_mode=False, model="", extra=(), out_dir=None):
        extra = list(extra)
        mode = (["--permission-mode", "plan"] if plan_mode
                else ["--dangerously-skip-permissions"])
        pin = ["--model", model] if model else []
        return ["claude", "-p", prompt, "--output-format", "stream-json",
                "--verbose", *mode, *pin, *extra]

    def resume_argv(self, session_id, prompt, *, model="", extra=(), out_dir=None):
        extra = list(extra)
        pin = ["--model", model] if model else []
        return ["claude", "-p", prompt, "--resume", session_id,
                "--output-format", "stream-json", "--verbose",
                "--dangerously-skip-permissions", *pin, *extra]

    def renderer(self):
        return render_claude

    def parse_run(self, out, *, workspace, model=""):
        report = unavailable_report(model=self.id)
        try:
            j = claude_result_event(out) or json.loads(out)
            report = claude_token_report(j)
            result = j.get("result") or ""
        except Exception:  # noqa: BLE001 — a parse miss still posts the tail
            result = out[-4000:]
        try:
            dump = claude_text_dump(out)
        except Exception:  # noqa: BLE001 — no dump ⇒ the no-dump fallback
            dump = ""
        return _reg.InvocationView(result_text=result, token_report=report,
                                   dump=dump)

    def fault(self, out, harness_exit, *, dump="", last_message="", prompt=""):
        return claude_run_fault(out, harness_exit, dump=dump)

    def api_error_status(self, out):
        status = _dict(claude_result_event(out)).get("api_error_status")
        return status if isinstance(status, int) else None

    def session_identity(self, out):
        try:
            return str(_dict(claude_result_event(out)).get("session_id") or "")
        except Exception:  # noqa: BLE001 — no handle ⇒ fresh-mode degradation
            return ""

    def terminal_evidence(self, out):
        try:
            ev = claude_result_event(out)
            if ev is None:
                return None
            return {"event": "result",
                    "subtype": str(ev.get("subtype") or ""),
                    "terminal_reason": str(ev.get("terminal_reason") or ""),
                    "num_turns": ev.get("num_turns"),
                    "is_error": bool(ev.get("is_error"))}
        except Exception:  # noqa: BLE001 — evidence is advisory
            return None


class _Grok:
    id = "grok-build"
    resume_spec = ResumeSpec(usage_cumulative=False)
    dump_cumulative_on_resume = True

    def argv(self, prompt, *, plan_mode=False, model="", extra=(), out_dir=None):
        extra = list(extra)
        mode = ["--permission-mode", "plan"] if plan_mode else ["--always-approve"]
        pin = ["--model", model] if model else []
        return ["grok", "-p", prompt, "--output-format", "streaming-json",
                *mode, *pin, *extra]

    def resume_argv(self, session_id, prompt, *, model="", extra=(), out_dir=None):
        extra = list(extra)
        pin = ["--model", model] if model else []
        return ["grok", "-p", prompt, "-r", session_id,
                "--output-format", "streaming-json", "--always-approve",
                *pin, *extra]

    def renderer(self):
        return GrokCoalescer()

    def parse_run(self, out, *, workspace, model=""):
        report = unavailable_report(model=self.id)
        sid, terminal = "", None
        try:
            parsed = grok_stream_parse(out)
            if parsed is not None:
                result, sid = parsed
                terminal = grok_end_event(out)
            else:
                j = json.loads(out)
                result = j.get("text") or ""
                sid = j.get("sessionId") or ""
                terminal = j
        except Exception:  # noqa: BLE001 — a parse miss still posts the tail
            result = out[-4000:]
        try:
            report = (grok_end_report(terminal)
                      or grok_signals_report(sid) or report)
        except Exception:  # noqa: BLE001 — the artifact path outranks its own token report
            print("token extraction failed; reporting unavailable", file=sys.stderr)
        dump = ""
        try:
            exp = (subprocess.run(["grok", "export", sid], capture_output=True,
                                  text=True) if sid else None)
            if exp is not None and exp.returncode == 0 and exp.stdout.strip():
                dump = exp.stdout
        except Exception:  # noqa: BLE001 — no export ⇒ no dump; the fault predicate handles an empty one
            print("grok export failed; transcript falls back to the agent report",
                  file=sys.stderr)
        return _reg.InvocationView(result_text=result, token_report=report,
                                   dump=dump)

    def fault(self, out, harness_exit, *, dump="", last_message="", prompt=""):
        return grok_run_fault(out, harness_exit, dump=dump, prompt=prompt)

    def api_error_status(self, out):
        for message in harness_error_messages(out):
            for rx in HARNESS_STATUS_PATTERNS[self.id]:
                hit = rx.search(message)
                if hit:
                    return int(hit.group(1))
        return None

    def session_identity(self, out):
        sid = ""
        try:
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — one bad line never costs the handle
                    continue
                if isinstance(ev, dict) and ev.get("sessionId"):
                    sid = str(ev["sessionId"])
        except Exception:  # noqa: BLE001 — no handle ⇒ fresh-mode degradation
            return ""
        return sid

    def terminal_evidence(self, out):
        try:
            ev = grok_end_event(out)
            if ev is not None:
                return {"event": "end",
                        "stop_reason": str(ev.get("stopReason") or ""),
                        "num_turns": ev.get("num_turns"),
                        "session_id": str(ev.get("sessionId") or "")}
            found = None
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — one unparseable line never costs the evidence
                    continue
                if isinstance(ev, dict) and ev.get("type") == "error":
                    found = ev
            if found is not None:
                return {"event": "error",
                        "message": _one_line(str(found.get("message") or ""), 120)}
            return None
        except Exception:  # noqa: BLE001 — evidence is advisory
            return None


class _Codex:
    id = "codex"
    resume_spec = ResumeSpec(usage_cumulative=True)
    dump_cumulative_on_resume = False

    def argv(self, prompt, *, plan_mode=False, model="", extra=(), out_dir=None):
        extra = list(extra)
        out = pathlib.Path(out_dir) if out_dir is not None else WORKSPACE / "out"
        mode = (["--sandbox", "read-only"] if plan_mode
                else ["--dangerously-bypass-approvals-and-sandbox"])
        pin = ["-m", model] if model else []
        return ["codex", "exec", prompt, "--json",
                "-o", str(out / "last_message.txt"),
                "--skip-git-repo-check", *mode, *pin, *extra]

    def resume_argv(self, session_id, prompt, *, model="", extra=(), out_dir=None):
        extra = list(extra)
        out = pathlib.Path(out_dir) if out_dir is not None else WORKSPACE / "out"
        pin = ["-m", model] if model else []
        return ["codex", "exec", "resume", session_id, prompt, "--json",
                "-o", str(out / "last_message.txt"), "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", *pin, *extra]

    def renderer(self):
        return render_codex

    def parse_run(self, out, *, workspace, model=""):
        report = unavailable_report(model=self.id)
        last = workspace / "out" / "last_message.txt"
        try:
            last_text = last.read_text() if last.exists() else ""
            result = last_text
            report = codex_token_report(out) or report
            if not result:
                result = out[-4000:]
        except Exception:  # noqa: BLE001 — a parse miss still posts the tail
            last_text = ""
            result = out[-4000:]
        try:
            dump = codex_text_dump(out)
        except Exception:  # noqa: BLE001 — no dump ⇒ the no-dump fallback
            dump = ""
        return _reg.InvocationView(result_text=result, token_report=report,
                                   dump=dump, last_message=last_text)

    def fault(self, out, harness_exit, *, dump="", last_message="", prompt=""):
        return codex_run_fault(out, harness_exit, last_message=last_message)

    def api_error_status(self, out):
        for message in harness_error_messages(out):
            for rx in HARNESS_STATUS_PATTERNS[self.id]:
                hit = rx.search(message)
                if hit:
                    return int(hit.group(1))
        return None

    def session_identity(self, out):
        try:
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — one bad line never costs the handle
                    continue
                if (isinstance(ev, dict) and ev.get("type") == "thread.started"
                        and ev.get("thread_id")):
                    return str(ev["thread_id"])
        except Exception:  # noqa: BLE001 — no handle ⇒ fresh-mode degradation
            return ""
        return ""

    def terminal_evidence(self, out):
        try:
            completed = None
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — one unparseable line never costs the evidence
                    continue
                if isinstance(ev, dict) and ev.get("type") == "turn.completed":
                    completed = ev
            if completed is None:
                return None
            return {"event": "turn.completed",
                    "output_tokens": _dict(completed.get("usage")).get("output_tokens")}
        except Exception:  # noqa: BLE001 — evidence is advisory
            return None


class _Pi:
    id = "pi"
    resume_spec = None
    dump_cumulative_on_resume = False

    def argv(self, prompt, *, plan_mode=False, model="", extra=(), out_dir=None):
        extra = list(extra)
        # --no-approve: non-interactive modes skip the project-trust prompt
        # and ignore untrusted project .pi (json.md / settings.md).
        mode = ["--tools", "read,grep,find,ls"] if plan_mode else []
        pin = ["--model", model] if model else []
        return ["pi", "--mode", "json", "--no-approve", *mode, *pin,
                prompt, *extra]

    def resume_argv(self, session_id, prompt, *, model="", extra=(), out_dir=None):
        return None

    def renderer(self):
        return render_pi

    def parse_run(self, out, *, workspace, model=""):
        report = unavailable_report(model=self.id)
        try:
            report = pi_token_report(out) or report
        except Exception:  # noqa: BLE001 — the artifact path outranks its own token report
            pass
        try:
            dump = pi_text_dump(out)
        except Exception:  # noqa: BLE001 — no dump ⇒ the no-dump fallback
            dump = ""
        result = dump
        if not result:
            result = out[-4000:]
        return _reg.InvocationView(result_text=result, token_report=report,
                                   dump=dump)

    def fault(self, out, harness_exit, *, dump="", last_message="", prompt=""):
        return pi_run_fault(out, harness_exit, dump=dump)

    def api_error_status(self, out):
        from .tokens import _pi_events
        blobs = []
        for ev in _pi_events(out):
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if msg.get("errorMessage"):
                blobs.append(str(msg["errorMessage"]))
            for amsg in ev.get("messages") or []:
                if _dict(amsg).get("errorMessage"):
                    blobs.append(str(amsg["errorMessage"]))
        blobs.extend(harness_error_messages(out))
        for message in blobs:
            head = message[:3]
            if head.isdigit():
                return int(head)
            for rx in HARNESS_STATUS_PATTERNS[self.id]:
                hit = rx.search(message)
                if hit:
                    return int(hit.group(1))
        return None

    def session_identity(self, out):
        try:
            ev = pi_session_event(out)
            return str((ev or {}).get("id") or "")
        except Exception:  # noqa: BLE001 — no handle ⇒ fresh-mode degradation
            return ""

    def terminal_evidence(self, out):
        try:
            ev = pi_agent_end(out)
            if ev is None:
                return None
            return {"event": "agent_end",
                    "messages": len(ev.get("messages") or [])}
        except Exception:  # noqa: BLE001 — evidence is advisory
            return None


def load_all() -> None:
    _reg.register(_Claude())
    _reg.register(_Grok())
    _reg.register(_Codex())
    _reg.register(_Pi())
