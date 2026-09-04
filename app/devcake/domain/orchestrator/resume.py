"""A resume is a human's answer — make it visible to the Dev.

A Dev that hands off (`human_needed`) is released when a person removes the
`DEVCAKE-NEEDS-HUMAN` label. Nothing else ever removes it, so the release IS
a human action — but a label removal is not a feed entry, and the Dev's
activity mirror only shows comments. Without this, a Dev whose brief asked
for an approval keeps re-verifying, finds no comment, and hands off again.

At dispatch (and on the mid-run activity refresh) the run history tells
whether the mission's most recent finished run was a genuine hand-off; if
so, MISSION.md opens with a one-line pointer and ACTIVITY.md with a banner:
what was asked, that a person released the hold, and how many human
comments that run never saw. The playbooks carry the matching convention
(prompts.HUMAN_HANDOFF): a human comment the run never saw is the answer;
without one, the release itself is the answer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...security import redact
from ..run import Run, aware
from .feed import is_devcake_comment

ASK_MAX = 600
HANDOFF_VERDICT = "handed off"      # transitions stamps this on a real hold
MISSION_NOTE = ("▶ RESUMED BY A HUMAN — a person released this mission after "
                "your previous run handed off. Read the banner at the top of "
                "ACTIVITY.md before anything else.")


def resumed_after_handoff(mgr: Any, pmo_id: str) -> dict | None:
    """{"mission_type", "at", "ended", "ask"} when this mission's most recent
    FINISHED run was a genuine hand-off (so a person released it), else None.

    Only `finished` runs count, like the hand-off counter in transitions: a
    failed retry (result kept) after a release must not hide the hand-off
    it is retrying. A run still in flight is ignored: the mid-run refresh
    must report the same hand-off the dispatch did. The verdict, not the
    outcome alone, decides — a run halted by an external stage change or
    parked for an illegal outcome also ends `human_needed` but never placed
    a hold, so no person released it."""
    runs = getattr(getattr(mgr, "runs", None), "store", None)
    if runs is None:
        return None
    ours = getattr(mgr, "_run_is_ours", lambda r: True)
    rows = [r for r in runs.all()
            if getattr(r, "mission_pmo_id", None) == pmo_id and ours(r)
            and r.mission_type != "STEWARD"
            and r.state == "finished" and r.result]
    if not rows:
        return None
    last = max(rows, key=lambda r: aware(r.created_at))
    if (last.result or {}).get("outcome") != "human_needed":
        return None
    if not (last.verdict or "").startswith(HANDOFF_VERDICT):
        return None
    ask = redact(str((last.result or {}).get("summary") or "")).strip()
    return {"mission_type": last.mission_type,
            "run": last,
            "at": seen_until(last),
            "ended": aware(last.ended_at or last.created_at),
            "ask": ask[:ASK_MAX] + ("…" if len(ask) > ASK_MAX else "")}


def seen_until(run: Run) -> datetime:
    """The last moment of the feed that run actually read: its feed
    watermark (ADR-0031), else its dispatch time — never its end, or an
    answer posted while it was still running would count as unseen by
    nobody."""
    wm = run.feed_watermark or {}
    if wm.get("ts"):
        try:
            return aware(datetime.fromisoformat(wm["ts"]))
        except ValueError:
            pass
    return aware(run.created_at)


def unseen_human_comments(info: dict, entries: list) -> list:
    """Human comments the hand-off run never saw: the freshness gate's own
    reading of its watermark (by entry position, so a second comment in the
    watermark's second still counts), else the timestamp boundary."""
    # local import: freshness → dispatch → activity_payload → resume
    from .freshness import entries_after_watermark
    run = info.get("run")
    after = (entries_after_watermark(entries, run) if run is not None
             else [e for e in entries if aware(e.ts) > info["at"]])
    return [e for e in after
            if getattr(e, "kind", "comment") == "comment"
            and not is_devcake_comment(e.body or "")]


def banner_lines(info: dict, entries: list) -> list[str]:
    """The ACTIVITY.md banner (precedes the mirror, like the gap banners)."""
    since = unseen_human_comments(info, entries)
    when = info["ended"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"▶ RESUMED BY A HUMAN — your previous {info['mission_type']} run "
        f"({when}) handed off asking:",
        f"> {info['ask'] or '(no summary was recorded)'}",
        "",
        "A person removed the hold on this mission; that is the only way a "
        "hand-off is released.",
    ]
    if since:
        lines.append(
            f"Human comments that run never saw: {len(since)} — the newest "
            f"🧑 HUMAN entries below answer the ask; follow them.")
    else:
        lines.append(
            "No human comment followed the hand-off, so the release itself "
            "is the answer: an approval you asked for is granted; a choice "
            "you asked for goes to the option you recommended; something "
            "you asked to be provided should now be there — verify it and "
            "continue. Re-read MISSION.md: the brief may have been revised "
            "instead of commented. Hand off again only if the obstacle "
            "demonstrably persists.")
    lines.append("")
    return lines
