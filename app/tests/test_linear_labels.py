"""ISSUES #7 regression + the CAKE-48 clobber: label reads paginate past a
full first page; the issue swap path uses per-label mutations (never a
full-set rewrite, so a concurrent writer's labels survive); the project path
still rewrites and must never do so from a truncated read."""
import asyncio

import pytest

from devcake.adapters.linear.adapter import (LABELS_PAGE, MAX_LABEL_PAGES,
                                             LinearAdapter)
from devcake.domain.model import MissionRef


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _label(i):
    return {"id": f"lid-{i}", "name": f"L{i}"}


def _labels_conn(labels, start, page=LABELS_PAGE):
    chunk = labels[start:start + page]
    return {"pageInfo": {"hasNextPage": start + page < len(labels),
                         "endCursor": str(start + page)},
            "nodes": chunk}


def _swap_adapter(total, mutations, extra_team_labels=()):
    """Issue i1 carrying `total` labels served in 50-label cursor pages."""
    ad = LinearAdapter("key")
    labels = [_label(i) for i in range(total)]

    async def _gql(query, variables=None):
        v = dict(variables or {})
        if "issueUpdate" in query:
            raise AssertionError(
                "full-set rewrite is banned on the issue path (CAKE-48)")
        if "issueRemoveLabel" in query:
            mutations.append(("remove", v["lid"]))
            return {"issueRemoveLabel": {"success": True}}
        if "issueAddLabel" in query:
            mutations.append(("add", v["lid"]))
            return {"issueAddLabel": {"success": True}}
        start = int(v["after"]) if v.get("after") else 0
        issue = {"labels": _labels_conn(labels, start)}
        if not v.get("after"):
            issue["team"] = {"key": "T"}
        return {"issue": issue}

    async def _team(_key):
        team_labels = labels + [{"id": f"tid-{n}", "name": n}
                                for n in extra_team_labels]
        return {"id": "team-1", "key": "T", "labels": {"nodes": team_labels}}

    ad._gql = _gql
    ad._team = _team
    return ad


def test_swap_issue_labels_is_per_label_and_paginates_removal_ids():
    """55 labels: the swap issues exactly one remove and one add mutation —
    no issueUpdate(labelIds) rewrite exists to clobber concurrent writers —
    and the removal id (L54, overflow page) proves the read paginated."""
    mutations = []
    ad = _swap_adapter(55, mutations, extra_team_labels=["DEVCAKE-REVIEW"])
    run_coro(ad._swap_issue_labels("i1", remove={"L54"}, add={"DEVCAKE-REVIEW"}))
    assert mutations == [("remove", "lid-54"), ("add", "tid-DEVCAKE-REVIEW")]


def test_swap_issue_labels_skips_absent_removal_and_present_add():
    """Absent removal is a no-op; adding a label already on the issue issues
    no mutation (the read steers, it never rewrites)."""
    mutations = []
    ad = _swap_adapter(3, mutations)
    run_coro(ad._swap_issue_labels("i1", remove={"NOPE"}, add={"L1"}))
    assert mutations == []


def test_swap_issue_labels_refuses_past_ceiling():
    """Past the fail-loud ceiling the swap must raise before mutating —
    a removal cannot be verified against a truncated read."""
    mutations = []
    ad = _swap_adapter(LABELS_PAGE * MAX_LABEL_PAGES + 1, mutations)
    with pytest.raises(RuntimeError, match="refusing label swap"):
        run_coro(ad._swap_issue_labels("i1", remove=set(), add=set()))
    assert mutations == []                   # nothing was mutated


def _team_gql(labels):
    """A _gql fake serving the SPLIT queries: a labels-free team shell via
    teams(filter…) — nesting a paginated labels connection there blew
    Linear's ~10k complexity budget ('Query too complex' 15560, live) —
    plus team(id…) cursor pages for the labels."""
    async def _gql(query, variables=None):
        v = dict(variables or {})
        if "teams(filter" in query:
            assert "labels" not in query, "labels must not nest under teams(filter)"
            return {"teams": {"nodes": [{
                "id": "team-1", "key": "DEV", "states": {"nodes": []}}]}}
        if "team(id" in query:
            start = int(v["after"]) if v.get("after") else 0
            return {"team": {"labels": _labels_conn(labels, start, page=100)}}
        raise AssertionError(f"unexpected query: {query}")
    return _gql


