"""ADR-0031 phase 1 — the Freshness Gate on REVIEW's context-closing finalize.

A run's view of the PMO is frozen at dispatch (the mirror watermark on the
Run is its reading receipt); a positive-verdict REVIEW is the pipeline's last
feed read, so material entries landing in [dispatch, finalize] would close
unread. The gate withholds the done-transition and routes the mission through
a counted re-review directive instead (the conflict-resolve pattern, docs/03
§4.1). The check is fail-OPEN — a gate bug must never wedge a finalize or
stamp a counted failure — but the directive post, once material is FOUND, is
not: a failed post early-returns with no transition, and the next plain
REVIEW's mirror carries the material anyway.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..model import MissionRef
from . import steps
from ..run import Run, aware
from .dispatch import mission_cost
from .feed import is_devcake_comment, unquoted
from .markers import (ELEVATED_MARKERS, FRESHNESS_MARKER,
                      MAX_FRESHNESS_REREVIEWS)

log = logging.getLogger("devcake.missions")

# Steps whose presence means the finalize is already past the point where
# withholding the transition is coherent (merged or parked pre-upgrade) —
# the gate must not run for the first time on a redelivery that already
# merged the PR.
# DERIVED from the step registry (ADR-0034): a step declares
# past_freshness_gate at registration — the old hand-enumeration of
# review.py's step names cannot drift anymore.
_PAST_GATE_STEPS = steps.past_gate_steps()

# Synthetic finding when the adapter's hard stop truncated the fetch: the
# gitea_issues adapter pages ASCENDING and drops the NEWEST entries at its
# ceiling — exactly the ones the gate exists to catch — so truncation means
# material-UNKNOWN, and unknown trips rather than passes (ADR-0031 D2).
_TRUNCATED = "(feed truncated — material unknown)"


def _is_material(body: str | None) -> bool:
    """ADR-0031 Decision 3. Non-empty unquoted body AND (no sentinel ⇒ 🧑
    HUMAN provenance, docs/03 §8a) OR an explicitly elevated marker class.
    Everything else DevCake posts — step markers, merge notes, replies, the
    gate's own directive — is sentinel'd bookkeeping and immaterial BY
    CONSTRUCTION: that is the livelock guarantee. (Label/status events never
    arrive as entries at all — both adapters fetch comments only — so the
    non-empty-body test is defense-in-depth, not the operative exclusion.)"""
    text = unquoted(body).strip()
    if not text:
        return False
    if not is_devcake_comment(body):
        return True
    return any(rx.search(text) for rx in ELEVATED_MARKERS)


def _entries_after_watermark(entries, run: Run) -> list:
    """Entries the run's context did not include. Watermark id present and
    found ⇒ everything after it (entries arrive ascending from both
    adapters). Id present but MISSING (a human deleted the watermark
    comment) ⇒ everything looks unread — the conservative direction, self-
    limiting at the loop cap, and consistent with the marker-count doctrine
    that deliberate comment deletion resets marker-derived state. No
    watermark (legacy record / internal forge absent / empty feed at
    dispatch) ⇒ timestamp fallback against the run's dispatch time."""
    wm = run.feed_watermark or {}
    wid = wm.get("entry_id")
    if wid:
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].entry_id == wid:
                return list(entries[i + 1:])
        return list(entries)
    # ts fallback: prefer the mirror-fetch timestamp over created_at — the
    # Run is minted AFTER the mirror push, so a created_at anchor would let
    # entries landing in that gap pass unseen (the wrong direction)
    anchor = None
    if wm.get("ts"):
        try:
            anchor = aware(datetime.fromisoformat(wm["ts"]))
        except ValueError:
            anchor = None
    if anchor is None:
        anchor = aware(run.created_at)
    return [e for e in entries if aware(e.ts) > anchor]


def _describe(entries: list) -> list[str]:
    """Short human-readable labels for the exhaustion disclosure."""
    out = []
    for e in entries:
        if isinstance(e, str):
            out.append(e)
        else:
            out.append(f"{e.ts:%Y-%m-%d %H:%M} — {e.author}")
    return out


async def _unread_material(mgr, run: Run) -> tuple[list, int]:
    """(material entries no run read, current re-review count). One
    full-history fetch — shallow entries carry entry_id=None in BOTH
    adapters, which would silently degrade every check to the timestamp
    fallback. The marker count rides the same fetch (over ALL unquoted
    bodies, not just new ones — the budget is per mission lifetime)."""
    act = await mgr.pmo.get_activity(
        MissionRef(run.mission_pmo_id, "issue"), full=True)
    hits = [int(m.group(1)) for e in act.entries
            for m in FRESHNESS_MARKER.finditer(unquoted(e.body))]
    count = max(hits) if hits else 0
    if act.truncated:
        return [_TRUNCATED], count
    new = _entries_after_watermark(act.entries, run)
    return [e for e in new if _is_material(e.body)], count


def _directive_body(run: Run, n: int, found: list) -> str:
    what = (_TRUNCATED if found and found[0] == _TRUNCATED
            else f"{len(found)} new feed entr{'y' if len(found) == 1 else 'ies'}")
    return (
        f"🔄 **Freshness re-review {n}/{MAX_FRESHNESS_REREVIEWS}:** {what} "
        f"arrived after this review's context was assembled, so the approve "
        f"verdict is withheld (report: `{run.seq}_REVIEW.md` in the feed). "
        f"The next REVIEW evaluates ONLY whether the newer entries change "
        f"that verdict. No reply needed — a reply posted during the "
        f"re-review is itself new material and will trigger another one. "
        f"`devcake:freshness-rereview:{n}`")


