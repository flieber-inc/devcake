"""ISSUES #7 regression: label reads paginate past a full first page, and the
label-swap WRITE paths (read-modify-write over the full set) must never rewrite
from a truncated read — that silently deletes the overflow labels."""
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
            mutations.append(v)
            return {"issueUpdate": {"success": True}}
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


def test_swap_issue_labels_paginates_before_rewrite():
    """55 labels: the rewrite must carry all of them (minus removed, plus
    added) — pre-fix it read only the first 50 and deleted the overflow."""
    mutations = []
    ad = _swap_adapter(55, mutations, extra_team_labels=["DEVCAKE-REVIEW"])
    run_coro(ad._swap_issue_labels("i1", remove={"L3"}, add={"DEVCAKE-REVIEW"}))
    assert len(mutations) == 1
    ids = set(mutations[0]["labelIds"])
    assert len(ids) == 55                    # 55 - 1 removed + 1 added
    assert "lid-54" in ids                   # overflow label survived
    assert "lid-3" not in ids                # removed
    assert "tid-DEVCAKE-REVIEW" in ids       # added


def test_swap_issue_labels_refuses_past_ceiling():
    """Past the fail-loud ceiling the swap must raise, not delete labels."""
    mutations = []
    ad = _swap_adapter(LABELS_PAGE * MAX_LABEL_PAGES + 1, mutations)
    with pytest.raises(RuntimeError, match="refusing label swap"):
        run_coro(ad._swap_issue_labels("i1", remove=set(), add=set()))
    assert mutations == []                   # nothing was rewritten


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
