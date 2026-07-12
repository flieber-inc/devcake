"""ADR-0007 Linear adapter contract (hermetic, MockTransport — battery rows
11/12 of docs/05 §7): inverseRelations→blocked_by parsing and the
issueRelationCreate payload, duplicate tolerance included."""
import asyncio
import json

import httpx

from devcake.linear import LinearAdapter

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
