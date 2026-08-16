"""Harness fault classification (ADR-0018) — pure stream → verdict.

Dev-side domain: no Redis, no subprocess, no filesystem. Inputs are harness
stdout/stderr bytes and exit codes; outputs are fault dicts / exit classes.
Composition lives in the entrypoint façade.
"""
from __future__ import annotations

import json
import re as _re

# ── the declared exit contract (ADR-0027) ─────────────────────────────────
# Every (exit code, error class) pair a Dev container can hand the app in a
# failure artifact. This is the IMAGE side's single source: the app-side
# taxonomy table (`domain/failure_taxonomy.py`) is asserted EQUAL to it by
# test_failure_taxonomy's parity test — real objects across the version-skew
# boundary, no text scraping — and an AST honesty check keeps this manifest
# true against dev_entrypoint.py's actual exit sites. Future HarnessDialect
# modules keep it. Two artifact-less bare exits ride codes already listed:
# dev_entrypoint's oauth-login exit 12 and unknown-phase exit 20 send no
# artifact, so they carry no class of their own (BARE_EXIT_CODES).
PRODUCED = frozenset({
    (10, "DEV_CRASH"),
    (11, "DEV_BAD_OUTPUT"),
    (12, "DEV_AUTH"),
    (13, "DEV_FORGE"),
    (13, "DEV_FORGE_AUTH"),
    (14, "DEV_MCP_SETUP"),
    (15, "DEV_HARNESS_FAULT"),
    (16, "DEV_TURN_BUDGET"),
    (20, "DEV_CRASH"),
})
BARE_EXIT_CODES = frozenset({12, 20})

# ── stderr auth classification (docs/15 §4) ───────────────────────────────
# Word-boundary regexes, NOT substrings: plain `"signed out" in text` matches
# "de|signed out|put"; a false 12 pauses the whole Dev Type — never over-match.
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
#
# CAPTURE RITUAL (2026-08-12 audit OPS-M4 — these markers are version-coupled
# to CLI stderr wording; exit 12 vs the exit-10 burned-attempt cascade turns
# on one phrase). Whenever a harness CLI pin is bumped (images/Dockerfile
# CLAUDE_CODE_VERSION / CODEX_VERSION, or the unpinned grok floats), RE-RUN
# the in-image capture so a wording change is caught as a fixture-verdict
# mismatch, not discovered as a revoked-credential cascade in production:
#   docker run --rm -v "$PWD:/srv" -w /srv devcake/dev-<harness>:<tag> \
#       python scripts/harness_capture/in_container.py --auth-revoked
# then commit the new capture under app/tests/fixtures/harness_streams/ — the
# parity test (test_harness_captures) fails if the CURRENT predicate disagrees
# with the recorded verdict. See scripts/harness_capture/in_container.py.
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



# ── stream helpers used by predicates ─────────────────────────────────────
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



# ── harness fault detection (ADR-0018) ────────────────────────────────────
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
            item_type = item.get("item_type") or item.get("type")  # 0.144.4+: type
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


def pi_run_fault(out: str, harness_exit: int, *, dump: str = ""):
    """Pi `--mode json`: fault dict or None.

    Terminal is `agent_end` (json.md). Tool activity is `tool_execution_*`
    or an assistant `toolCall` block — a tool result `isError` (e.g. ENOENT
    on a stub path) is NOT a harness fault. API/transport failures arrive as
    assistant `stopReason: error` plus `errorMessage` ("401: …") and still
    emit `agent_end` (Pi exits 0). Empty: ended cleanly with no assistant
    text and no tools.
    """
    from ..harness.tokens import _pi_events, _pi_message_text, pi_agent_end

    texts = tools = 0
    orphan_error = None
    last_end_error = None
    saw_end = False
    for ev in _pi_events(out):
        kind = str(ev.get("type") or "")
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if kind == "message_end" and msg.get("role") == "assistant":
            if _pi_message_text(msg).strip():
                texts += 1
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    tools += 1
        elif kind.startswith("tool_execution_"):
            tools += 1
        elif kind == "error":
            orphan_error = str(ev.get("message") or ev.get("error") or "")[:120] \
                or orphan_error
        elif kind == "agent_end":
            saw_end = True
            last_end_error = None
            for amsg in ev.get("messages") or []:
                if _dict(amsg).get("stopReason") == "error":
                    last_end_error = str(amsg.get("errorMessage")
                                         or "stopReason=error")[:120]
    ended = pi_agent_end(out)
    tail = (f"(harness_exit={harness_exit} texts={texts} tools={tools} "
            f"stdout={len(out)}B transcript={len(dump or '')}B)")
    if ended is None:
        err = last_end_error or orphan_error
        if err:
            return _fault(FAULT_TERMINAL_ERROR,
                          f"pi reported an error and never ended: {err}",
                          tail)
        return _fault(FAULT_NO_TERMINAL_EVENT,
                      "pi stream ended without an agent_end event", tail)
    if last_end_error:
        return _fault(FAULT_TERMINAL_ERROR,
                      f"pi reported a terminal error: {last_end_error}", tail)
    if texts == 0 and tools == 0 and not (dump or "").strip():
        return _fault(FAULT_EMPTY_COMPLETION,
                      "pi produced nothing at all — no assistant text and no "
                      "tool calls", tail)
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
    """Did the harness actually work? Fault dict or None. Unknown ids raise
    (docs/16 H1) — never the Claude predicate."""
    from ..harness.dialect import get_dialect
    return get_dialect(harness).fault(
        out, harness_exit, dump=dump, last_message=last_message, prompt=prompt)


# HTTP status from CLI transport wording (not model prose). Precision over
# recall: false 12 pauses a Dev Type; missed 401 falls through to 15.
# Word-boundary 3-digit HTTP status. message[:3].isdigit() is not a
# status: "40100ms deadline" is not 401; "120s timeout" is not 120.
_HTTP_STATUS_TOKEN = _re.compile(r"\b([1-5]\d{2})\b")


def http_status_from_message(message: str):
    """First plausible HTTP status (100–599) at a word boundary, or None."""
    hit = _HTTP_STATUS_TOKEN.search(message or "")
    return int(hit.group(1)) if hit else None


HARNESS_STATUS_PATTERNS = {
    "codex": (_re.compile(r"unexpected status (\d{3})"),
              _re.compile(r"last status: (\d{3})")),
    "grok-build": (_re.compile(r"Unauthorized \((\d{3})\)"),
                   _re.compile(r"\(status (\d{3})")),
    "pi": (_re.compile(r"\bstatus(?: code)?[:\s]+(\d{3})\b"),
           _re.compile(r"\bHTTP[ /]?(\d{3})\b"),
           _re.compile(r"\b(\d{3})\s+Unauthorized\b")),
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
    """HTTP status the harness reported (int), or None. Feeds auth precedence.
    Unknown ids raise (docs/16 H1)."""
    from ..harness.dialect import get_dialect
    return get_dialect(harness).api_error_status(out)


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
