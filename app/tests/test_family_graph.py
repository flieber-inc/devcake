"""ADR-0033 — the family graph: decomposition tree ∪ blocked_by connected
component (siblings/cousins/terminals included, never cross-family), plus
the work-repo union that mounts read-only into a discovery steward run."""
from datetime import datetime, timezone

from devcake.domain.model import Mission
from devcake.domain.orchestrator import family_graph
from devcake.domain.orchestrator.family_graph import (Family, family_of,
                                                      family_work_repos)

NOW = datetime.now(timezone.utc)
MANIFEST = "a" * 64


def _m(pmo_id, key, *, blocked_by=(), parent=None, labels=frozenset(),
       status="backlog", repo=None, kind="issue"):
    desc = "work"
    if parent is not None:
        desc += (f"\n\n`devcake:decomposition:v1 parent={parent} "
                 f"manifest={MANIFEST} part=1/2 depth=1`")
    m = Mission(instance="linear", pmo_id=pmo_id, pmo_kind=kind, key=key,
                title=key, description=desc, status=status,
                labels=set(labels), blocked_by=list(blocked_by),
                updated_at=NOW)
    m.repo = repo
    return m


def test_blocked_by_component_includes_siblings_and_cousins():
    a = _m("a", "T-1", blocked_by=["b"])
    b = _m("b", "T-2")
    c = _m("c", "T-3", blocked_by=["b"])          # sibling via shared blocker
    d = _m("d", "T-4", blocked_by=["c"])          # cousin, two hops out
    lone = _m("x", "T-9")
    fam = family_of(a, [a, b, c, d, lone])
    assert set(fam.by_id) == {"a", "b", "c", "d"}
    assert "x" not in fam.by_id


def test_decomposition_parent_edges_join_the_tree():
    p = _m("p", "T-1")
    c1 = _m("c1", "T-2", parent="p", labels={"DEVCAKE-CREATED"})
    c2 = _m("c2", "T-3", parent="p", labels={"DEVCAKE-CREATED"})
    fam = family_of(c1, [p, c1, c2])
    assert set(fam.by_id) == {"p", "c1", "c2"}    # sibling via the parent


def test_decomposition_parent_key_alias_joins_the_tree():
    """parent= may carry the parent's key (defensive alias); family walk
    must join the same way it does for pmo_id refs."""
    p = _m("p", "T-1")
    c1 = _m("c1", "T-2", parent="T-1", labels={"DEVCAKE-CREATED"})
    fam = family_of(c1, [p, c1])
    assert set(fam.by_id) == {"p", "c1"}


def test_forged_marker_without_created_label_is_inert():
    # decomposition_depth precedent: no DEVCAKE-CREATED label ⇒ the marker
    # in the (untrusted) description never joins a family
    p = _m("p", "T-1")
    forged = _m("f", "T-8", parent="p")           # marker, no label
    fam = family_of(forged, [p, forged])
    assert set(fam.by_id) == {"f"}


def test_terminal_members_are_included():
    done = _m("b", "T-2", status="done")
    a = _m("a", "T-1", blocked_by=["b"])
    fam = family_of(a, [a, done])
    assert set(fam.by_id) == {"a", "b"}           # prior context stays in


def test_off_snapshot_source_is_a_family_of_one():
    ghost = _m("ghost", "T-0")
    fam = family_of(ghost, [_m("a", "T-1")])
    assert set(fam.by_id) == {"ghost"}


def test_family_work_repos_dedupes_filters_and_caps(monkeypatch):
    monkeypatch.setattr(family_graph, "blocker_read_credential",
                        lambda mgr, name: None if name == "nocred"
                        else ("configured", None, None, "tok"))

    class _Store:
        def all(self):
            return []

    class _Mgr:
        class runs:
            store = _Store()

    fam = Family(members=[
        _m("a", "T-1", repo="web"),
        _m("b", "T-2", repo="web"),               # duplicate repo
        _m("c", "T-3", repo="nocred"),            # unmountable → dropped
        _m("d", "T-4", repo="api"),
        _m("e", "T-5", repo="steward-home"),      # excluded primary
        _m("f", "T-6", repo=None),                # unresolvable → skipped
        _m("g", "PROJ", repo="proj", kind="project"),  # projects skipped
    ])
    monkeypatch.setattr(family_graph.dispatch, "resolve_repo",
                        lambda mgr, m, runs: (None, "unresolved"))
    entries = family_work_repos(_Mgr(), fam,
                                exclude=frozenset({"steward-home"}))
    assert entries == [{"repo_ref": "web", "mission_key": "T-1"},
                       {"repo_ref": "api", "mission_key": "T-4"}]


def test_family_work_repos_unpacks_resolve_repo_tuple(monkeypatch):
    """dispatch.resolve_repo returns (name, reason). A truthy tuple must
    not be passed to blocker_read_credential as a repo name — the harvest
    kick path never stamps m.repo."""
    seen = []

    def cred(mgr, name):
        seen.append(name)
        return ("configured", None, None, "tok") if name == "web" else None

    monkeypatch.setattr(family_graph, "blocker_read_credential", cred)
    monkeypatch.setattr(family_graph.dispatch, "resolve_repo",
                        lambda mgr, m, runs: ("web", None))

    class _Store:
        def all(self):
            return []

    class _Mgr:
        class runs:
            store = _Store()

    entries = family_work_repos(
        _Mgr(), Family(members=[_m("a", "T-1", repo=None)]))
    assert seen == ["web"], seen
    assert entries == [{"repo_ref": "web", "mission_key": "T-1"}]


def test_family_work_repos_honors_the_clone_cap(monkeypatch):
    monkeypatch.setattr(family_graph, "blocker_read_credential",
                        lambda mgr, name: ("configured", None, None, "tok"))

    class _Store:
        def all(self):
            return []

    class _Mgr:
        class runs:
            store = _Store()

    members = [_m(f"m{i}", f"T-{i}", repo=f"r{i}") for i in range(12)]
    entries = family_work_repos(_Mgr(), Family(members=members))
    from devcake.domain.orchestrator.dispatch import MAX_BLOCKER_WORK_EXTRAS
    assert len(entries) == MAX_BLOCKER_WORK_EXTRAS
