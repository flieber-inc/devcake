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

from typing import Literal

from ..model import LABEL_MERGE, LABEL_REVIEW, Mission, MissionRef
from . import steps
from ..run import Run, aware
from .dispatch import mission_cost
from .feed import is_devcake_comment, unquoted
from .markers import ELEVATED_MARKERS, FRESHNESS_MARKER

RecheckResult = Literal["pass", "tripped", "exhausted", "no_review_anchor"]

log = logging.getLogger("devcake.missions")

# Steps whose presence means the finalize is already past the point where
# withholding the transition is coherent (merged or parked pre-upgrade) —
# the gate must not run for the first time on a redelivery that already
# merged the PR.
# DERIVED from the step registry (ADR-0034): a step declares
# past_freshness_gate at registration — the old hand-enumeration of
# review.py's step names cannot drift anymore.
_PAST_GATE_STEPS = steps.past_gate_steps()

# Synthetic finding when the adapter's hard stop truncated the fetch.
# Which end a vendor drops is adapter-specific (Linear newest-first keeps
# newest; Gitea/GitHub use paginate_rest_newest so the ceiling also keeps
# newest when last-page is known) — but truncated always means
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


def entries_after_watermark(entries, run: Run) -> list:
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
    new = entries_after_watermark(act.entries, run)
    return [e for e in new if _is_material(e.body)], count


def _count_label(n: int, cap: int) -> str:
    """"{n}/{cap}", or bare "{n}" under an unlimited (0) budget."""
    return f"{n}/{cap}" if cap else f"{n}"


def _directive_body(run: Run, n: int, found: list, cap: int) -> str:
    # Do NOT backtick `{seq}_REVIEW.md` — that shape is STEP_MARKER wire
    # format and would render a phantom [attachment: …] line in ACTIVITY.md.
    # Plain-language pointer keeps seq for the operator without the wire token.
    what = (_TRUNCATED if found and found[0] == _TRUNCATED
            else f"{len(found)} new feed entr{'y' if len(found) == 1 else 'ies'}")
    return (
        f"🔄 **Freshness re-review {_count_label(n, cap)}:** {what} "
        f"arrived after this review's context was assembled, so the approve "
        f"verdict is withheld (the standing REVIEW report for seq {run.seq} "
        f"is already in the feed). The next REVIEW evaluates ONLY whether "
        f"the newer entries change that verdict. No reply needed — a reply "
        f"posted during the re-review is itself new material and will "
        f"trigger another one. "
        f"`devcake:freshness-rereview:{n}`")


def _exhaustion_copy(
        *, closes: bool, key: str, cap: int, names: str, more: str,
        cost: float) -> tuple[str, str]:
    """Feed disclosure + anomaly for a spent freshness re-review budget.

    ``closes=True`` is the in-pipeline finalize path (standing approve
    proceeds). ``closes=False`` is Force / merge-settle recheck — labels stay
    on DEVCAKE-MERGE; nothing closes. Sharing one "Closing…" string across
    both paths is the honesty bug this helper exists to prevent.
    """
    if closes:
        body = (
            f"⚠️ Closing with unevaluated activity: the freshness "
            f"re-review budget ({cap}) is spent, so "
            f"the standing approve verdict proceeds. Unevaluated: "
            f"{names}{more}. Cumulative recorded mission cost: "
            f"${cost:.2f}.")
        anomaly = (f"{key}: closed with unevaluated feed activity "
                   f"(re-review budget spent)")
    else:
        # Shared by operator Force and merge-settle — do not say "Force"
        # (merge-settle also lands here) and do not say "Closing" (labels
        # stay on DEVCAKE-MERGE; nothing closes).
        body = (
            f"⚠️ Freshness re-review declined: the freshness re-review "
            f"budget ({cap}) is spent, so the mission remains parked on "
            f"`DEVCAKE-MERGE` awaiting a human. Unevaluated: "
            f"{names}{more}. Cumulative recorded mission cost: "
            f"${cost:.2f}.")
        anomaly = (f"{key}: freshness re-review budget spent; still parked "
                   f"on DEVCAKE-MERGE")
    return body, anomaly


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
    # operator knob (ADR-0033 D7 as amended): 0 = unlimited — never exhausts
    cap = mgr.config.budgets.freshness_rereviews
    if cap and count >= cap:
        names = "; ".join(_describe(found)[:5])
        more = f" (+{len(found) - 5} more)" if len(found) > 5 else ""
        body, anomaly = _exhaustion_copy(
            closes=True, key=run.mission_key, cap=cap, names=names,
            more=more, cost=mission_cost(mgr, pmo_id))

        async def _disclose():
            await mgr._feed(pmo_id, "issue", body)
            mgr._audit(pmo_id, "freshness_exhausted",
                       f"{len(found)} unread entries at close")
            mgr.anomalies[pmo_id] = anomaly  # transient — pruned once done
        await mgr._checkpoint(run, steps.REVIEW_FRESHNESS_EXHAUSTED, _disclose)
        return "exhausted"

    n = count + 1

    async def _directive():
        await mgr._feed(pmo_id, "issue", _directive_body(run, n, found, cap))
        mgr._audit(pmo_id, "freshness_tripped",
                   f"re-review {_count_label(n, cap)}: "
                   f"{len(found)} unread entries")
    try:
        await mgr._checkpoint(run, steps.REVIEW_FRESHNESS_DIRECTIVE, _directive)
    except Exception:  # noqa: BLE001 — a failed post must NOT fail open past found material (that would close on known-unread entries) and must not wedge finalize either: with no marker posted the count cannot inflate, and the next plain REVIEW's mirror carries the material anyway
        log.exception("freshness directive post failed for %s — the "
                      "re-review proceeds undirected", run.mission_key)
    done_steps.append(steps.REVIEW_FRESHNESS_TRIPPED)
    run.verdict = (f"handed off: freshness re-review "
                   f"{_count_label(n, cap)} dispatched")
    mgr.runs.store.save(run)
    return "tripped"


