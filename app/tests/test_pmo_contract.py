"""PMOPort conformance (docs/05): the LinearAdapter must implement the FULL
port with matching signatures, test fakes must not drift on the subset they
implement, and the unified MissionRef methods must dispatch issue vs project
to the right vendor operations (asserted against canned GraphQL)."""

import asyncio
import inspect
import json

import httpx
import pytest

from devcake.adapters.linear.adapter import LinearAdapter
from devcake.domain.model import ALL_LABELS, MissionRef
from devcake.ports.pmo import PMOHealth, PMOPort, PMOTransient

PORT_METHODS = [n for n, v in vars(PMOPort).items()
                if callable(v) and not n.startswith("_")]


def _params(fn):
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def test_port_declares_expected_surface():
    # the authoritative contract — a port edit must be deliberate (docs/05 §1)
    assert sorted(PORT_METHODS) == sorted([
        "list_missions", "list_all", "get", "get_activity", "children_of",
        "post_feed", "set_status", "cancel_mission", "swap_labels", "create_mission",
        "create_relation", "ensure_labels", "append_description",
        "upload_attachment", "download_asset", "health_probe", "capabilities"])


def test_linear_adapter_implements_full_port():
    for name in PORT_METHODS:
        impl = getattr(LinearAdapter, name, None)
        assert impl is not None, f"LinearAdapter missing port method {name}"
        assert _params(impl) == _params(getattr(PMOPort, name)), \
            f"LinearAdapter.{name} signature drifted from PMOPort"


def test_fakes_do_not_drift_from_port():
    """Every port method a test fake implements must match the port signature
    — keeps FakePMO/MapPMO/DepPMO honest as the contract evolves."""
    from test_transitions import FakePMO
    from test_steward import MapPMO
    from test_dependencies import DepPMO
    for fake in (FakePMO, MapPMO, DepPMO):
        for name in PORT_METHODS:
            impl = getattr(fake, name, None)
            if impl is None:
                continue  # fakes implement only what their tests exercise
            port_params = _params(getattr(PMOPort, name))
            fake_params = _params(impl)
            assert fake_params == port_params, \
                f"{fake.__name__}.{name}{fake_params} drifted from port {port_params}"


# ── unified dispatch against canned GraphQL ──────────────────────────────────

ISSUE_NODE = {
    "id": "uuid-i1", "identifier": "DEV-1", "title": "t", "description": "",
    "url": "", "updatedAt": "2026-07-13T00:00:00Z", "priority": 0,
    "state": {"name": "Todo", "type": "backlog"},
    "labels": {"nodes": [{"name": "DEVCAKE"}]},
    "project": None, "inverseRelations": {"nodes": []},
}
PROJECT_NODE = {
    "id": "uuid-p1", "name": "Proj", "description": "", "content": "",
    "url": "", "updatedAt": "2026-07-13T00:00:00Z", "priority": 0,
    "status": {"name": "Backlog", "type": "backlog"},
    "labels": {"nodes": [{"name": "DEVCAKE"}]},
}


