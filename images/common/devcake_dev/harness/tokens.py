"""Stream dumps and token-report extraction (docs/08 §5).

ADR-0029: every extractor emits **TokenReport v1** — one CLOSED shape,
every key always present (None = unknown, never absent), provenance as the
`source` field instead of key-presence folklore, and the vendor usage
payload preserved untouched under `raw`. Normalization happens HERE, at the
token-extraction seam (the seam a future HarnessDialect.parse_run
formalizes), so app consumers read fixed keys and never branch per harness.
"""
from __future__ import annotations

import json
import pathlib

from devcake_dev.domain.fault import _dict

TOKEN_REPORT_SCHEMA = 1

# The closed key set — test_token_report_shape pins every extractor to it.
TOKEN_REPORT_KEYS = (
    "schema", "model", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "total_tokens", "reasoning_tokens", "num_turns",
    "duration_ms", "cost_usd_native", "cost_usd_estimated", "source", "raw")

# `source` provenance values: the three extraction paths, "cumulative" (a
# resume chain whose harness reports cumulative counters — codex; the last
# report IS the chain total), "mixed" (a multi-chain merge whose inputs
# disagree), "unavailable" (INV-5: reported explicitly, never silence).
TOKEN_REPORT_SOURCES = ("session_json", "end_event", "signals", "cumulative",
                        "mixed", "unavailable")


def token_report_v1(*, model=None, source="unavailable", raw=None,
                    input_tokens=None, output_tokens=None,
                    cache_read_tokens=None, cache_write_tokens=None,
                    total_tokens=None, reasoning_tokens=None, num_turns=None,
                    duration_ms=None, cost_usd_native=None) -> dict:
    """The one constructor for TokenReport v1. `cost_usd_estimated` is
    ALWAYS None here — it is the app-side rate-card stamp (ADR-0021), and
    the harness layer never estimates. `total_tokens` is reported-only:
    deriving it from splits is a display concern the app labels honestly
    ("effective"), not a measurement this layer may invent."""
    return {
        "schema": TOKEN_REPORT_SCHEMA,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "cost_usd_native": cost_usd_native,
        "cost_usd_estimated": None,
        "source": source,
        "raw": raw if isinstance(raw, dict) else {},
    }


def unavailable_report(model=None) -> dict:
    """INV-5: "no report" is itself reported, in the full closed shape."""
    return token_report_v1(model=model, source="unavailable")


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
    return token_report_v1(
        model=models[0] if models else "grok",
        source="end_event",
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        total_tokens=usage.get("total_tokens"),
        # a SUBSET of output_tokens (never priced on top) — first-class in
        # v1; pre-v1 it hid in a regex-parsed `notes` string
        reasoning_tokens=usage.get("reasoning_tokens"),
        num_turns=ev.get("num_turns"),
        cost_usd_native=None,           # never 0 — see above
        raw={"usage": usage, "modelUsage": mu,
             "num_turns": ev.get("num_turns")})


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
    return token_report_v1(
        model=models[0] if isinstance(models, list) and models else "grok",
        # names its path truthfully — pre-v1 this masqueraded as
        # "session_json", indistinguishable from the claude/codex path
        source="signals",
        total_tokens=sig.get("contextTokensUsed") or sig.get("totalTokens"),
        num_turns=sig.get("turnCount"),
        raw=sig)


def codex_token_report(out: str):
    """TokenReport from codex's JSONL `turn.completed` events (verified
    0.144.1), or None when the stream carries none. Last event wins — on a
    multi-turn run each carries the running usage. Moved here from the
    entrypoint's inline arm (ADR-0029): extraction lives at this seam."""
    report = None
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:  # noqa: BLE001 — one unparseable line must never cost the whole report
            continue
        if isinstance(ev, dict) and ev.get("type") == "turn.completed":
            u = _dict(ev.get("usage"))
            report = token_report_v1(
                model="codex",
                source="session_json",
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cache_read_tokens=u.get("cached_input_tokens"),
                # NEW at codex 0.146.0 (capture-verified): 0.144.4 had no
                # write counter at all — absent key reads None, so pre-bump
                # streams keep rendering "—", never a fabricated 0
                cache_write_tokens=u.get("cache_write_input_tokens"),
                reasoning_tokens=u.get("reasoning_output_tokens"),
                raw=u)
    return report


def claude_token_report(j) -> dict:
    """TokenReport from claude-code's final result event (stream-json) or
    the `--output-format json` blob — same fields either way (verified
    live). Moved here from the entrypoint's inline arm (ADR-0029)."""
    j = _dict(j)
    usage = _dict(j.get("usage"))
    mu = _dict(j.get("modelUsage"))

    def _weight(v):  # dominant model = the one that cost/produced the most
        return ((v.get("costUSD") or 0, v.get("outputTokens") or 0)
                if isinstance(v, dict) else (0, 0))

    models = sorted(mu, key=lambda k: _weight(mu[k]), reverse=True)
    return token_report_v1(
        model=models[0] if models else "claude-code",
        source="session_json",
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_write_tokens=usage.get("cache_creation_input_tokens"),
        # NEW at claude-code 2.1.229 (capture-verified): thinking rides an
        # output_tokens_details breakdown — a SUBSET of output_tokens, so it
        # lands in the v1 reasoning slot (grok/codex parity), never priced
        # on top; a pre-2.1.229 stream without the key reads None, never 0
        reasoning_tokens=_dict(usage.get("output_tokens_details")).get(
            "thinking_tokens"),
        cost_usd_native=j.get("total_cost_usd"),
        num_turns=j.get("num_turns"),
        duration_ms=j.get("duration_ms"),
        raw={"usage": usage, "modelUsage": mu,
             "total_cost_usd": j.get("total_cost_usd"),
             "num_turns": j.get("num_turns"),
             "duration_ms": j.get("duration_ms")})


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

