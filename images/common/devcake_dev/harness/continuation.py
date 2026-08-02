"""Continuation domain (ADR-0022) — pure stream → evidence helpers.

Dev-side domain like fault.py: no Redis, no subprocess, no filesystem. A
separate module because its helpers need BOTH tokens.py (grok_end_event) and
fault.py (claude_result_event) — landing them in either would cycle the
import between those two (tokens already imports from fault).
"""
from __future__ import annotations

import json

from devcake_dev.domain.fault import _dict, _one_line, claude_result_event
from devcake_dev.harness.tokens import grok_end_event


def _grok_error_event(out: str):
    """Last {"type":"error"} event, or None. An error event outranks nothing:
    on the exit-11 paths it cannot occur (an error event is a fault, exit 15),
    but this helper must stay honest for any stream it is handed."""
    found = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — one unparseable line never costs the evidence
            continue
        if isinstance(ev, dict) and ev.get("type") == "error":
            found = ev
    return found


def terminal_evidence(harness: str, out: str):
    """Small terminal-event summary for the exit-11 forensics dict, or None
    when the stream carries no terminal event at all — None IS evidence then
    (it serializes as null under the `terminal` key: the stream just stopped).

    The exit status and stderr cannot distinguish "the model ended its turn
    cleanly but early" (grok stopReason EndTurn, the narrate-and-stop shape)
    from a truncated stream; this names which one happened. Guarded like
    every stream parser — model-adjacent bytes must never abort the artifact
    path. An unknown harness falls through to the claude arm, mirroring
    main()'s renderer dispatch and harness_fault."""
    try:
        if harness == "grok-build":
            ev = grok_end_event(out)
            if ev is not None:
                return {"event": "end",
                        "stop_reason": str(ev.get("stopReason") or ""),
                        "num_turns": ev.get("num_turns"),
                        "session_id": str(ev.get("sessionId") or "")}
            err = _grok_error_event(out)
            if err is not None:
                return {"event": "error",
                        "message": _one_line(str(err.get("message") or ""), 120)}
            return None
        if harness == "codex":
            completed = None
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — as above
                    continue
                if isinstance(ev, dict) and ev.get("type") == "turn.completed":
                    completed = ev
            if completed is None:
                return None
            return {"event": "turn.completed",
                    "output_tokens": _dict(completed.get("usage")).get("output_tokens")}
        ev = claude_result_event(out)
        if ev is None:
            return None
        return {"event": "result",
                "subtype": str(ev.get("subtype") or ""),
                "terminal_reason": str(ev.get("terminal_reason") or ""),
                "num_turns": ev.get("num_turns"),
                "is_error": bool(ev.get("is_error"))}
    except Exception:  # noqa: BLE001 — evidence is advisory; the fail() path outranks it
        return None
