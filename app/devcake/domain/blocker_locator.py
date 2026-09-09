"""Deployment-wide blocker Mission resolution (ADR-0009 amendment).

A `blocked_by` edge stores an opaque vendor pmo_id. Within one instance the
poll snapshot or the instance's own adapter resolves it; under multi-PMO
(ADR-0009) the id may belong to a PEER instance in the same vendor
environment — an edge an external agent drew on the board (ADR-0007).
The locator widens WHERE a blocker id is looked up — never what "blocked"
means (open/done semantics stay with the callers), and never dispatch
ownership: this path is read-only, claims nothing, and writes nothing.

Attribution is the load-bearing half: `accepted_pmo_refs` names WHICH
instances' run histories may serve the blocker, so resolve_blocker_work can
widen its run index without reopening the colliding-id hole — gitea_issues
pmo_ids are per-repo issue numbers, so two instances legitimately share
mission_pmo_id "3" for unrelated missions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from .model import Mission, MissionRef

# Peer resolution runs inside the LOCAL instance's poll segment: a sick peer
# adapter (revoked key hanging to its 20 s client timeout, vendor brownout)
# must not couple its latency into every local cycle. A peer that cannot
# answer within this budget counts as a miss for THIS resolution path —
# fail-safe unchanged (miss ⇒ next candidate ⇒ at worst unreadable-open,
# self-healing next cycle).
PEER_GET_TIMEOUT_S = 5.0

# Peer resolution is legal only when the LOCAL adapter declares
# PMOCapabilities.global_ids (2026-08 evaluation F10). The old
# GLOBAL_ID_SYSTEMS vendor-name literal meant adding a PMO required
# editing domain policy — capabilities keep domain vendor-agnostic.

# Pre-schema-v3 run records always count for the attributed instance —
# hiding them would orphan pre-v3 blocker work (mirrors
# MissionManager._run_is_ours). Definition lives with the Run model;
# re-exported here for existing importers (manager._run_is_ours).
from .run import LEGACY_PMO_REFS  # noqa: E402 — deliberate re-export


@dataclass(frozen=True)
class Resolved:
    """A located blocker Mission plus run-history attribution."""
    mission: Mission
    accepted_pmo_refs: frozenset[str]


class BlockerLocator:
    """One shared instance per deployment, holding LIVE references: the
    composition root's managers dict mutates in place on config reload, and
    the owner getter reads PollRuntime's durable claim map at call time."""

    def __init__(self, managers: dict, owner_of: Callable[[str], str | None]):
        self._managers = managers          # live: instance name → manager
        self._owner_of = owner_of          # bid → owning instance name | None

    async def resolve(self, bid: str, *, local_mgr,
                      memo: dict) -> Resolved | None:
        """Mission for `bid` plus attribution, or None (= unreadable — callers
        keep the ADR-0007 fail-safe: a missing Mission is NEVER done). This
        seam is for OFF-SNAPSHOT ids only — the gate resolves snapshot hits
        against its own by_id first. Memoized in the caller-supplied `memo`
        (one dict per poll segment or dispatch), so a shared blocker costs
        one network walk per cycle. One id; `resolve_many` is the batch
        form and the one the dispatch path uses."""
        return (await self.resolve_many([bid], local_mgr=local_mgr,
                                        memo=memo))[bid]

    async def resolve_many(self, bids: list[str], *, local_mgr,
                           memo: dict) -> dict[str, Resolved | None]:
        """Every id at once: memo hits served, the rest resolved in as few
        wire reads as the adapters allow (`PMOCapabilities.batch_get` — one
        query per hundred ids on Linear; a `get` per id elsewhere). Order:
        the owner map's peer for the ids it names → the LOCAL adapter for
        everything still open → the same-system peer scan (config order,
        first success) for the misses. Local precedes the scan because a
        mission's blockers are overwhelmingly its own board's, and a
        same-workspace key reads a peer's issue anyway; attribution for a
        local hit is every same-system instance (safe exactly where ids
        cannot collide, which is the only place peers are consulted). A
        wire failure on any path counts as a miss for that path — never a
        retry per id, which is the storm this batch form exists to end.
        Every answer, negative included, lands in `memo`."""
        todo = [b for b in dict.fromkeys(bids) if b not in memo]
        if todo:
            for bid, r in (await self._resolve_batch(todo, local_mgr)).items():
                memo[bid] = r
        return {b: memo.get(b) for b in bids}

    async def _resolve_batch(self, bids: list[str],
                             local_mgr) -> dict[str, Resolved | None]:
        out: dict[str, Resolved | None] = {b: None for b in bids}
        system = local_mgr.instance.system
        try:
            peers_allowed = bool(local_mgr.pmo.capabilities().global_ids)
        except Exception:  # noqa: BLE001 — a capability probe failure must fail CLOSED (no peer resolution), never crash the locator
            peers_allowed = False
        pending = list(bids)
        tried_owner: dict[str, str] = {}
        if peers_allowed:
            by_owner: dict[str, list[str]] = {}
            for b in pending:
                owner = self._owner_of(b)
                peer = self._managers.get(owner) if owner else None
                if peer is not None and peer is not local_mgr \
                        and peer.instance.system == system:
                    by_owner.setdefault(owner, []).append(b)
            for owner, ids in by_owner.items():
                peer = self._managers[owner]
                got = await self._fetch(peer, ids, timeout=PEER_GET_TIMEOUT_S)
                for b, m in got.items():
                    # Same attribution shape as local: LEGACY stamps so a
                    # multi-PMO upgrade does not orphan pre-v3 peer runs.
                    out[b] = Resolved(m, self._instance_refs(peer))
                for b in ids:
                    tried_owner[b] = owner
            pending = [b for b in pending if out[b] is None]
        if pending:
            got = await self._fetch(local_mgr, pending)
            # Same-workspace vendor keys can resolve a peer's id through the
            # LOCAL adapter; the true owner is then unknown and the Mission
            # is stamped with the local instance (adapters are
            # instance-bound). Widening attribution to every same-system
            # instance is safe only where ids cannot collide.
            refs = set(self._instance_refs(local_mgr))
            if peers_allowed:
                refs |= {m.instance_name for m in self._managers.values()
                         if m.instance.system == system}
            for b, m in got.items():
                out[b] = Resolved(m, frozenset(refs))
            pending = [b for b in pending if out[b] is None]
        if peers_allowed and pending:
            # The owner map misses done+aged-out blockers BY DESIGN
            # (release_stale_ownership frees entries the owner no longer
            # sees), so for a genuinely foreign id this scan is the path.
            # Config order; first success wins (safe only because global
            # ids cannot collide).
            for peer in self._managers.values():
                if peer is local_mgr or peer.instance.system != system:
                    continue
                ids = [b for b in pending
                       if tried_owner.get(b) != peer.instance_name]
                if not ids:
                    continue
                got = await self._fetch(peer, ids, timeout=PEER_GET_TIMEOUT_S)
                for b, m in got.items():
                    out[b] = Resolved(m, self._instance_refs(peer))
                pending = [b for b in pending if out[b] is None]
                if not pending:
                    break
        return out

    async def _fetch(self, mgr, bids: list[str],
                     timeout: float | None = None) -> dict[str, Mission]:
        """The missions `mgr` can read among `bids`, keyed by id. One
        `get_many` call when the adapter declares batch_get (the timeout
        then covers the whole call, scaled by its page count), else one
        `get` per id. A failure is a miss, never a retry."""
        try:
            batch = bool(mgr.pmo.capabilities().batch_get)
        except Exception:  # noqa: BLE001 — an unreadable capability row means the plain path, not a crash
            batch = False
        if not batch:
            out: dict[str, Mission] = {}
            for b in bids:
                m = await self._get(mgr, b, timeout=timeout)
                if m is not None:
                    out[b] = m
            return out
        refs = [MissionRef(b, "issue") for b in bids]
        try:
            if timeout is not None:
                pages = max(1, -(-len(bids) // 100))
                async with asyncio.timeout(timeout * pages):
                    return dict(await mgr.pmo.get_many(refs))
            return dict(await mgr.pmo.get_many(refs))
        except Exception:  # noqa: BLE001 — one unreadable/slow path falls through to the next candidate; unresolved blockers stay open (ADR-0007 fail-safe)
            return {}

    @staticmethod
    async def _get(mgr, bid: str,
                   timeout: float | None = None) -> Mission | None:
        # Native blocked_by edges are issue-kind across adapters (projects
        # always normalize blocked_by=[]). Kind is therefore not part of the
        # opaque id — always query as issue.
        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    return await mgr.pmo.get(MissionRef(bid, "issue"))
            return await mgr.pmo.get(MissionRef(bid, "issue"))
        except Exception:  # noqa: BLE001 — one unreadable/slow path falls through to the next candidate; a fully-unresolved blocker stays open (ADR-0007 fail-safe)
            return None

    @staticmethod
    def _instance_refs(mgr) -> frozenset[str]:
        """Run-history stamps accepted for work attributed to `mgr`.

        Pre-schema-v3 records always count as that instance's history —
        hiding them would orphan pre-v3 blocker work (mirrors
        MissionManager._run_is_ours). Used for both local and peer
        attribution so peer-resolved blockers keep the same contract.
        """
        return LEGACY_PMO_REFS | {mgr.instance_name}
