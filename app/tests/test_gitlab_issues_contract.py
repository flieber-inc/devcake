"""GitLabIssuesAdapter PMOPort conformance (offline MockTransport)."""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import httpx
import pytest

from devcake.adapters.gitlab_issues.adapter import GitLabIssuesAdapter
from devcake.adapters.gitlab_issues.mapping import CANCEL_FOOTER
from devcake.domain.model import ALL_LABELS, MissionRef
from devcake.ports.pmo import PMOPort, PMOTransient

PORT_METHODS = [n for n, v in vars(PMOPort).items()
                if callable(v) and not n.startswith("_")]


def _params(fn):
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_gitlab_issues_implements_full_port():
    for name in PORT_METHODS:
        impl = getattr(GitLabIssuesAdapter, name, None)
        assert impl is not None, f"missing {name}"
        assert _params(impl) == _params(getattr(PMOPort, name)), name


def _issue(iid=1, state="opened", description="", labels=None, title="t"):
    return {
        "id": 9000 + iid,
        "iid": iid,
        "title": title,
        "description": description,
        "state": state,
        "web_url": f"https://gitlab.com/o/r/-/issues/{iid}",
        "updated_at": "2026-08-15T00:00:00Z",
        "labels": labels or [],
    }


class Router:
    def __init__(self):
        self.issues = {
            1: _issue(1, labels=["DEVCAKE"]),
            2: _issue(2, description="blocked"),
        }
        self.labels = {
            "DEVCAKE": {"id": 1, "name": "DEVCAKE", "color": "#000000"},
        }
        self.notes: dict[int, list] = {1: [], 2: []}
        self.uploads: dict[str, bytes] = {}
        self.next_label = 10
        self.next_note = 1
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        # httpx MockTransport decodes %2F; accept both encodings.
        from urllib.parse import unquote
        path = unquote(req.url.path)
        method = req.method.upper()
        self.calls.append(f"{method} {path}")
        body = {}
        if req.content:
            try:
                body = json.loads(req.content)
            except json.JSONDecodeError:
                body = {}

        if path == "/api/v4/projects/o/r":
            return httpx.Response(200, json={"id": 42, "path_with_namespace": "o/r"})

        if path == "/api/v4/projects/o/r/labels":
            if method == "GET":
                page = int(req.url.params.get("page", 1))
                per = int(req.url.params.get("per_page", 50))
                items = list(self.labels.values())
                return httpx.Response(
                    200, json=items[(page - 1) * per: page * per])
            if method == "POST":
                name = (body.get("name") or "").upper()
                self.next_label += 1
                lb = {"id": self.next_label, "name": name, "color": "#6e40c9"}
                self.labels[name] = lb
                return httpx.Response(201, json=lb)

        if path == "/api/v4/projects/o/r/issues":
            if method == "GET":
                state = req.url.params.get("state", "all")
                items = list(self.issues.values())
                if state == "opened":
                    items = [i for i in items if i["state"] == "opened"]
                return httpx.Response(200, json=items)
            if method == "POST":
                n = max(self.issues) + 1
                labs = [x for x in (body.get("labels") or "").split(",") if x]
                iss = _issue(n, description=body.get("description") or "",
                             labels=labs, title=body.get("title") or "")
                self.issues[n] = iss
                self.notes[n] = []
                return httpx.Response(201, json=iss)

        if path == "/api/v4/projects/o/r/uploads" and method == "POST":
            secret = "abc123"
            self.uploads[f"{secret}/plan.md"] = b"# plan"
            return httpx.Response(201, json={
                "url": f"/uploads/{secret}/plan.md",
                "full_path": f"/-/project/42/uploads/{secret}/plan.md",
                "markdown": f"[plan.md](/uploads/{secret}/plan.md)",
            })

        m = re.match(r"^/api/v4/projects/o/r/uploads/([^/]+)/([^/]+)$", path)
        if m and method == "GET":
            blob = self.uploads.get(f"{m.group(1)}/{m.group(2)}")
            if blob is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, content=blob)

        m = re.match(r"^/api/v4/projects/o/r/issues/(\d+)(.*)$", path)
        if m:
            iid = int(m.group(1))
            rest = m.group(2) or ""
            if rest == "" and method == "GET":
                return httpx.Response(200, json=self.issues[iid])
            if rest == "" and method == "PUT":
                iss = self.issues[iid]
                if body.get("state_event") == "close":
                    iss["state"] = "closed"
                if body.get("state_event") == "reopen":
                    iss["state"] = "opened"
                if "description" in body:
                    iss["description"] = body["description"]
                if "labels" in body:
                    iss["labels"] = [x for x in body["labels"].split(",") if x]
                return httpx.Response(200, json=iss)
            if rest == "/notes" and method == "GET":
                return httpx.Response(200, json=self.notes.get(iid, []))
            if rest == "/notes" and method == "POST":
                c = {
                    "id": self.next_note,
                    "body": body.get("body") or "",
                    "created_at": "2026-08-15T12:00:00Z",
                    "author": {"username": "bot"},
                    "system": False,
                }
                self.next_note += 1
                self.notes.setdefault(iid, []).append(c)
                return httpx.Response(201, json=c)
            if rest == "/links" and method == "GET":
                return httpx.Response(200, json=[])
            if rest == "/links" and method == "POST":
                return httpx.Response(
                    403, json={"message":
                               "Blocked issues not available for current license"})

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})


