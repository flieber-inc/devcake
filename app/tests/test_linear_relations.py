"""ADR-0007 Linear adapter contract (hermetic, MockTransport — battery rows
11/12 of docs/05 §7): inverseRelations→blocked_by parsing and the
issueRelationCreate payload, duplicate tolerance included."""
import asyncio
import json

import httpx

from devcake.adapters.linear.adapter import LinearAdapter

ISSUE = {
    "id": "uuid-b", "identifier": "T-2", "title": "implement",
    "description": "", "url": "https://linear.app/x", "priority": 2,
    "updatedAt": "2026-07-12T00:00:00Z",
    "state": {"name": "Backlog", "type": "backlog"},
    "labels": {"nodes": [{"name": "DEVCAKE"}]},
    "project": None,
    "inverseRelations": {"nodes": [
        {"type": "blocks", "issue": {"id": "uuid-a"}},      # blocker
        {"type": "related", "issue": {"id": "uuid-c"}},     # ignored
        {"type": "blocks", "issue": None},                  # dangling — ignored
    ]},
}


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_inverse_relations_parse_into_blocked_by():
    mock = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": {"issue": ISSUE}}))
    pmo = LinearAdapter("fake-key", transport=mock)
    mission = run_coro(pmo.get_mission("uuid-b"))
    assert mission.blocked_by == ["uuid-a"]


def test_list_all_paginates_issue_pages():
    seen_cursors = []

    def handler(req):
        body = json.loads(req.content)
        q, variables = body["query"], body.get("variables", {})
        if "teams(" in q:
            return httpx.Response(200, json={"data": {"teams": {"nodes": [
                {"id": "tid", "key": "T", "states": {"nodes": []},
                 "labels": {"nodes": []}}]}}})
        if "issues(" in q:
            after = variables.get("after")
            seen_cursors.append(after)
            if after is None:
                return httpx.Response(200, json={"data": {"issues": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    "nodes": [ISSUE]}}})
            return httpx.Response(200, json={"data": {"issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{**ISSUE, "id": "uuid-c", "identifier": "T-3"}]}}})
        return httpx.Response(200, json={"data": {"projects": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}})

    pmo = LinearAdapter("fake-key", transport=httpx.MockTransport(handler))
    missions = run_coro(pmo.list_all("T"))
    assert [m.key for m in missions] == ["T-2", "T-3"]   # both pages merged
    assert seen_cursors == [None, "c1"]                  # cursor threaded through


def test_full_relations_page_warns(caplog):
    import logging

    from devcake.adapters.linear.adapter import RELATIONS_PAGE
    issue = {**ISSUE, "inverseRelations": {"nodes": [
        {"type": "related", "issue": {"id": f"r{i}"}}
        for i in range(RELATIONS_PAGE)]}}
    mock = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": {"issue": issue}}))
    pmo = LinearAdapter("fake-key", transport=mock)
    with caplog.at_level(logging.WARNING, logger="devcake.linear"):
        mission = run_coro(pmo.get_mission("uuid-b"))
    assert mission.blocked_by == []                      # blocks evicted by the page
    assert any("inverseRelations page is full" in r.message
               for r in caplog.records)                  # …but never silently


def test_create_relation_payload_and_duplicate_tolerance():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json={"data": {"issueRelationCreate":
                                                      {"success": True}}})
        return httpx.Response(200, json={"errors": [
            {"message": "issue relation already exists"}]})

    pmo = LinearAdapter("fake-key", transport=httpx.MockTransport(handler))
    run_coro(pmo.create_relation("uuid-a", "uuid-b"))
    assert seen[0]["variables"] == {"a": "uuid-a", "b": "uuid-b"}
    assert "type: blocks" in seen[0]["query"]
    run_coro(pmo.create_relation("uuid-a", "uuid-b"))   # duplicate → no raise
    assert len(seen) == 2
