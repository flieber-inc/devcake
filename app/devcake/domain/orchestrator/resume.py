"""A resume is a human's answer — make it visible to the Dev.

A Dev that hands off (`human_needed`) is released when a person removes the
`DEVCAKE-NEEDS-HUMAN` label. Nothing else ever removes it, so the release IS
a human action — but a label removal is not a feed entry, and the Dev's
activity mirror only shows comments. Without this, a Dev whose brief asked
for an approval keeps re-verifying, finds no comment, and hands off again.

At dispatch (and on the mid-run activity refresh) the run history tells
whether the mission's most recent finished run was a hand-off; if so, the
activity mirror opens with a banner: what was asked, that a person released
the hold, and how many human comments followed. The playbooks carry the
matching convention (prompts.HUMAN_HANDOFF): a human comment since the
hand-off is the answer; without one, the release itself is the answer.
"""

from __future__ import annotations

from typing import Any

from ...security import redact
from ..run import TERMINAL_STATES, aware
from .feed import is_devcake_comment

ASK_MAX = 600


def resumed_after_handoff(mgr: Any, pmo_id: str) -> dict | None:
    """{"mission_type", "at", "ask"} when this mission's most recent
    FINISHED run ended in a hand-off (so a person released it), else None.
    A run still in flight is ignored: the mid-run refresh must report the
    same hand-off the dispatch did."""
    runs = getattr(getattr(mgr, "runs", None), "store", None)
    if runs is None:
        return None
    ours = getattr(mgr, "_run_is_ours", lambda r: True)
    rows = [r for r in runs.all()
            if getattr(r, "mission_pmo_id", None) == pmo_id and ours(r)
            and r.mission_type != "STEWARD"
            and r.state in TERMINAL_STATES and r.result]
    if not rows:
        return None
    last = max(rows, key=lambda r: aware(r.created_at))
    if (last.result or {}).get("outcome") != "human_needed":
        return None
    at = last.ended_at or last.created_at
    ask = redact(str((last.result or {}).get("summary") or "")).strip()
    return {"mission_type": last.mission_type, "at": aware(at),
            "ask": ask[:ASK_MAX] + ("…" if len(ask) > ASK_MAX else "")}


def banner_lines(info: dict, entries: list) -> list[str]:
    """The ACTIVITY.md banner (precedes the mirror, like the gap banners)."""
    since = [e for e in entries
             if not is_devcake_comment(e.body or "") and aware(e.ts) > info["at"]]
    when = info["at"].strftime("%Y-%m-%d %H:%M UTC")
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
            f"Human comments since the hand-off: {len(since)} — the newest "
            f"🧑 HUMAN entries below answer the ask; follow them.")
    else:
        lines.append(
            "No human comment followed the hand-off, so the release itself "
            "is the answer: an approval or decision you asked for is granted; "
            "something you asked to be provided should now be there — verify "
            "it and continue. Hand off again only if the obstacle "
            "demonstrably persists.")
    lines.append("")
    return lines