class Recorder:
    def __init__(self, responses):
        self.queries = []
        self.responses = responses

    def handler(self, req):
        body = json.loads(req.content)
        self.queries.append(body["query"])
        for marker, data in self.responses.items():
            if marker in body["query"]:
                return httpx.Response(200, json={"data": data})
        return httpx.Response(200, json={"data": {}})


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_get_dispatches_on_kind():
    rec = Recorder({"issue(": {"issue": ISSUE_NODE},
                    "project(": {"project": PROJECT_NODE}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(rec.handler))
    m = run(pmo.get(MissionRef("uuid-i1", "issue")))
    assert m.pmo_kind == "issue" and m.key == "DEV-1"
    m = run(pmo.get(MissionRef("uuid-p1", "project")))
    assert m.pmo_kind == "project" and m.key.startswith("PRJ-")


def test_post_feed_dispatches_on_kind():
    rec = Recorder({"commentCreate": {"commentCreate": {"success": True}},
                    "projectUpdateCreate": {"projectUpdateCreate": {"success": True}}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(rec.handler))
    run(pmo.post_feed(MissionRef("uuid-i1", "issue"), "hi"))
    run(pmo.post_feed(MissionRef("uuid-p1", "project"), "hi"))
    assert any("commentCreate" in q for q in rec.queries)
    assert any("projectUpdateCreate" in q for q in rec.queries)


def test_append_description_reads_then_appends():
    """ADR-0012 lineage note: append-only read-modify-write on the issue
    description; project refs are refused loudly (no v0 caller, and Linear
    caps project `description` at 255 chars)."""
    issue = dict(ISSUE_NODE, description="body")
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "issueUpdate" in b["query"]:
            return httpx.Response(200, json={"data": {"issueUpdate": {"success": True}}})
        return httpx.Response(200, json={"data": {"issue": issue}})

    pmo = LinearAdapter("k", transport=httpx.MockTransport(handler))
    run(pmo.append_description(MissionRef("uuid-i1", "issue"), "\n\n_note_"))
    update = next(b for b in bodies if "issueUpdate" in b["query"])
    assert update["variables"]["description"] == "body\n\n_note_"
    with pytest.raises(ValueError):
        run(pmo.append_description(MissionRef("uuid-p1", "project"), "x"))


def test_get_activity_on_project_returns_empty_entries():
    rec = Recorder({"project(": {"project": PROJECT_NODE}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(rec.handler))
    act = run(pmo.get_activity(MissionRef("uuid-p1", "project")))
    assert act.entries == [] and act.mission.pmo_kind == "project"
    assert not any("comments" in q for q in rec.queries)


def test_get_activity_names_attachments():
    issue = dict(ISSUE_NODE)
    issue["comments"] = {"pageInfo": {"hasNextPage": False, "endCursor": None},
                         "nodes": [{
                             "body": "report: [r.md](https://uploads.linear.app/x/r)"
                                     " and bare https://uploads.linear.app/y/s",
                             "createdAt": "2026-07-13T00:00:00Z",
                             "user": {"name": "dev"}}]}
    rec = Recorder({"issue(": {"issue": issue}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(rec.handler))
    act = run(pmo.get_activity(MissionRef("uuid-i1", "issue")))
    atts = act.entries[0].attachments
    by_url = {a.url: a.name for a in atts}
    assert by_url["https://uploads.linear.app/x/r"] == "r.md"   # named link
    assert by_url["https://uploads.linear.app/y/s"] is None     # bare URL


def test_health_probe_counts_managed_labels():
    team = {"id": "t1", "key": "DEV", "states": {"nodes": []}}
    labels = {"nodes": [{"id": "1", "name": "DEVCAKE"},
                        {"id": "2", "name": "DEVCAKE-PLAN"},
                        {"id": "3", "name": "DEVCAKE-CUSTOM-EXTRA"},
                        {"id": "4", "name": "unrelated"}],
              "pageInfo": {"hasNextPage": False}}
    # labels ride the split single-team query (D1 complexity fix)
    rec = Recorder({"team(id": {"team": {"labels": labels}},
                    "teams(": {"teams": {"nodes": [team]}}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(rec.handler))
    h = run(pmo.health_probe("DEV"))
    assert isinstance(h, PMOHealth) and h.ok and h.workspace == "DEV"
    # DEVCAKE-CUSTOM-EXTRA is DEVCAKE-prefixed but NOT managed — must not count
    assert h.managed_labels_present == 2
    assert h.managed_labels_expected == len(ALL_LABELS)


def test_transient_errors_typed_from_port():
    mock = httpx.MockTransport(lambda req: httpx.Response(429, json={}))
    pmo = LinearAdapter("k", transport=mock)
    with pytest.raises(PMOTransient):
        run(pmo.get(MissionRef("x", "issue")))


def test_get_activity_full_on_project_mirrors_native_feed():
    # project-fidelity fix: FULL mode on a project ref pays the enrichment
    # queries (documents/updates/links); shallow stays pinned cheap by
    # test_get_activity_on_project_returns_empty_entries above
    empty = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    upd = {"id": "u1", "body": "hello", "createdAt": "2026-07-13T00:00:00Z",
           "user": {"name": "alice"}, "comments": dict(empty)}
    rec = Recorder({
        "documents(first:": {"project": {
            "documents": {**empty, "nodes": [
                {"id": "d1", "title": "Spec", "content": "# s", "url": "u"}]},
            "projectUpdates": {**empty, "nodes": [upd]},
            "externalLinks": dict(empty), "attachments": dict(empty)}},
        "project(": {"project": PROJECT_NODE}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(rec.handler))
    act = run(pmo.get_activity(MissionRef("uuid-p1", "project"), full=True))
    assert [d.title for d in act.documents] == ["Spec"]
    assert [e.entry_id for e in act.entries] == ["u1"]
    assert act.entries[0].author == "alice (project update)"
    assert any("projectUpdates" in q for q in rec.queries)