def _newest_finished_review(mgr, pmo_id: str) -> Run | None:
    """Newest finished REVIEW run for a mission — the post-approve watermark
    anchor (force re-review / merge-settle / disclose-at-close)."""
    reviews = [r for r in mgr.runs.store.all()
               if r.mission_pmo_id == pmo_id
               and mgr._run_is_ours(r) and r.mission_type == "REVIEW"
               and r.state == "finished"]
    if not reviews:
        return None
    return max(reviews, key=lambda r: aware(r.created_at))


async def recheck_and_maybe_rereview(
        mgr, mission: Mission, *, reason: str) -> RecheckResult:
    """Post-approve freshness re-check for a MERGE-hold mission.

    Used by the operator Force action and the auto-merge settle window end.
    Anchors on the newest finished REVIEW run's watermark (not an in-flight
    finalize Run). Returns:
      pass            — no material after watermark (labels untouched)
      tripped         — 🔄 directive posted, DEVCAKE-MERGE → DEVCAKE-REVIEW
      exhausted       — budget spent; ⚠ disclosed; labels untouched
      no_review_anchor — no finished REVIEW on record (nothing to compare)

    Shares material rules, markers, and budgets.freshness_rereviews with
    review_freshness_gate — does not checkpoint a Run (there may not be one).
    """
    run = _newest_finished_review(mgr, mission.pmo_id)
    if run is None:
        return "no_review_anchor"
    try:
        found, count = await _unread_material(mgr, run)
    except Exception:  # noqa: BLE001 — fail-OPEN: a recheck bug must not wedge the operator action or the merge sweep
        log.exception("freshness recheck failed for %s — treating as pass",
                      mission.key)
        return "pass"
    if not found:
        return "pass"

    pmo_id = mission.pmo_id
    cap = mgr.config.budgets.freshness_rereviews
    if cap and count >= cap:
        names = "; ".join(_describe(found)[:5])
        more = f" (+{len(found) - 5} more)" if len(found) > 5 else ""
        body, anomaly = _exhaustion_copy(
            closes=False, key=mission.key, cap=cap, names=names,
            more=more, cost=mission_cost(mgr, pmo_id))
        await mgr._feed(pmo_id, "issue", body)
        mgr._audit(pmo_id, "freshness_exhausted",
                   f"{reason}: {len(found)} unread entries")
        mgr.anomalies[pmo_id] = anomaly
        return "exhausted"

    n = count + 1
    await mgr._feed(pmo_id, "issue", _directive_body(run, n, found, cap))
    ref = MissionRef(pmo_id, mission.pmo_kind)
    await mgr.pmo.swap_labels(ref, remove={LABEL_MERGE}, add={LABEL_REVIEW})
    # mission.labels is mutated by FakePMO; live adapters re-fetch on next poll
    if LABEL_MERGE in mission.labels:
        mission.labels = (mission.labels - {LABEL_MERGE}) | {LABEL_REVIEW}
    handoffs = getattr(mgr, "merge_handoffs", None)
    if isinstance(handoffs, dict):
        handoffs.pop(pmo_id, None)
    audit_action = ("freshness_forced" if reason == "operator_force"
                    else "freshness_tripped")
    mgr._audit(pmo_id, audit_action,
               f"{reason}: re-review {_count_label(n, cap)}: "
               f"{len(found)} unread entries")
    return "tripped"


async def disclose_unread_at_close(mgr, mission) -> None:
    """ADR-0031 D1 inventory, deferred-merge sweep row: DISCLOSE-ONLY. The
    sweep's merge was operator-sanctioned minutes-to-hours after the gate
    passed at finalize; material landing in that window still closes the
    mission, but never silently. Best-effort throughout — the sweep's close
    must not gain a new failure mode. Anchored on the newest finished REVIEW
    run's watermark; no REVIEW run on record ⇒ nothing to compare, skip."""
    try:
        run = _newest_finished_review(mgr, mission.pmo_id)
        if run is None:
            return
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