def test_team_labels_paginate_past_first_page():
    """Audit A12: _team cached only labels(first:100) — a managed label on
    page 2 was invisible to every cache consumer (ensure_labels re-created
    it, create_mission KeyError'd, swaps dropped it)."""
    ad = LinearAdapter("key")
    labels = [_label(i) for i in range(150)] + [{"id": "dv", "name": "DEVCAKE"}]
    ad._gql = _team_gql(labels)
    team = run_coro(ad._team("DEV"))
    names = {l["name"] for l in team["labels"]["nodes"]}
    assert "DEVCAKE" in names and len(team["labels"]["nodes"]) == 151


def test_team_labels_past_ceiling_fail_loud():
    ad = LinearAdapter("key")
    ad._gql = _team_gql([_label(i) for i in range(100 * MAX_LABEL_PAGES + 1)])
    with pytest.raises(RuntimeError, match="refusing"):
        run_coro(ad._team("DEV"))


def test_ensure_labels_project_half_paginates():
    """ensure_labels read projectLabels(first:100) with no cursor — a managed
    project label past 100 was invisibly 'missing' and re-created (API error
    on the duplicate) every boot (audit A12)."""
    ad = LinearAdapter("key")
    plabels = [_label(i) for i in range(100)] + [{"id": "pv", "name": "DEVCAKE"}]
    created = []

    async def _gql(query, variables=None):
        v = dict(variables or {})
        if "projectLabelCreate" in query or "issueLabelCreate" in query:
            created.append(v["name"])
            return {"projectLabelCreate": {"success": True},
                    "issueLabelCreate": {"success": True}}
        if "projectLabels" in query:
            start = int(v["after"]) if v.get("after") else 0
            return {"projectLabels": _labels_conn(plabels, start, page=100)}
        raise AssertionError(f"unexpected query: {query}")

    async def _team(_key):
        return {"id": "team-1", "key": "DEV",
                "labels": {"nodes": [{"id": "x", "name": "DEVCAKE"}]}}

    ad._gql = _gql
    ad._team = _team
    run_coro(ad.ensure_labels("DEV", names={"DEVCAKE"}))
    assert created == []              # the page-2 project label was SEEN


def test_all_project_labels_past_ceiling_fail_loud():
    """Silent truncation would make a DEVCAKE-* label past the ceiling
    unresolvable with no signal (audit A29)."""
    ad = LinearAdapter("key")
    plabels = [_label(i) for i in range(100 * MAX_LABEL_PAGES + 1)]

    async def _gql(query, variables=None):
        v = dict(variables or {})
        start = int(v["after"]) if v.get("after") else 0
        return {"projectLabels": _labels_conn(plabels, start, page=100)}

    ad._gql = _gql
    with pytest.raises(RuntimeError, match="refusing"):
        run_coro(ad._all_project_labels())


def test_get_issue_paginates_labels():
    """Read path: DEVCAKE-SKIP hiding on page 2 must be seen (ISSUES #7)."""
    ad = LinearAdapter("key")
    labels = [_label(i) for i in range(50)] + [{"id": "s", "name": "DEVCAKE-SKIP"}]

    async def _gql(query, variables=None):
        v = dict(variables or {})
        start = int(v["after"]) if v.get("after") else 0
        issue = {"id": "i1", "identifier": "T-1", "title": "t", "description": "",
                 "url": "", "updatedAt": "2026-07-12T10:00:00.000Z", "priority": 2,
                 "state": {"name": "In Progress", "type": "started"},
                 "project": None, "inverseRelations": {"nodes": []},
                 "labels": _labels_conn(labels, start)}
        return {"issue": issue}

    ad._gql = _gql
    mission = run_coro(ad.get(MissionRef("i1", "issue")))
    assert "DEVCAKE-SKIP" in mission.labels


def test_fully_walked_large_label_set_does_not_warn(caplog):
    """A complete walk over 60 labels is NOT truncation — no wolf-crying."""
    ad = LinearAdapter("key")
    labels = [_label(i) for i in range(60)]

    async def _gql(query, variables=None):
        v = dict(variables or {})
        start = int(v["after"]) if v.get("after") else 0
        issue = {"id": "i1", "identifier": "T-1", "title": "t", "description": "",
                 "url": "", "updatedAt": "2026-07-12T10:00:00.000Z", "priority": 2,
                 "state": {"name": "In Progress", "type": "started"},
                 "project": None, "inverseRelations": {"nodes": []},
                 "labels": _labels_conn(labels, start)}
        return {"issue": issue}

    ad._gql = _gql
    with caplog.at_level("WARNING", logger="devcake.linear"):
        mission = run_coro(ad.get(MissionRef("i1", "issue")))
    assert len(mission.labels) == 60
    assert not any("truncated" in r.message for r in caplog.records)