def make_pmo(router: Router | None = None) -> GitLabIssuesAdapter:
    r = router or Router()
    return GitLabIssuesAdapter(
        "https://gitlab.com", "tok", "o/r", instance="gl",
        transport=httpx.MockTransport(r.handler))


def test_list_and_get_normalize_opened_to_backlog():
    pmo = make_pmo()
    missions = run(pmo.list_all("o/r"))
    assert all(m.pmo_kind == "issue" for m in missions)
    m = run(pmo.get(MissionRef("1", "issue")))
    assert m.status == "backlog"
    assert m.key == "o/r#1"
    assert m.pmo_id == "1"
    assert m.instance == "gl"


def test_closed_with_cancel_footer_is_canceled():
    r = Router()
    r.issues[1] = _issue(1, state="closed",
                         description=f"x\n{CANCEL_FOOTER}\n")
    pmo = make_pmo(r)
    m = run(pmo.get(MissionRef("1", "issue")))
    assert m.status == "canceled"


def test_cancel_mission_idempotent():
    r = Router()
    pmo = make_pmo(r)
    run(pmo.cancel_mission(MissionRef("1", "issue")))
    assert r.issues[1]["state"] == "closed"
    assert CANCEL_FOOTER in r.issues[1]["description"]
    run(pmo.cancel_mission(MissionRef("1", "issue")))


def test_post_feed_and_activity_marker_body():
    r = Router()
    pmo = make_pmo(r)
    marker = "step `devcake:v1` ok"
    run(pmo.post_feed(MissionRef("1", "issue"), marker))
    act = run(pmo.get_activity(MissionRef("1", "issue")))
    assert act.entries[-1].body == marker


def test_create_mission_returns_key_and_id():
    r = Router()
    pmo = make_pmo(r)
    key, pid = run(pmo.create_mission(
        "o/r", "child", "desc `devcake:v1`", "high", {"DEVCAKE"}))
    assert pid == "3"
    assert key == "o/r#3"


def test_health_probe_is_read_only():
    r = Router()
    pmo = make_pmo(r)
    h = run(pmo.health_probe("o/r"))
    assert h.ok
    assert h.workspace == "o/r"
    assert h.managed_labels_present == 1
    assert not any(c.startswith(("POST", "PUT", "PATCH")) for c in r.calls)


def test_capabilities_issue_only_no_relations_on_free_tier():
    caps = make_pmo().capabilities()
    assert caps.projects_supported is False
    assert caps.relations_supported is False
    assert caps.global_ids is False
    assert caps.native_label_swap_atomic is True


def test_project_ref_get_raises():
    pmo = make_pmo()
    with pytest.raises(RuntimeError, match="projects"):
        run(pmo.get(MissionRef("1", "project")))


def test_429_is_pmo_transient():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    pmo = GitLabIssuesAdapter(
        "https://gitlab.com", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    with pytest.raises(PMOTransient):
        run(pmo.get(MissionRef("1", "issue")))


def test_upload_and_download_round_trip():
    r = Router()
    pmo = make_pmo(r)
    url = run(pmo.upload_attachment("1", "plan.md", b"# plan"))
    assert "/uploads/abc123/plan.md" in url
    blob = run(pmo.download_asset(url))
    assert blob == b"# plan"


def test_download_asset_refuses_evil_host():
    pmo = make_pmo()
    with pytest.raises(RuntimeError, match="refused"):
        run(pmo.download_asset("https://evil.example/secret"))


def test_create_relation_surfaces_license_error():
    pmo = make_pmo()
    with pytest.raises(RuntimeError, match="403"):
        run(pmo.create_relation("1", "2"))
