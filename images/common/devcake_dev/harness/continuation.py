"""Continuation domain (ADR-0022) — pure decisions for the in-container
continuation loop, plus the stream → evidence helpers it shares with the
exit-11 forensics.

Dev-side domain like fault.py: no Redis, no subprocess, no filesystem — the
entrypoint façade composes, this module decides. A separate module because
its helpers need BOTH tokens.py (grok_end_event) and fault.py
(claude_result_event) — landing them in either would cycle the import
between those two (tokens already imports from fault).

Turn-budget interplay, accepted and deliberate: every relaunch resets the
CLI's own --max-turns counter, so a run's effective turn budget is
(continuations + 1) × max-turns. The relay's `continuation N/M` boundary
lines keep the multiplication operator-visible.
"""
from __future__ import annotations

import dataclasses
import json

from devcake_dev.domain.fault import _dict, _one_line, claude_result_event
from devcake_dev.harness.tokens import grok_end_event

POLICY_AUTO = "auto"
POLICY_RESUME_ONLY = "resume-only"
POLICY_FRESH_ONLY = "fresh-only"
POLICY_OFF = "off"
POLICIES = frozenset({POLICY_AUTO, POLICY_RESUME_ONLY, POLICY_FRESH_ONLY,
                      POLICY_OFF})


@dataclasses.dataclass(frozen=True)
class ContinuationConfig:
    max_continuations: int      # 0 = feature off; the ONLY terminator
    policy: str                 # one of POLICIES


def continuation_config(env) -> ContinuationConfig:
    """Parse DEVCAKE_MAX_CONTINUATIONS / DEVCAKE_CONTINUATION_POLICY.

    NEVER raises: absent/""/garbage/negative budget → 0 (off); an unknown
    policy string → "auto" (the budget still gates). An entrypoint that
    exits 20 over a config typo is strictly worse than one that simply does
    not continue."""
    try:
        budget = int(str(env.get("DEVCAKE_MAX_CONTINUATIONS") or "0").strip())
    except Exception:  # noqa: BLE001 — garbage means off, not exit 20
        budget = 0
    policy = str(env.get("DEVCAKE_CONTINUATION_POLICY") or "").strip()
    return ContinuationConfig(max(0, budget),
                              policy if policy in POLICIES else POLICY_AUTO)


@dataclasses.dataclass
class ContinuationState:
    """Loop bookkeeping, owned by main(), advanced ONLY by next_continuation."""
    used: int = 0                        # continuations launched so far
    escalated_to_fresh: bool = False     # auto's PERMANENT escalation latch
    last_fingerprint: str | None = None  # snapshot at the previous row-9 landing


@dataclasses.dataclass(frozen=True)
class ContinuationDecision:
    action: str      # "stop" | "resume" | "fresh"
    reason: str      # one line; the relay boundary line and stop evidence


def next_continuation(config: ContinuationConfig, state: ContinuationState, *,
                      plan_mode: bool, session_id: str,
                      resume_supported: bool,
                      fingerprint: str | None) -> ContinuationDecision:
    """Decide the next action at a row-9 landing and ADVANCE `state`.

    The integer budget is the ONLY terminator (founder decision 2026-08-02):
    zero progress escalates auto's mode, never stops. `fingerprint is None`
    means progress UNKNOWN — it never latches the escalation (a broken git
    must not cost the session context). Escalation is permanent: a later
    fingerprint change never de-escalates."""
    if plan_mode:
        return ContinuationDecision("stop", "plan mode never continues")
    if config.policy == POLICY_OFF or config.max_continuations <= 0:
        return ContinuationDecision("stop", "continuations disabled")
    if state.used >= config.max_continuations:
        return ContinuationDecision(
            "stop", f"continuation budget exhausted ({config.max_continuations})")
    stalled = (state.used > 0 and fingerprint is not None
               and state.last_fingerprint is not None
               and fingerprint == state.last_fingerprint)
    if stalled and config.policy == POLICY_AUTO and not state.escalated_to_fresh:
        state.escalated_to_fresh = True
    resume_available = resume_supported and bool(session_id)
    if config.policy == POLICY_FRESH_ONLY:
        action, why = "fresh", "policy fresh-only"
    elif config.policy == POLICY_RESUME_ONLY:
        if not resume_available:
            # The operator explicitly forbade fresh sessions; degrading would
            # violate that intent, so this fails exactly as today's exit 11.
            return ContinuationDecision(
                "stop", "resume unavailable under resume-only "
                        f"(session_id={'set' if session_id else 'missing'}, "
                        f"resume_supported={resume_supported})")
        action, why = "resume", "policy resume-only"
    elif state.escalated_to_fresh:
        action, why = "fresh", ("no progress since last continuation — "
                                "escalated to a fresh session"
                                if stalled else "escalated to fresh earlier")
    elif resume_available:
        action, why = "resume", "resuming the session"
    else:
        action, why = "fresh", ("no session handle to resume"
                                if resume_supported else
                                "no verified resume for this harness")
    state.used += 1
    state.last_fingerprint = fingerprint
    return ContinuationDecision(action, why)


