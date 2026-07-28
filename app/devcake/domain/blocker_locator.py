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

# Systems whose pmo_ids are globally unique across the vendor environment
# (Linear UUIDs). Only these may resolve via PEER adapters, and only these
# may accept peer run history on a locally-resolved foreign id. Colliding-id
# systems (gitea_issues) never cross instances — hard refuse, not best-effort.
GLOBAL_ID_SYSTEMS = frozenset({"linear"})

# Pre-schema-v3 run records ("", "main") always count as local — hiding them
# would orphan pre-v3 blocker work (mirrors MissionManager._run_is_ours).
LEGACY_PMO_REFS = frozenset({"", "main"})


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
        one network walk per cycle."""
        if bid in memo:
            return memo[bid]
        memo[bid] = await self._resolve_uncached(bid, local_mgr)
        return memo[bid]

    async def _resolve_uncached(self, bid: str, local_mgr) -> Resolved | None:
        system = local_mgr.instance.system
        peers_allowed = system in GLOBAL_ID_SYSTEMS
        owner = self._owner_of(bid) if peers_allowed else None
        if peers_allowed:
            peer = self._managers.get(owner) if owner else None
            if peer is not None and peer is not local_mgr \
                    and peer.instance.system == system:
                got = await self._get(peer, bid, timeout=PEER_GET_TIMEOUT_S)
                if got is not None:
                    return Resolved(got, frozenset({peer.instance_name}))
            # The owner map misses done+aged-out blockers BY DESIGN
            # (release_stale_ownership frees entries the owner no longer
            # sees) — for extant pipelines this scan is the hot path, not a
            # last resort. Config order; first success wins (safe only
            # because global ids cannot collide).
            for peer in self._managers.values():
                if peer is local_mgr or peer.instance.system != system:
                    continue
                if owner is not None and peer.instance_name == owner:
                    continue       # already tried via the owner map
                got = await self._get(peer, bid, timeout=PEER_GET_TIMEOUT_S)
                if got is not None:
                    return Resolved(got, frozenset({peer.instance_name}))
        got = await self._get(local_mgr, bid)
        if got is not None:
            # Same-workspace vendor keys can resolve a peer's id through the
            # LOCAL adapter; the true owner is then unknown and the Mission
            # is stamped with the local instance (adapters are
            # instance-bound). Widening attribution to every same-system
            # instance is safe only where ids cannot collide.
            refs = self._local_refs(local_mgr)
            if peers_allowed:
                refs |= {m.instance_name for m in self._managers.values()
                         if m.instance.system == system}
            return Resolved(got, frozenset(refs))
        return None

    @staticmethod
    async def _get(mgr, bid: str,
                   timeout: float | None = None) -> Mission | None:
        try:
            if timeout is not None:
                async with asyncio.timeout(timeout):
                    return await mgr.pmo.get(MissionRef(bid, "issue"))
            return await mgr.pmo.get(MissionRef(bid, "issue"))
        except Exception:  # noqa: BLE001 — one unreadable/slow path falls through to the next candidate; a fully-unresolved blocker stays open (ADR-0007 fail-safe)
            return None

    @staticmethod
    def _local_refs(local_mgr) -> frozenset[str]:
        return LEGACY_PMO_REFS | {local_mgr.instance_name}
