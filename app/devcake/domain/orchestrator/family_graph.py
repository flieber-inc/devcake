"""ADR-0033 — the family graph: decomposition tree ∪ blocked_by component.

A discovery's routing universe (Decisions 4/6): every mission connected to
the source through decomposition parentage or blocked_by edges — siblings
and cousins included, terminal members included (their handoffs are prior
context). Never cross-instance by construction: the walk runs over ONE
instance's poll snapshot, and DevCake creates no cross-instance edges.
Pure graph arithmetic over Mission records plus one repo-union helper; no
PMO calls, no adapters.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..model import LABEL_CREATED, Mission
from ..repo_sourcing import blocker_read_credential
from . import dispatch
from .markers import decomposition_marker


@dataclass
class Family:
    members: list[Mission]

    @property
    def by_id(self) -> dict[str, Mission]:
        return {m.pmo_id: m for m in self.members}

    @property
    def by_key(self) -> dict[str, Mission]:
        return {m.key.upper(): m for m in self.members if m.key}


def _parent_ref(m: Mission) -> str | None:
    """The decomposition parent edge — trusted only under the app-managed
    DEVCAKE-CREATED label (decomposition_depth precedent): a forged marker
    in an untrusted description must never join a mission to an arbitrary
    family (routing-universe poisoning)."""
    if LABEL_CREATED not in m.labels:
        return None
    mk = decomposition_marker(m.description)
    return mk.group(1) if mk else None


def family_of(source: Mission, missions: list[Mission]) -> Family:
    """BFS over the undirected union of blocked_by and decomposition-parent
    edges, seeded at `source`. `missions` is one instance's snapshot;
    matching is by pmo_id (the marker's parent= carries the parent's
    pmo_id; key accepted as a defensive alias). blocked_by edges are
    PMO-native and trusted as-is."""
    pool = [m for m in missions if m.pmo_id]
    by_id = {m.pmo_id: m for m in pool}
    by_key = {m.key.upper(): m for m in pool if m.key}

    def resolve(ref: str | None) -> Mission | None:
        if not ref:
            return None
        return by_id.get(ref) or by_key.get(ref.upper())

    adj: dict[str, set[str]] = {m.pmo_id: set() for m in pool}

    def join(a: str, b: str) -> None:
        adj[a].add(b)
        adj[b].add(a)

    for m in pool:
        for blocker in (m.blocked_by or []):
            other = resolve(blocker)
            if other is not None:
                join(m.pmo_id, other.pmo_id)
        parent = resolve(_parent_ref(m))
        if parent is not None:
            join(m.pmo_id, parent.pmo_id)

    seen = {source.pmo_id}
    queue = [source.pmo_id]
    while queue:
        for nxt in adj.get(queue.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    members = [m for m in pool if m.pmo_id in seen]
    if all(m.pmo_id != source.pmo_id for m in members):
        members.insert(0, source)   # source off-snapshot: still a family of 1
    return Family(members=members)


def family_work_repos(mgr, family: Family, all_runs=None, *,
                      exclude: frozenset[str] = frozenset()) -> list[dict]:
    """The family's work-repo union as blocker_work-shaped entries
    [{repo_ref, mission_key}] — the discovery steward's read-only clone set
    (rides Run.blocker_work; MAX_BLOCKER_WORK_EXTRAS clone cap, ADR-0017).
    Uses the poll-stamped m.repo, else the PURE resolver — never
    resolve_repo_live, whose zero-repo arm provisions internal repos as a
    side effect. Names without a read credential are dropped
    (blocker_read_credential — the ONE mountability rule); dedup by repo;
    `exclude` drops the steward's own primary repo."""
    if all_runs is None:
        all_runs = mgr.runs.store.all()
    entries: list[dict] = []
    seen: set[str] = set(exclude)
    for m in family.members:
        if m.pmo_kind != "issue":
            continue
        name = m.repo or dispatch.resolve_repo(mgr, m, all_runs)
        if not name or name in seen:
            continue
        if blocker_read_credential(mgr, name) is None:
            continue
        seen.add(name)
        entries.append({"repo_ref": name, "mission_key": m.key})
        if len(entries) >= dispatch.MAX_BLOCKER_WORK_EXTRAS:
            break
    return entries
