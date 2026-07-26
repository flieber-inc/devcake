"""Stream dumps and token-report extraction (docs/08 §5)."""
from __future__ import annotations

import json
import pathlib

from devcake_dev.domain.fault import _dict

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

