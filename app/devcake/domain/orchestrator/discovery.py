"""ADR-0033 harvest half — the counterflow lane's memorialization seam.

A Dev's `discoveries` (structured finding/evidence/scope entries, docs/03 §6)
are surplus learning: the memory this otherwise memoryless system keeps
between runs (founder ruling 2026-08-13). Harvest is UNCONDITIONAL
memorialization (Decision 11): render DISCOVERY_<seq>.md and attach it as
the Mission Step's deliverable, post the marked source-feed comment, add the
DEVCAKE-DISCOVERY sweep-gate label, seed the advisory pending set. Routing
(the STEWARD discovery flavor, PR-2) consumes the pending state;
`scan_source` is the ONE pending-scan pipe both halves share, and
`render_entry_lines` the ONE feed-comment entry renderer (chokepoint
rulings). Error doctrine is HANDOFF's, not completion's F4: every sub-step
is best-effort — harvest must never wedge a close.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...security import redact
from ..model import LABEL_DISCOVERY, MissionRef
from ..run import Run, utcnow
from . import steps
from .feed import blockquote, post_attachment_comment, unquoted
from .markers import (DISCOVERY_FIELD_MAX, DISCOVERY_PREVIEW_MAX, defang,
                      discovery_marker, discovery_posts, discovery_receipts)

log = logging.getLogger("devcake.missions")

# result.json authorship (ADR-0033 D1): PLAN's result is entrypoint-
# synthesized and steward runs are excluded (chain-reaction damper D7).
HARVEST_TYPES = ("ONBOARD", "EXECUTE", "REVIEW")
_FIELDS = ("finding", "evidence", "scope")


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
            out.append(vals)
    return out


def render_discovery_md(run: Run, entries: list[dict]) -> str:
    """The attachment body — the full-fidelity knowledge-base record
    (founder ruling 2b: ALWAYS a step deliverable). Caller redacts."""
    lines = [f"# Discoveries — {run.mission_key} step {run.seq} "
             f"({run.mission_type})",
             f"run: `{run.run_id}` · {utcnow():%Y-%m-%d}", ""]
    for i, e in enumerate(entries, 1):
        lines += [f"## {i}. Finding", e["finding"], "",
                  f"**Evidence:** {e['evidence']}", "",
                  f"**Scope:** {e['scope']}", ""]
    return "\n".join(lines)


def render_entry_lines(entries: list[dict], *, full: bool = False) -> list[str]:
    """The ONE entry renderer for feed comments (chokepoint ruling — PR-2's
    recipient delivery reuses it): per entry a numbered header plus the
    defanged, blockquoted text — previews by default, the whole record with
    full=True (the upload-failed fallback). Marker/provenance lines stay
    unquoted at the caller; entry text is quarantined here (ADR-0014 D2,
    and quoted lines never count in any scan)."""
    out = []
    for i, e in enumerate(entries, 1):
        if full:
            text = (f"Finding: {e['finding']}\n\nEvidence: {e['evidence']}"
                    f"\n\nScope: {e['scope']}")
        else:
            text = e["finding"]
            if len(text) > DISCOVERY_PREVIEW_MAX:
                text = text[:DISCOVERY_PREVIEW_MAX] + "…"
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
    cap = mgr.config.budgets.discoveries_per_run
    if cap and len(entries) > cap:
        mgr._audit(pmo_id, "discovery_capped",
                   f"{len(entries) - cap} of {len(entries)} entries dropped "
                   f"(budgets.discoveries_per_run={cap})")
        entries = entries[:cap]
    name = f"DISCOVERY_{run.seq}.md"

    async def _post():
        # HANDOFF doctrine: each sub-step best-effort — a PMO hiccup is
        # audited, never propagated into the close
        try:
            await post_attachment_comment(
                mgr, pmo_id, "issue", filename=name,
                content=redact(render_discovery_md(run, entries)),
                comment_of=lambda url: comment_body(run, entries, name, url))
        except Exception as e:  # noqa: BLE001 — best-effort BY DESIGN
            mgr._audit(pmo_id, "discovery_post_failed", str(e)[:200])
        try:
            await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                      remove=set(), add={LABEL_DISCOVERY})
        except Exception as e:  # noqa: BLE001 — the label is a sweep hint; the feed marker is the truth
            mgr._audit(pmo_id, "discovery_label_failed", str(e)[:200])
        mgr._discoveries_pending.add(pmo_id)
        mgr._audit(pmo_id, "discovery_post", f"{name}: {len(entries)} entries")

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
    receipted: set[tuple[int, str]]   # (step, target) routing receipts (PR-2)

    @property
    def pending(self) -> list[tuple[int, int]]:
        done = {s for s, _ in self.receipted}
        return [(s, n) for s, n in self.posted if s not in done]


async def scan_source(mgr, m) -> SourceState:
    """The ONE labeled-mission feed scan (shared with PR-2's sweep arm):
    posted markers and routing receipts, both over unquoted bodies (IRON
    RULE). pending = posted − receipted — restart-proof board arithmetic,
    no local ledger (ADR-0033 D3)."""
    act = await mgr.pmo.get_activity(MissionRef(m.pmo_id, "issue"))
    posted: list[tuple[int, int]] = []
    receipted: set[tuple[int, str]] = set()
    for e in act.entries:
        text = unquoted(e.body)
        posted += discovery_posts(text)
        receipted |= discovery_receipts(text)
    return SourceState(posted=posted, receipted=receipted)


async def pending_from_board(mgr, missions) -> dict[str, list[tuple[int, int]]]:
    """Label-gated recovery: ONLY missions carrying DEVCAKE-DISCOVERY get a
    feed read — the poll cycle has no unconditional per-mission feed read
    anywhere, and this keeps it that way. pmo_id → pending (step, n)."""
    out: dict[str, list[tuple[int, int]]] = {}
    for m in missions:
        if m.pmo_kind != "issue" or LABEL_DISCOVERY not in m.labels:
            continue
        state = await scan_source(mgr, m)
        if state.pending:
            out[m.pmo_id] = state.pending
    return out
