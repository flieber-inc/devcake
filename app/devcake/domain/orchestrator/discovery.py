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
from dataclasses import dataclass, field

from ...security import redact
from ..model import LABEL_DISCOVERY, MissionRef
from ..run import TERMINAL_STATES, Run, utcnow
from . import board
from . import feed_memo
from . import steps
from .feed import (FOR_THE_RECORD, SECTION_ROUTING_RECEIPTS, FoldSection,
                   _part_coords, append_fold_section, blockquote,
                   collapsible_of, notice, post_attachment_comment,
                   strip_fold, unquoted)
from .markers import (DISCOVERY_FIELD_MAX, DISCOVERY_PREVIEW_MAX, defang,
                      discovery_marker, discovery_posts, discovery_receipts,
                      finding_fingerprint)

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
    # per-finding fingerprints (ADR-0033 addendum): a mission never receives
    # back, from a sibling, a finding it discovered itself
    lines += [f"`devcake:finding:v1 sha={finding_fingerprint(e)}`"
              for e in entries]
    if url is not None:
        lines.append(f"Full record attached: [{name}]({url})")
        lines += render_entry_lines(entries)
    else:
        lines.append("(attachment upload failed — full record inline)")
        lines += render_entry_lines(entries, full=True)
    return "\n\n".join(lines), False


@dataclass
class HarvestPart:
    """A run's discoveries as the step card carries them (ADR-0042): the
    capped entries, the attachment name and its rendered record. The
    card's fold section is `section_body(url)`; `harvest_commit` does the
    bookkeeping once the card is on the feed."""
    entries: list[dict]
    name: str
    md: str

    def section_body(self, run: Run, url: str | None) -> str:
        return comment_body(run, self.entries, self.name, url)[0]


def harvest_part(mgr, run: Run, result: dict) -> HarvestPart | None:
    """The pure half of the harvest: the gate, the valid entries, the cap
    (audited). None ⇒ nothing to memorialize."""
    if run.pmo_kind != "issue" or run.mission_type not in HARVEST_TYPES:
        return None
    entries = valid_entries(result)
    if not entries:
        return None
    cap = mgr.config.budgets.discoveries_per_run
    if cap and len(entries) > cap:
        mgr._audit(run.mission_pmo_id, "discovery_capped",
                   f"{len(entries) - cap} of {len(entries)} entries "
                   f"dropped (budgets.discoveries_per_run={cap})")
        del entries[cap:]
    name = f"DISCOVERY_{run.seq}.md"
    return HarvestPart(entries, name, redact(render_discovery_md(run, entries)))


async def harvest_commit(mgr, run: Run, part: HarvestPart) -> int:
    """The bookkeeping half, once the marker is on the feed (inside the
    step card): label, pending set, audit, routing trigger, claims
    conveyor — under the same DISCOVERY_POST checkpoint as before, so a
    redelivered finalize past the card still commits exactly once.
    Best-effort: never wedges the close."""
    pmo_id = run.mission_pmo_id
    entries = part.entries

    async def _commit():
        await _after_post(mgr, run, pmo_id, part.name, entries)

    try:
        await mgr._checkpoint(run, steps.DISCOVERY_POST, _commit)
    except Exception:  # noqa: BLE001 — harvest must never wedge a close
        log.exception("discovery harvest commit failed for %s", run.run_id)
        return 0
    return len(entries)


async def _after_post(mgr, run: Run, pmo_id: str, name: str,
                      entries: list[dict]) -> None:
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
            written = await claims_mod.append_from_harvest(
                writer, mgr.config, run, entries,
                audit=mgr._audit)
            for card, n in (written or {}).items():
                if n:
                    mgr.repo_cache.invalidate(card)   # own write
        except Exception:  # noqa: BLE001 — conveyor must never wedge harvest
            log.exception("claims conveyor failed for %s", run.run_id)


