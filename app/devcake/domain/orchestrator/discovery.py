"""ADR-0033 harvest half — the counterflow lane's memorialization seam.

A Dev's `discoveries` (structured finding/evidence/scope entries, docs/03 §6)
are surplus learning: the memory this otherwise memoryless system keeps
between runs (founder ruling 2026-08-13). Harvest is UNCONDITIONAL
memorialization (Decision 11): render DISCOVERY_<seq>.md and attach it as
the Mission Step's deliverable, post the marked source-feed comment, add the
DEVCAKE-DISCOVERY sweep-gate label, seed the advisory pending set. Routing
(the STEWARD discovery flavor) consumes the pending state; `scan_source`
is the ONE pending-scan pipe both halves share, and `render_entry_lines`
the ONE feed-comment entry renderer (chokepoint rulings). Error doctrine
is HANDOFF's, not completion's F4: every sub-step is best-effort — harvest
must never wedge a close.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...security import redact
from ..model import LABEL_DISCOVERY, MissionRef
from ..run import TERMINAL_STATES, Run, utcnow
from . import board
from . import steps
from .feed import blockquote, post_attachment_comment, unquoted
from .markers import (DISCOVERY_FIELD_MAX, DISCOVERY_PREVIEW_MAX, defang,
                      discovery_marker, discovery_posts, discovery_receipts)

log = logging.getLogger("devcake.missions")

# result.json authorship (ADR-0033 D1): PLAN's result is entrypoint-
# synthesized and steward runs are excluded (chain-reaction damper D7).
HARVEST_TYPES = ("ONBOARD", "EXECUTE", "REVIEW")
_FIELDS = ("finding", "evidence", "scope")


def harvest_run_index(mgr, runs=None) -> dict[tuple[str, int], Run]:
    """ONE run-index filter for harvest / package / apply / sweep peers
    (chokepoint). Harvest-typed runs that are ours AND carry a result —
    missing result is treated as gone so clear-runs / incomplete records
    cannot be routed as if they still had findings."""
    rows = mgr.runs.store.all() if runs is None else runs
    return {(r.mission_pmo_id, r.seq): r for r in rows
            if r.mission_type in HARVEST_TYPES
            and mgr._run_is_ours(r) and r.result}


def valid_entries(result: dict) -> list[dict]:
    """Defensive normalization of the optional `discoveries` result key:
    list-of-dicts with non-empty string finding/evidence/scope (evidence is
    the receipt — an entry without one is an opinion and is dropped), each
    field trimmed to DISCOVERY_FIELD_MAX. Anything malformed degrades
    silently (D1: missing/empty never fails a run). Uncapped — the caller
    applies budgets.discoveries_per_run and audits the drop."""
    raw = result.get("discoveries")
    if not isinstance(raw, list):
        return []
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        vals = {}
        for f in _FIELDS:
            v = e.get(f)
            if not isinstance(v, str) or not v.strip():
                break
            vals[f] = v.strip()[:DISCOVERY_FIELD_MAX]
        else:
            about = e.get("about")
            if isinstance(about, list):
                vals["about"] = [a.strip() for a in about
                                 if isinstance(a, str) and a.strip()]
            out.append(vals)
    return out


def render_discovery_md(run: Run, entries: list[dict]) -> str:
    """The attachment body — the full-fidelity knowledge-base record
    (founder ruling 2b: ALWAYS a step deliverable). Caller redacts."""
    lines = [f"# Discoveries — {run.mission_key} step {run.seq} "
             f"({run.mission_type})",
             f"run: `{run.run_id}` · {utcnow():%Y-%m-%d}", ""]
    for i, e in enumerate(entries, 1):
        lines += [f"## {i}. Finding", defang(e["finding"]), "",
                  f"**Evidence:** {defang(e['evidence'])}", "",
                  f"**Scope:** {defang(e['scope'])}", ""]
    return "\n".join(lines)


def render_entry_lines(entries: list[dict], *, full: bool = False,
                       cap: int = DISCOVERY_PREVIEW_MAX) -> list[str]:
    """The ONE entry renderer for feed comments (chokepoint ruling — the
    routing delivery reuses it with cap=DISCOVERY_IN_EXCERPT_MAX): per
    entry a numbered header plus the defanged, blockquoted text — capped
    excerpts by default, the whole record with full=True (the upload-failed
    fallback). Marker/provenance lines stay unquoted at the caller; entry
    text is quarantined here (ADR-0014 D2, and quoted lines never count in
    any scan)."""
    out = []
    for i, e in enumerate(entries, 1):
        if full:
            text = (f"Finding: {e['finding']}\n\nEvidence: {e['evidence']}"
                    f"\n\nScope: {e['scope']}")
        else:
            text = f"{e['finding']}\nEvidence: {e['evidence']}"
            if len(text) > cap:
                text = text[:cap] + "…"
        out.append(f"**{i}.**")
        out.append(blockquote(defang(text)))
    return out


def comment_body(run: Run, entries: list[dict], name: str,
                 url: str | None) -> tuple[str, bool]:
    """(body, externalize) for the source-feed comment. The marker line is
    FIRST and unquoted (the scan surface); externalize is ALWAYS False — a
    counted marker must never leave the feed body (markers.py doctrine).
    url None (upload failed) ⇒ the full record rides inline, blockquoted."""
    lines = [discovery_marker(run.seq, len(entries)),
             f"🔎 {len(entries)} discover{'y' if len(entries) == 1 else 'ies'}"
             f" from step {run.seq} ({run.mission_type}) — leads for related "
             f"missions, routed separately."]
    if url is not None:
        lines.append(f"Full record attached: [{name}]({url})")
        lines += render_entry_lines(entries)
    else:
        lines.append("(attachment upload failed — full record inline)")
        lines += render_entry_lines(entries, full=True)
    return "\n\n".join(lines), False


async def harvest(mgr, run: Run, result: dict) -> int:
    """The finalize hook — ONE call site (finalize.finalize, between the
    token report and the transition). Returns the harvested entry count;
    0 = nothing to do, no checkpoint, feed byte-identical to pre-ADR."""
    if run.pmo_kind != "issue" or run.mission_type not in HARVEST_TYPES:
        return 0
    entries = valid_entries(result)
    if not entries:
        return 0
    pmo_id = run.mission_pmo_id
    name = f"DISCOVERY_{run.seq}.md"

    async def _post():
        # The comment write is the commit point: label / pending / success
        # audit / notify happen only after the marker is on the feed.
        # A failed post is audited and re-raised so _checkpoint does not
        # record discovery:post — redelivery retries. The outer harvest
        # try still swallows so the close cannot wedge.
        cap = mgr.config.budgets.discoveries_per_run
        if cap and len(entries) > cap:
            mgr._audit(pmo_id, "discovery_capped",
                       f"{len(entries) - cap} of {len(entries)} entries "
                       f"dropped (budgets.discoveries_per_run={cap})")
            del entries[cap:]
        try:
            await post_attachment_comment(
                mgr, pmo_id, "issue", filename=name,
                content=redact(render_discovery_md(run, entries)),
                comment_of=lambda url: comment_body(run, entries, name, url))
        except Exception as e:  # noqa: BLE001 — audited; raise so we do not checkpoint
            mgr._audit(pmo_id, "discovery_post_failed", str(e)[:200])
            raise
        try:
            await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                      remove=set(), add={LABEL_DISCOVERY})
        except Exception as e:  # noqa: BLE001 — the label is a sweep hint; the feed marker is the truth
            mgr._audit(pmo_id, "discovery_label_failed", str(e)[:200])
        mgr._discoveries_pending.add(pmo_id)
        mgr._audit(pmo_id, "discovery_post", f"{name}: {len(entries)} entries")
        # event trigger for the routing lane (composition root injects the
        # callable; None in tests / pre-wiring) — best-effort, the sweep is
        # the durable path
        notify = getattr(mgr, "discovery_notify", None)
        if notify is not None:
            try:
                notify()
            except Exception:  # noqa: BLE001 — never let the trigger touch the close
                log.debug("discovery notify failed", exc_info=True)
        # PLAN_MEMORY §5.2: copy each entry onto every snapshotted
        # notebook. Failures stay inside append_from_harvest.
        writer = getattr(mgr, "claims", None)
        if writer is not None and entries:
            from .. import claims as claims_mod
            try:
                await claims_mod.append_from_harvest(
                    writer, mgr.config, run, entries,
                    audit=mgr._audit)
            except Exception:  # noqa: BLE001 — conveyor must never wedge harvest
                log.exception("claims conveyor failed for %s", run.run_id)

    try:
        await mgr._checkpoint(run, steps.DISCOVERY_POST, _post)
    except Exception:  # noqa: BLE001 — harvest must never wedge a close
        log.exception("discovery harvest failed for %s", run.run_id)
        return 0
    return len(entries)


# ── the ONE pending-scan pipe (recovery is board arithmetic) ─────────────────

@dataclass
class SourceState:
    posted: list[tuple[int, int]]     # (step, n) markers on the source feed
    receipted: set[tuple[int, str]]   # (step, target) routing receipts
    truncated: bool = False           # fail-closed: counts unknown

    @property
    def pending(self) -> list[tuple[int, int]]:
        if self.truncated:
            return []
        done = {s for s, _ in self.receipted}
        return [(s, n) for s, n in self.posted if s not in done]


async def scan_source(mgr, m, *, memo: bool = True) -> SourceState:
    """The ONE labeled-mission feed scan (shared by harvest recovery, the
    discovery sweep, and steward apply): posted markers and routing
    receipts, both over unquoted bodies (IRON RULE). full=True so newest
    receipts (gitea pages oldest-first) cannot fall off the window.
    truncated ⇒ fail-closed: callers must not treat the feed as empty or
    write to=-. pending = posted − receipted when counts are known —
    restart-proof board arithmetic, no local ledger.

    `memo` (ADR-0033 addendum): the per-cycle sweep reuses a recent scan
    while nothing changed (`FeedScanMemo`); a caller about to WRITE on the
    strength of the scan passes memo=False and pays the live read. A
    truncated scan is never memoized."""
    fm = getattr(mgr, "feed_memo", None) if memo else None
    if fm is not None:
        hit = fm.get("discovery", m)
        if hit is not None:
            board.bump(mgr, "feed_scan_memo_hits")
            return hit
        gen = fm.generation(m.pmo_id)
    board.bump(mgr, "feed_scan_reads")
    act = await mgr.pmo.get_activity(MissionRef(m.pmo_id, "issue"), full=True)
    posted: list[tuple[int, int]] = []
    receipted: set[tuple[int, str]] = set()
    for e in act.entries:
        text = unquoted(e.body)
        posted += discovery_posts(text)
        receipted |= discovery_receipts(text)
    state = SourceState(posted=posted, receipted=receipted,
                        truncated=bool(act.truncated))
    if fm is not None and not state.truncated:
        fm.put("discovery", m, state, gen)
    return state


async def pending_from_board(mgr, missions) -> dict[str, list[tuple[int, int]]]:
    """Label-gated recovery: ONLY missions carrying DEVCAKE-DISCOVERY get a
    feed read — the poll cycle has no unconditional per-mission feed read
    anywhere, and this keeps it that way. pmo_id → pending (step, n)."""
    out: dict[str, list[tuple[int, int]]] = {}
    for m in missions:
        if m.pmo_kind != "issue" or LABEL_DISCOVERY not in m.labels:
            continue
        state = await scan_source(mgr, m)
        if state.truncated:
            continue
        if state.pending:
            out[m.pmo_id] = state.pending
    return out


async def discovery_sweep(mgr, m) -> None:
    """The per-mission sweep arm (called from sweeps() for every issue; the
    label gate lives HERE so sweeps.py never reads the label — the guard
    allowlist stays tight). Re-seeds the advisory queue for pending batches,
    self-heals the label off fully-receipted sources, terminates batches
    whose source run record is gone (clear-runs) with a sentinel'd
    unroutable comment + a `to=-` receipt so the board arithmetic closes,
    and retires ceiling-truncated sources with a raise-to-human comment
    (addendum 14). Toggle-off (D11) leaves everything untouched — the label
    stays as honest board state until the operator re-enables routing."""
    if m.pmo_kind != "issue" or LABEL_DISCOVERY not in m.labels:
        return
    if not mgr.instance.discovery_routing:
        return
    state = await scan_source(mgr, m)
    if state.truncated:
        # past the full-read page ceiling — feeds only grow, so the board
        # arithmetic is permanently unknowable here. Raise to the humans
        # and retire the gate (addendum 14): no to=- (nothing countable to
        # disposition), one loud comment, label off, pending dropped. A
        # failed comment retries next sweep (rare duplicate on a failed
        # label drop is accepted — the alternative is silence).
        try:
            await mgr._feed(
                m.pmo_id, "issue",
                "⚠️ This mission's feed exceeds the readable page ceiling, "
                "so discovery-routing bookkeeping (delivery dedup, receipt "
                "arithmetic) is impossible and now retired for it. The "
                "DISCOVERY_<n>.md attachments above remain the record — "
                "carry them to related missions manually if they matter.",
                externalize=False)
            mgr._audit(m.pmo_id, "discovery_unreadable",
                       "feed past the full-read ceiling — routing "
                       "bookkeeping retired, raised to humans")
        except Exception as ex:  # noqa: BLE001 — retried next sweep
            mgr._audit(m.pmo_id, "discovery_receipt_failed", str(ex)[:200])
            return
        try:
            await mgr.pmo.swap_labels(MissionRef(m.pmo_id, "issue"),
                                      remove={LABEL_DISCOVERY}, add=set())
        except Exception as ex:  # noqa: BLE001 — the label is a hint; retried next sweep
            mgr._audit(m.pmo_id, "discovery_label_failed", str(ex)[:200])
        mgr._discoveries_pending.discard(m.pmo_id)
        return
    if not state.posted:
        return   # label without markers (human relabel) — humans own labels
    pending = state.pending
    if not pending:
        try:     # fully receipted — self-heal the gate off
            await mgr.pmo.swap_labels(MissionRef(m.pmo_id, "issue"),
                                      remove={LABEL_DISCOVERY}, add=set())
        except Exception as ex:  # noqa: BLE001 — the label is a hint; retried next sweep
            mgr._audit(m.pmo_id, "discovery_label_failed", str(ex)[:200])
        mgr._discoveries_pending.discard(m.pmo_id)
        return
    rows = mgr.runs.store.all()
    run_ix = harvest_run_index(mgr, rows)
    # A record that exists but is not terminal is IN FLIGHT (finalize has
    # posted the marker and is still closing, or a watchdog has yet to
    # rule): hold it this cycle — the pending id is kept and the label
    # stays. "Gone" is reserved for an absent record or a terminal one
    # without a usable result (clear-runs, a failed close). Closing an
    # in-flight batch with to=- is permanent: receipts are board arithmetic
    # and nothing reopens them once the result lands.
    inflight = {(r.mission_pmo_id, r.seq) for r in rows
                if r.mission_type in HARVEST_TYPES and mgr._run_is_ours(r)
                and r.state not in TERMINAL_STATES}
    gone = [(step, n) for step, n in pending
            if (m.pmo_id, step) not in inflight
            and ((m.pmo_id, step) not in run_ix
                 or not valid_entries(run_ix[(m.pmo_id, step)].result))]
    if gone:
        lines = [f"`devcake:discovery-routed:v1 step={s} to=-`"
                 for s, _n in sorted(gone)]
        try:
            await mgr._feed(
                m.pmo_id, "issue",
                "⚠️ Unroutable discoveries — the source run record was "
                "cleared, so verbatim transport is impossible. The full "
                "DISCOVERY file above remains the record; disposition "
                "receipts:\n" + "\n".join(lines), externalize=False)
            mgr._audit(m.pmo_id, "discovery_unroutable",
                       f"steps {[s for s, _ in gone]}: run record cleared")
        except Exception as ex:  # noqa: BLE001 — retried next sweep
            mgr._audit(m.pmo_id, "discovery_receipt_failed", str(ex)[:200])
            return
    if [p for p in pending if p not in gone]:
        mgr._discoveries_pending.add(m.pmo_id)   # routable work remains
    else:
        try:
            await mgr.pmo.swap_labels(MissionRef(m.pmo_id, "issue"),
                                      remove={LABEL_DISCOVERY}, add=set())
        except Exception as ex:  # noqa: BLE001 — retried next sweep
            mgr._audit(m.pmo_id, "discovery_label_failed", str(ex)[:200])
        mgr._discoveries_pending.discard(m.pmo_id)
