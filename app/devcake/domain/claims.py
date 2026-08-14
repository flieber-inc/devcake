"""`.claims/` conveyor — the fast lane (PLAN_MEMORY §5).

The app writes one file per harvested discovery onto every memory card
snapshotted on the run. No judgment. Failure never fails the discovering
run. The directory listing is the truth; `claims_depth` is an advisory
cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone

from ..ports.claims import ClaimsNotebooks
from .run import Run

log = logging.getLogger("devcake.claims")

CLAIMS_DIR = ".claims"
README_PATH = f"{CLAIMS_DIR}/README.md"
SCHEMA = "devcake-claims/v1"
_SAFE_ID = re.compile(r"^[a-z0-9]{8,64}$")

CLAIMS_README = (
    "These files are unvalidated leads the app copied from DevCake runs.\n"
    "They are not truths. They may contradict notes outside this folder.\n"
    "A Curator drains them: promote a lead to a note only with evidence,\n"
    "or discard it. Notes never live here. Do not edit this README.\n"
)

# advisory depth cache: card → json-file count. Restart-stale until the
# next append or list-only refresh.
claims_depth: dict[str, int] = {}
# health: last cap event per card (standing warning until a successful
# under-cap write). Restart-stale; next append refreshes.
claims_queue_capped: set[str] = set()


def claim_id(source_instance: str, source_pmo_id: str, step: int,
             index: int) -> str:
    """Deterministic safe path segment. The instance joins the hash so two
    boards on different forges with colliding pmo ids (numeric issue
    numbers) can never dedup each other's claims on a shared notebook."""
    raw = f"{source_instance}\0{source_pmo_id}\0{step}\0{index}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def claim_record(run: Run, entry: dict, index: int, *,
                 harvested_at: str | None = None) -> dict:
    about = entry.get("about")
    if not isinstance(about, list):
        about = []
    about = [a for a in about if isinstance(a, str) and a.strip()]
    return {
        "schema": SCHEMA,
        "id": claim_id(run.pmo_ref, run.mission_pmo_id, run.seq, index),
        "source_instance": run.pmo_ref,
        "source_key": run.mission_key,
        "source_pmo_id": run.mission_pmo_id,
        "step": run.seq,
        "index": index,
        "run_id": run.run_id,
        "finding": entry.get("finding") or "",
        "evidence": entry.get("evidence") or "",
        "scope": entry.get("scope") or "",
        "about": about,
        "harvested_at": harvested_at or datetime.now(timezone.utc).isoformat(),
    }


def snapshot_cards(run: Run) -> list[str]:
    """Cards the harvest may write — dispatch snapshot, minus repo_ref."""
    cards = []
    seen: set[str] = set()
    for mount in run.memory_mounts or []:
        name = mount.get("card") or ""
        if name and name != run.repo_ref and name not in seen:
            seen.add(name)
            cards.append(name)
    return cards


def _json_names(listing: list[str] | None) -> list[str]:
    if not listing:
        return []
    return [n for n in listing if n.endswith(".json") and not n.startswith(".")]


async def refresh_depth(notebooks: ClaimsNotebooks, card: str) -> int:
    names = await notebooks.list_json_names(card)
    if names is None:
        return claims_depth.get(card, 0)
    depth = len(_json_names(names))
    claims_depth[card] = depth
    return depth


async def append_from_harvest(notebooks: ClaimsNotebooks, cfg, run: Run,
                              entries: list[dict], *,
                              audit=None) -> dict[str, int]:
    """Write one `.claims/<id>.json` per entry onto every snapshot card.

    Returns `{card: n_written}`. Never raises to the caller — failures
    are audited. Does not withhold feed memorialization (caller already
    posted).
    """
    cards = snapshot_cards(run)
    if not cards or not entries:
        return {}
    written: dict[str, int] = {}
    cap = cfg.budgets.claims_queue_max
    for card in cards:
        try:
            n = await _append_one_notebook(
                notebooks, cfg, run, entries, card, cap=cap, audit=audit)
        except Exception:  # noqa: BLE001 — conveyor must never fail the discovering run
            log.exception("claims append failed for card %s run %s",
                          card, run.run_id)
            if audit:
                audit(run.mission_pmo_id, "claims_append_failed",
                      f"{card}: run={run.run_id}")
            continue
        if n:
            written[card] = n
    return written


