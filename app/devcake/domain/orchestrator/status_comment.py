"""The mission's status comment (ADR-0042 §5): one per mission, a VIEW the
reader opens first — the step ladder, what is happening now, the PR, the
cost so far — edited in place at every step end, park, hand-off, merge
event and completion. Created by WHICHEVER dispatch finds the feed without
one (DevCake has no privileged entry point: a scheduled task starts at
EXECUTE, a person can order a mission into any step by label, an older
mission resumes mid-pipeline); found again by its marker, oldest wins, so
a restart or a second instance converges on the same entry.

Everything in it is derived from the record — the mission's runs, its
labels and status, the recorded PR url — never the other way round: it
carries no counted marker, it is immaterial to the Freshness Gate by
construction (sentinel-signed, not elevated) and the Dev's folder omits
it (its every fact is already there). It is never an ask: the ✋ notice
is, and the Now line points at it. Best-effort by contract — a refused or
failed write is audited and never gates a dispatch, a finalize or a sweep.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...ports.pmo import PMOTransient
from ..model import (LABEL_EXECUTE, LABEL_FAILED, LABEL_MERGE,
                     LABEL_NEEDS_HUMAN, LABEL_PLAN, LABEL_REVIEW, LABEL_SKIP,
                     Mission, MissionRef)
from ..run import Run, aware, utcnow
from .feed import (SECTION_RECORD, FoldSection, collapsible_of, format_duration,
                   render_fold)
from .markers import STATUS_MARKER, decomposition_parent_ref

log = logging.getLogger("devcake.missions")

_IN_FLIGHT = ("dispatched", "running", "finalizing")
_STAGE_STEP = {LABEL_PLAN: "PLAN", LABEL_EXECUTE: "EXECUTE",
               LABEL_REVIEW: "REVIEW"}
_OUTCOME_GLYPH = {
    ("ONBOARD", "plan_needed"): ("📋", "triaged"),
    ("ONBOARD", "decomposed"): ("🧩", "decomposed"),
    ("PLAN", "planned"): ("📋", "planned"),
    ("EXECUTE", "executed"): ("🔀", "PR opened"),
}


def runs_of(mgr, pmo_id: str) -> list[Run]:
    """This instance's runs of the mission, oldest first — THE ladder's
    source (a mission that entered at REVIEW has one row)."""
    runs = [r for r in mgr.runs.store.all()
            if r.mission_pmo_id == pmo_id and mgr._run_is_ours(r)
            and r.mission_type not in ("STEWARD", "HELLO", "OAUTH")]
    runs.sort(key=lambda r: aware(r.created_at))
    return runs


def _row_state(r: Run) -> tuple[str, str]:
    """(glyph, word) for a ladder row."""
    if r.state in _IN_FLIGHT:
        return "⏳", "running"
    if r.state != "finished":
        return "⚠️", f"{r.state}" + (f" ({r.error_class})" if r.error_class else "")
    outcome = str((r.result or {}).get("outcome") or "")
    if outcome == "human_needed":
        return "✋", "handed off"
    if r.mission_type == "REVIEW" and outcome == "reviewed":
        verdict = str((r.result or {}).get("verdict") or "").lower()
        return ("✅", "approved") if verdict == "approve" else \
            ("🔁", "rejected") if verdict == "reject" else ("⚠️", verdict or "reviewed")
    return _OUTCOME_GLYPH.get((r.mission_type, outcome),
                              ("⚠️", outcome or "no result"))


def _duration_s(r: Run) -> float | None:
    if r.started_at is None:
        return None
    end = r.ended_at if r.ended_at is not None else utcnow()
    return (aware(end) - aware(r.started_at)).total_seconds()


def _pr_url(runs: list[Run], given: str | None) -> str:
    if given:
        return given
    for r in reversed(runs):
        if r.pr_url:
            return r.pr_url
    for r in reversed(runs):
        url = (r.result or {}).get("pr_url") if r.mission_type == "EXECUTE" else None
        if url:
            return str(url)
    return ""


def _now_line(mission: Mission, runs: list[Run], pr_url: str,
              waiting: str | None = None) -> str:
    labels = mission.labels
    if mission.status == "done":
        return "✅ Done."
    if mission.status == "canceled":
        return "🚫 Canceled."
    if LABEL_SKIP in labels:
        return "⏸ Stopped by DEVCAKE-SKIP."
    if LABEL_FAILED in labels:
        return "⚠️ Gave up — needs a person (see the ✋ notice above)."
    if LABEL_NEEDS_HUMAN in labels:
        return "✋ Parked — needs a person (see the ✋ notice above)."
    if LABEL_MERGE in labels:
        return f"⏳ Awaiting merge of {pr_url}." if pr_url else "⏳ Awaiting merge."
    live = next((r for r in reversed(runs) if r.state in _IN_FLIGHT), None)
    if live is not None:
        since = (f" since {aware(live.started_at):%Y-%m-%d %H:%M} UTC"
                 if live.started_at else "")
        return (f"⏳ Running step {live.seq} · {live.mission_type} "
                f"(attempt {live.attempt_of_step}){since}.")
    if waiting:                       # stalls.py: cannot start, and why
        return waiting
    stage = next((_STAGE_STEP[l] for l in _STAGE_STEP if l in labels), None)
    return f"⏳ Queued for {stage}." if stage else "⏳ Queued."


def render(mgr, mission: Mission, runs: list[Run], *, pr_url: str | None,
           collapsible: str, waiting: str | None = None) -> str:
    """The comment body (sentinel-free; the chokepoints seal it). Pure —
    everything comes from the record. Never a file token, never a
    `Part i of n` line, no marker but the status marker (each pinned)."""
    from .dispatch import mission_cost, run_cost
    url = _pr_url(runs, pr_url)
    latest: dict[int, Run] = {}
    for r in runs:
        latest[r.seq] = r                        # oldest first ⇒ last wins
    total, est = mission_cost(mgr, mission.pmo_id, split_estimated=True)
    minutes = sum((_duration_s(r) or 0) for r in latest.values()) / 60
    lines = [f"📌 **{mission.key} — status** · a view DevCake keeps current; "
             "the record is the entries above.", "",
             f"**Now:** {_now_line(mission, runs, url, waiting)}"]
    if url:
        lines.append(f"**PR:** {url}")
    cost = f"**Cost so far:** ${total:.2f}"
    if est:
        cost += f" (of which ${est:.2f} estimated)"
    cost += f" across {len(latest)} step{'s' if len(latest) != 1 else ''}"
    if minutes >= 1:
        cost += f" · {format_duration(minutes * 60)} of Dev time"
    lines.append(cost)
    parent = decomposition_parent_ref(mission)
    if parent:
        lines.append(f"**Parent:** {_parent_label(mgr, parent)}")
    if latest:
        lines += ["", "| Step | Outcome | Duration | Cost |", "|---|---|---|---|"]
        for seq in sorted(latest):
            r = latest[seq]
            glyph, word = _row_state(r)
            attempt = (f" (attempt {r.attempt_of_step})"
                       if r.attempt_of_step > 1 else "")
            dur = format_duration(_duration_s(r)) or "—"
            c = run_cost(mgr, r)
            lines.append(f"| {seq} · {r.mission_type} | {glyph} {word}{attempt} "
                         f"| {dur} | {'$%.2f' % c if c is not None else '—'} |")
    record = FoldSection(SECTION_RECORD, (
        f"{STATUS_MARKER}\nupdated {utcnow():%Y-%m-%d %H:%M} UTC · instance "
        f"{getattr(mgr, 'instance_name', '')}"))
    lines += ["", render_fold([record], collapsible=collapsible, summary="Record")]
    return "\n".join(lines)


def _parent_label(mgr, parent_ref: str) -> str:
    snap = getattr(mgr, "snapshot", None)
    for m in (getattr(snap, "missions", None) or []):
        if m.pmo_id == parent_ref or m.key == parent_ref:
            return f"{m.key} — {m.url}" if m.url else m.key
    return parent_ref


def _cache(mgr) -> dict[str, str]:
    cache = getattr(mgr, "_status_entries", None)
    if cache is None:
        cache = {}
        try:
            mgr._status_entries = cache
        except Exception:  # noqa: BLE001 — a frozen test double: the cache is optional
            pass
    return cache


async def ensure(mgr, mission: Mission, run: Run, *, found: str) -> str:
    """Dispatch-time (the only creation site): `found` is the marker hit
    from the mirror's full read; when empty, the comment is created —
    never refreshed here (finalize writes the next state). Returns the
    entry id, "" when there is none (a project mission, a vendor that
    returned no id, a refused write — audited, never raised)."""
    if mission.pmo_kind != "issue":
        return ""
    cache = _cache(mgr)
    if found:
        cache[mission.pmo_id] = found
        return found
    try:
        runs = runs_of(mgr, mission.pmo_id)
        if all(r.run_id != run.run_id for r in runs):
            runs.append(run)
        body = render(mgr, mission, runs, pr_url=None,
                      collapsible=collapsible_of(mgr))
        cid = await mgr._feed(mission.pmo_id, "issue", body, externalize=False)
    except Exception as e:  # noqa: BLE001 — a view never gates a dispatch: audited, the next dispatch tries again
        mgr._audit(mission.pmo_id, "status_comment_failed", f"create: {str(e)[:160]}")
        return ""
    if cid:
        cache[mission.pmo_id] = cid
        mgr._audit(mission.pmo_id, "status_comment_created", cid)
    return cid or ""


def _known_entry(mgr, pmo_id: str, run: Run | None) -> str:
    if run is not None and run.status_entry_id:
        return run.status_entry_id
    cache = _cache(mgr)
    if cache.get(pmo_id):
        return cache[pmo_id]
    for r in reversed(runs_of(mgr, pmo_id)):
        if r.status_entry_id:
            return r.status_entry_id
    return ""


async def refresh(mgr, pmo_id: str, *, reason: str, run: Run | None = None,
                  mission: Mission | None = None,
                  pr_url: str | None = None) -> None:
    """Rebuild the body from the record and edit it in place. At most ONE
    cheap `pmo.get` when the caller has no fresh mission. Catches
    everything: a refused budget, a transient, a vanished entry (the
    cached id is dropped so the next dispatch re-creates it)."""
    entry_id = _known_entry(mgr, pmo_id, run)
    if not entry_id:
        seen = getattr(mgr, "_status_unknown", None)
        if seen is None:
            seen = set()
            try:
                mgr._status_unknown = seen
            except Exception:  # noqa: BLE001 — optional bookkeeping on a double
                pass
        if pmo_id not in seen:
            seen.add(pmo_id)
            mgr._audit(pmo_id, "status_comment_unknown", reason)
        return
    try:
        if mission is None:
            mission = await mgr.pmo.get(MissionRef(pmo_id, "issue"))
        runs = runs_of(mgr, pmo_id)
        if run is not None and all(r.run_id != run.run_id for r in runs):
            runs.append(run)
        body = render(mgr, mission, runs, pr_url=pr_url,
                      collapsible=collapsible_of(mgr))
        await mgr._edit(pmo_id, "issue", entry_id, body)
        _cache(mgr)[pmo_id] = entry_id
    except PMOTransient as e:
        mgr._audit(pmo_id, "status_comment_failed", f"{reason}: {str(e)[:160]}")
    except Exception as e:  # noqa: BLE001 — permanent (the entry is gone, the vendor refused): forget it; the next dispatch re-creates
        mgr._audit(pmo_id, "status_comment_failed", f"{reason}: {str(e)[:160]}")
        _cache(mgr).pop(pmo_id, None)
        if run is not None and run.status_entry_id == entry_id:
            run.status_entry_id = ""
            try:
                mgr.runs.store.save(run)
            except Exception:  # noqa: BLE001 — best-effort bookkeeping
                log.debug("status entry reset not saved for %s", run.run_id,
                          exc_info=True)