# ── nudge prompts ────────────────────────────────────────────────────────────
# The nudge NEVER restates the result.json JSON shape: the per-type contracts
# carry required fields beyond outcome/summary (REVIEW's verdict/report_md,
# EXECUTE's pr_url, ONBOARD's decomposition payload), and a reduced example
# here would pass the entrypoint's first-line check yet fail app-side
# finalization. Resume mode points back into the session's own instructions;
# fresh mode embeds the ORIGINAL prompt verbatim — which IS the contract
# (and the frozen ADR-0016 assembly: identifying prompt + playbook + skills).

def outcome_contract(mission_type: str, legal) -> str:
    outcomes = " | ".join(f'"{o}"' for o in sorted(legal))
    return (
        f"Write /workspace/out/result.json with an \"outcome\" from: "
        f"{outcomes} — EXACTLY the JSON shape defined in the "
        f"'Required output' section of the original mission instructions, "
        f"including every field it requires. Do not invent a reduced shape. "
        f"If a pull request already exists for this branch, do NOT open "
        f"another — reference the existing one. If the {mission_type} work "
        f"is complete, write the file NOW. If it is not, continue working — "
        f"do NOT end your turn until /workspace/out/result.json exists.")


def _stray_sentence(stray_note: str) -> str:
    return (f" Note: {_one_line(stray_note, 300)}" if (stray_note or "").strip()
            else "")


def resume_nudge_prompt(mission_type: str, legal, *, attempt: int, budget: int,
                        stray_note: str = "") -> str:
    return (
        f"[devcake continuation {attempt}/{budget}] Your previous turn ended "
        f"without the required output file.{_stray_sentence(stray_note)}\n\n"
        + outcome_contract(mission_type, legal))


def fresh_nudge_prompt(original_prompt: str, mission_type: str, legal, *,
                       attempt: int, budget: int, stray_note: str = "") -> str:
    return (
        f"[devcake continuation {attempt}/{budget}] You are resuming an "
        f"INTERRUPTED run: a previous session was working on the mission "
        f"below IN THIS WORKING TREE and ended without writing the required "
        f"output file.{_stray_sentence(stray_note)}\n\n"
        f"Before anything else, inspect the current state — run `git status` "
        f"and `git diff` in the repository clone (the work may already be "
        f"partly or fully done — do NOT redo finished work) and read your "
        f"predecessor's notes under /workspace/activity/.\n\n"
        + outcome_contract(mission_type, legal)
        + "\n\n--- ORIGINAL MISSION INSTRUCTIONS (unchanged, still binding) "
          "---\n\n" + original_prompt)


# ── session chains (fork tolerance) ──────────────────────────────────────────

def record_session(chains: list, sid: str, mode: str) -> None:
    """Track session ids as CHAINS. `initial`/`fresh` with a new sid opens a
    chain; `resume` appends the sid to the LAST chain when it differs (a
    fork-style resume mints a new id that CONTAINS the history — no fork was
    observed in the Phase-0 captures, but the shape survives one). Empty sid
    or a duplicate of the chain tail: no-op."""
    if not sid:
        return
    if mode == "resume" and chains:
        if chains[-1][-1] != sid:
            chains[-1].append(sid)
        return
    if not chains or chains[-1][-1] != sid:
        chains.append([sid])