async def _append_one_notebook(notebooks, cfg, run, entries, card, *,
                               cap, audit) -> int:
    if not notebooks.can_write(card):
        log.warning("claims: skipping notebook %s (no write / reference_only)",
                    card)
        if audit:
            audit(run.mission_pmo_id, "claims_skip_nowrite", card)
        return 0
    snap = await notebooks.snapshot(card)
    if snap is None:
        log.warning("claims: cannot list %s — skip", card)
        if audit:
            audit(run.mission_pmo_id, "claims_list_failed", card)
        return 0
    have = {n[:-5] if n.endswith(".json") else n
            for n in _json_names(snap.get("json_names"))}
    depth = len(have)
    claims_depth[card] = depth
    creates: dict[str, str] = {}
    if snap.get("has_readme") is False:
        creates[README_PATH] = CLAIMS_README
    for i, entry in enumerate(entries):
        cid = claim_id(run.pmo_ref, run.mission_pmo_id, run.seq, i)
        if not _SAFE_ID.match(cid) or cid in have:
            continue
        if cap and depth + len([p for p in creates if p.endswith(".json")]) >= cap:
            claims_queue_capped.add(card)
            if audit:
                audit(run.mission_pmo_id, "claims_queue_capped",
                      f"{card}: cap={cap} id={cid}")
            log.warning("claims queue capped on %s (cap=%s) — refused %s",
                        card, cap, cid)
            continue
        rec = claim_record(run, entry, i)
        creates[f"{CLAIMS_DIR}/{cid}.json"] = json.dumps(rec, indent=2) + "\n"
    json_creates = [p for p in creates if p.endswith(".json")]
    if not creates:
        return 0
    n = len(json_creates)
    await notebooks.commit(
        card, creates=creates, deletes=[],
        message=f"devcake:claims:v1 run={run.run_id} n={n}")
    claims_depth[card] = depth + n
    if cap and claims_depth[card] < cap:
        claims_queue_capped.discard(card)
    return n


async def prune_all(notebooks: ClaimsNotebooks, cards: list[str], *,
                    audit=None) -> dict[str, int]:
    """Clear-all prune: delete every `.claims/*.json` on every writable
    notebook (README stays). Clear wipes every board, and orphan claims
    from boards deleted before the Clear have no owner left — pruning by
    currently-configured source names would leak them forever."""
    pruned: dict[str, int] = {}
    for card in cards:
        if not notebooks.can_write(card):
            continue
        try:
            names = _json_names(await notebooks.list_json_names(card))
            if not names:
                continue
            await notebooks.commit(
                card, creates={},
                deletes=[f"{CLAIMS_DIR}/{n}" for n in names],
                message="devcake:claims:v1 prune source=clear-all")
            claims_depth[card] = 0
            pruned[card] = len(names)
        except Exception:  # noqa: BLE001 — clear must not fail the wipe
            log.exception("claims prune failed for %s", card)
            if audit:
                audit("clear", "claims_prune_failed", card)
    return pruned


async def prune_board(notebooks: ClaimsNotebooks, cards: list[str], *,
                      source_instance: str, source_pmo_id: str = "",
                      audit=None) -> dict[str, int]:
    """Delete `.claims/*.json` whose source matches the wiped board.
    Leave README. Never touch paths outside `.claims/`."""
    pruned: dict[str, int] = {}
    for card in cards:
        if not notebooks.can_write(card):
            continue
        try:
            n = await _prune_one(notebooks, card, source_instance,
                                 source_pmo_id)
        except Exception:  # noqa: BLE001 — clear must not fail the wipe
            log.exception("claims prune failed for %s", card)
            if audit:
                audit(source_pmo_id or source_instance, "claims_prune_failed",
                      card)
            continue
        if n:
            pruned[card] = n
    return pruned


async def _prune_one(notebooks, card, source_instance, source_pmo_id) -> int:
    records = await notebooks.list_claim_meta(card)
    if records is None:
        return 0
    deletes = []
    for rec in records:
        if ((source_instance and rec.get("source_instance") == source_instance)
                or (source_pmo_id and rec.get("source_pmo_id") == source_pmo_id)):
            cid = rec.get("id") or ""
            if _SAFE_ID.match(str(cid)):
                deletes.append(f"{CLAIMS_DIR}/{cid}.json")
    if not deletes:
        return 0
    await notebooks.commit(
        card, creates={}, deletes=deletes,
        message=f"devcake:claims:v1 prune source={source_instance}")
    await refresh_depth(notebooks, card)
    return len(deletes)