async def harvest(mgr, run: Run, result: dict) -> int:
    """The PRE-CARD harvest: its own comment threaded under the transcript.
    Kept for runs mid-flight at the ADR-0042 upgrade (finalize's legacy
    tail); the step card carries the harvest otherwise. Returns the
    harvested entry count; 0 = nothing to do, no checkpoint."""
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
            # nests under the step's transcript comment where the vendor
            # threads (docs/03 §8); same body and markers either way
            await post_attachment_comment(
                mgr, pmo_id, "issue", filename=name,
                content=redact(render_discovery_md(run, entries)),
                comment_of=lambda url: comment_body(run, entries, name, url),
                reply_to=run.feed_anchor or None)
        except Exception as e:  # noqa: BLE001 — audited; raise so we do not checkpoint
            mgr._audit(pmo_id, "discovery_post_failed", str(e)[:200])
            raise
        await _after_post(mgr, run, pmo_id, name, entries)

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
    # the LIVE read's entries (memo=False only — never memoized): the
    # receipt append finds the step card's current body here, so a writer
    # never pays a second read (ADR-0042 §6)
    entries: list = field(default_factory=list, repr=False)

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

    `memo` (ADR-0033 addendum): the per-cycle sweep and the discovery
    drain reuse a recent scan while nothing changed (`FeedScanMemo`); a
    caller about to WRITE on the strength of the scan (a label drop, a
    `to=-` close, the steward's own apply) passes memo=False and pays the
    live read. A truncated scan is never memoized."""
    fm = getattr(mgr, "feed_memo", None) if memo else None
    if fm is not None:
        hit = fm.get("discovery", m)
        if hit is not None:
            board.bump(mgr, "feed_scan_memo_hits")
            return hit
        gen = fm.generation(m.pmo_id)
        floor = fm.witnessed(m.pmo_id)     # captured before the await, like gen
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
        fm.put("discovery", m, state, gen,
               feed_until=feed_memo.feed_until(act.entries), floor=floor)
    if not memo:
        # the live path only: a memoized value must stay a small summary
        state.entries = list(act.entries)
    return state


def _fold_anchor(state, source_run) -> tuple[str, str] | None:
    """(entry id, current body) of the step card that harvested a batch —
    the source run's `feed_anchor` — when the live scan holds it as a
    comment with a fold that is not paged; None otherwise (a pre-card
    harvest, a paged card, an anchor a person deleted)."""
    anchor = getattr(source_run, "feed_anchor", "") if source_run else ""
    if not anchor:
        return None
    for e in getattr(state, "entries", None) or ():
        if e.entry_id != anchor:
            continue
        body = e.body or ""
        if _part_coords(body) is None and strip_fold(body)[1] is not None:
            return anchor, body
        return None
    return None


async def record_receipts(mgr, pmo_id: str, state, pairs, *, run_ix,
                          body_of, what: str) -> None:
    """Routing receipts never make a new entry when they can help it
    (ADR-0042 §6): per step, the receipt lines — `body_of(pairs)`, today's
    receipt comment for exactly those pairs, verbatim — are appended as a
    `Routing receipts` section (stamped with the append time) to the fold
    of the step card that harvested them, through the edit chokepoint.
    Steps without a card in the live scan land together on ONE ⚠️ notice
    whose Record is today's comment for them; an append that fails for any
    reason (a paged or over-cap body → ValueError, a transient, a vanished
    entry) is audited `discovery_receipt_fold_failed` and takes the same
    fallback, so `pending = posted − receipted` always closes — the scan
    reads folds and notices alike through `unquoted`. The fallback post's
    own failure propagates: the caller audits and the sweep re-drives."""
    by_step: dict[int, list] = {}
    for s, t in pairs:
        by_step.setdefault(s, []).append((s, t))
    fallback: list = []
    for step in sorted(by_step):
        found = _fold_anchor(state, run_ix.get((pmo_id, step)))
        if found is None:
            fallback += by_step[step]
            continue
        entry_id, body = found
        section = FoldSection(SECTION_ROUTING_RECEIPTS,
                              body_of(by_step[step]), at=utcnow())
        try:
            await mgr._edit(pmo_id, "issue", entry_id, append_fold_section(
                body, section, collapsible=collapsible_of(mgr)))
        except Exception as ex:  # noqa: BLE001 — the receipt must land somewhere: audited, then today's post
            mgr._audit(pmo_id, "discovery_receipt_fold_failed",
                       f"step {step}: {str(ex)[:160]}")
            fallback += by_step[step]
    if fallback:
        await mgr._feed(pmo_id, "issue",
                        notice(mgr, FOR_THE_RECORD, what, body_of(fallback)),
                        externalize=False)


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


def _gone_batches(mgr, m, pending) -> list[tuple[int, int]]:
    """Pending batches whose source run record is gone. A record that
    exists but is not terminal is IN FLIGHT (finalize has posted the marker
    and is still closing, or a watchdog has yet to rule): hold it this
    cycle — the pending id is kept and the label stays. "Gone" is reserved
    for an absent record or a terminal one without a usable result
    (clear-runs, a failed close). Closing an in-flight batch with to=- is
    permanent: receipts are board arithmetic and nothing reopens them once
    the result lands."""
    rows = mgr.runs.store.all()
    run_ix = harvest_run_index(mgr, rows)
    inflight = {(r.mission_pmo_id, r.seq) for r in rows
                if r.mission_type in HARVEST_TYPES and mgr._run_is_ours(r)
                and r.state not in TERMINAL_STATES}
    return [(step, n) for step, n in pending
            if (m.pmo_id, step) not in inflight
            and ((m.pmo_id, step) not in run_ix
                 or not valid_entries(run_ix[(m.pmo_id, step)].result))]


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
                notice(
                    mgr, FOR_THE_RECORD,
                    "This mission's feed is past the readable page ceiling, "
                    "so discovery-routing bookkeeping is retired for it; the "
                    "DISCOVERY files above remain the record.",
                    "⚠️ This mission's feed exceeds the readable page ceiling, "
                    "so discovery-routing bookkeeping (delivery dedup, receipt "
                    "arithmetic) is impossible and now retired for it. The "
                    "DISCOVERY_<n>.md attachments above remain the record — "
                    "carry them to related missions manually if they matter.",
                    todo="Carry them to related missions by hand if they "
                         "matter."),
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
    if not pending or _gone_batches(mgr, m, pending):
        # about to WRITE (drop the label, or close batches with to=-) on
        # the strength of a possibly memoized scan: confirm live first —
        # one extra read only in the cycles that act (ADR-0033 addendum)
        state = await scan_source(mgr, m, memo=False)
        if state.truncated or not state.posted:
            return   # the next sweep takes the fresh path
        pending = state.pending
    if not pending:
        try:     # fully receipted — self-heal the gate off
            await mgr.pmo.swap_labels(MissionRef(m.pmo_id, "issue"),
                                      remove={LABEL_DISCOVERY}, add=set())
        except Exception as ex:  # noqa: BLE001 — the label is a hint; retried next sweep
            mgr._audit(m.pmo_id, "discovery_label_failed", str(ex)[:200])
        mgr._discoveries_pending.discard(m.pmo_id)
        return
    gone = _gone_batches(mgr, m, pending)
    if gone:
        def unroutable_body(sel) -> str:
            """Today's unroutable comment for these (step, `-`) pairs."""
            lines = [f"`devcake:discovery-routed:v1 step={s} to=-`"
                     for s, _t in sel]
            return ("⚠️ Unroutable discoveries — the source run record was "
                    "cleared, so verbatim transport is impossible. The full "
                    "DISCOVERY file above remains the record; disposition "
                    "receipts:\n" + "\n".join(lines))
        try:
            await record_receipts(
                mgr, m.pmo_id, state, [(s, "-") for s, _n in sorted(gone)],
                run_ix=harvest_run_index(mgr), body_of=unroutable_body,
                what="Some discoveries cannot be routed — their source run "
                     "record was cleared; the DISCOVERY files above remain "
                     "the record.")
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