def export_sids(chains: list) -> list:
    """One sid per chain — the LAST: a resumed/forked session's export
    supersedes its ancestors' (the Phase-0 grok pair measured the resumed
    export containing both exchanges). Widen here if a fork is ever measured
    to LOSE history."""
    return [chain[-1] for chain in chains if chain]


def last_sid(chains: list) -> str:
    return chains[-1][-1] if chains and chains[-1] else ""


# ── aggregation across invocations ───────────────────────────────────────────

_SUMMED_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "total_tokens", "cost_usd",
                  "num_turns", "duration_ms")


def _sum_reports(reports: list) -> dict:
    out = {}
    for field in _SUMMED_FIELDS:
        vals = [r.get(field) for r in reports
                if isinstance(r.get(field), (int, float))
                and not isinstance(r.get(field), bool)]
        out[field] = sum(vals) if vals else None
    models = [r.get("model") for r in reports if r.get("model")]
    out["model"] = models[-1] if models else None
    methods = {r.get("extraction_method") for r in reports
               if r.get("extraction_method")}
    out["extraction_method"] = (methods.pop() if len(methods) == 1
                                else "mixed" if methods else "unavailable")
    return out


def merge_token_reports(reports, modes, *, resume_cumulative: bool) -> dict:
    """Aggregate per-invocation token reports (index-aligned with `modes`,
    each "initial" | "resume" | "fresh").

    Within a resume chain: last-wins when the harness reports CUMULATIVE
    usage on a resumed terminal event (codex — summing would double-count),
    else field-wise sum (grok, claude — measured per-invocation, Phase-0
    captures). Across chains: always sum. None-safe per field: the sum of
    the values present, or None when none are. A single report is returned
    unchanged — the zero-continuation payload stays byte-identical."""
    reports = [r if isinstance(r, dict) else {} for r in (reports or [])]
    if not reports:
        return {"extraction_method": "unavailable", "model": None}
    if len(reports) == 1:
        return dict(reports[0])
    groups = []
    for report, mode in zip(reports, modes):
        if mode == "resume" and groups:
            groups[-1].append(report)
        else:
            groups.append([report])
    per_chain = [(dict(g[-1]) if resume_cumulative else _sum_reports(g))
                 if len(g) > 1 else dict(g[0]) for g in groups]
    return _sum_reports(per_chain) if len(per_chain) > 1 else per_chain[0]


def merged_transcript_dump(segments, labels) -> str:
    """Attempt-labeled join of per-chain dump segments. A single non-empty
    segment passes through UNLABELED — the zero-continuation transcript
    stays byte-identical to today's."""
    present = [(seg, lab) for seg, lab in zip(segments, labels)
               if (seg or "").strip()]
    if not present:
        return ""
    if len(present) == 1:
        return present[0][0]
    return "\n\n---\n\n".join(
        f"## Continuation segment — {lab}\n\n{seg}" for seg, lab in present)


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


def session_identity(harness: str, out: str) -> str:
    """The harness's session handle for a resume relaunch, or "".

    grok: the LAST `sessionId` on any stream event (the `end` event carries
    it; `error` events never do — docs/08 §1). claude: `session_id` on the
    result event. codex: the `thread_id` of `thread.started`. An unknown
    harness falls through to the claude arm, mirroring harness_fault."""
    try:
        if harness == "grok-build":
            sid = ""
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — one bad line never costs the handle
                    continue
                if isinstance(ev, dict) and ev.get("sessionId"):
                    sid = str(ev["sessionId"])
            return sid
        if harness == "codex":
            for line in out.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001 — as above
                    continue
                if (isinstance(ev, dict) and ev.get("type") == "thread.started"
                        and ev.get("thread_id")):
                    return str(ev["thread_id"])
            return ""
        return str(_dict(claude_result_event(out)).get("session_id") or "")
    except Exception:  # noqa: BLE001 — no handle ⇒ fresh-mode degradation, never a crash
        return ""


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