async def review_freshness_gate(mgr, run: Run) -> str:
    """The gate, called at the TOP of finalize_review's approve branch —
    before any approval artifact, so a trip posts nothing a re-review would
    duplicate. Returns 'pass' | 'tripped' | 'exhausted'; 'tripped' means the
    caller must return without transitioning ('exhausted' proceeds — the
    disclosure was posted here)."""
    done_steps = run.finalized_steps
    if steps.REVIEW_FRESHNESS_TRIPPED in done_steps:
        # redelivery of a tripped finalize: stay tripped — re-running a
        # check that can fail open would close the mission directly under a
        # directive that just promised a re-review
        return "tripped"
    if steps.REVIEW_FRESHNESS_OK in done_steps \
            or steps.REVIEW_FRESHNESS_EXHAUSTED in done_steps \
            or any(s in done_steps for s in _PAST_GATE_STEPS):
        return "pass"

    try:
        found, count = await _unread_material(mgr, run)
    except Exception:  # noqa: BLE001 — fail-OPEN by doctrine (ADR-0031 D4): an escaping non-ValueError wedges finalize into redelivery/dead-letter; a ValueError stamps a counted DEV_BAD_OUTPUT feeding the ADR-0026 brakes
        log.exception("freshness gate check failed for %s — proceeding open",
                      run.mission_key)
        found, count = [], 0
    if not found:
        done_steps.append(steps.REVIEW_FRESHNESS_OK)
        mgr.runs.store.save(run)
        return "pass"

    pmo_id = run.mission_pmo_id
    if count >= MAX_FRESHNESS_REREVIEWS:
        names = "; ".join(_describe(found)[:5])
        more = f" (+{len(found) - 5} more)" if len(found) > 5 else ""

        async def _disclose():
            await mgr._feed(
                pmo_id, "issue",
                f"⚠️ Closing with unevaluated activity: the freshness "
                f"re-review budget ({MAX_FRESHNESS_REREVIEWS}) is spent, so "
                f"the standing approve verdict proceeds. Unevaluated: "
                f"{names}{more}. Cumulative recorded mission cost: "
                f"${mission_cost(mgr, pmo_id):.2f}.")
            mgr._audit(pmo_id, "freshness_exhausted",
                       f"{len(found)} unread entries at close")
            mgr.anomalies[pmo_id] = (
                f"{run.mission_key}: closed with unevaluated feed activity "
                f"(re-review budget spent)")  # transient — pruned once done
        await mgr._checkpoint(run, steps.REVIEW_FRESHNESS_EXHAUSTED, _disclose)
        return "exhausted"

    n = count + 1

    async def _directive():
        await mgr._feed(pmo_id, "issue", _directive_body(run, n, found))
        mgr._audit(pmo_id, "freshness_tripped",
                   f"re-review {n}/{MAX_FRESHNESS_REREVIEWS}: "
                   f"{len(found)} unread entries")
    try:
        await mgr._checkpoint(run, steps.REVIEW_FRESHNESS_DIRECTIVE, _directive)
    except Exception:  # noqa: BLE001 — a failed post must NOT fail open past found material (that would close on known-unread entries) and must not wedge finalize either: with no marker posted the count cannot inflate, and the next plain REVIEW's mirror carries the material anyway
        log.exception("freshness directive post failed for %s — the "
                      "re-review proceeds undirected", run.mission_key)
    done_steps.append(steps.REVIEW_FRESHNESS_TRIPPED)
    run.verdict = (f"handed off: freshness re-review "
                   f"{n}/{MAX_FRESHNESS_REREVIEWS} dispatched")
    mgr.runs.store.save(run)
    return "tripped"


async def disclose_unread_at_close(mgr, mission) -> None:
    """ADR-0031 D1 inventory, deferred-merge sweep row: DISCLOSE-ONLY. The
    sweep's merge was operator-sanctioned minutes-to-hours after the gate
    passed at finalize; material landing in that window still closes the
    mission, but never silently. Best-effort throughout — the sweep's close
    must not gain a new failure mode. Anchored on the newest finished REVIEW
    run's watermark; no REVIEW run on record ⇒ nothing to compare, skip."""
    try:
        reviews = [r for r in mgr.runs.store.all()
                   if r.mission_pmo_id == mission.pmo_id
                   and mgr._run_is_ours(r) and r.mission_type == "REVIEW"
                   and r.state == "finished"]
        if not reviews:
            return
        run = max(reviews, key=lambda r: aware(r.created_at))
        found, _ = await _unread_material(mgr, run)
        if not found:
            return
        names = "; ".join(_describe(found)[:5])
        more = f" (+{len(found) - 5} more)" if len(found) > 5 else ""
        await mgr._feed(
            mission.pmo_id, "issue",
            f"⚠️ Merged and closed with feed activity the final review did "
            f"not see: {names}{more}. The merge was already sanctioned "
            f"(deferred-retry window) — disclosure only.")
        mgr._audit(mission.pmo_id, "freshness_unread_at_close",
                   f"{len(found)} entries")
    except Exception:  # noqa: BLE001 — disclosure is best-effort by design
        log.debug("unread-at-close disclosure failed for %s", mission.key,
                  exc_info=True)
