"""GiteaIssuesAdapter PMOPort conformance (offline MockTransport)."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone

import httpx
import pytest

from devcake.adapters.gitea_issues.adapter import GiteaIssuesAdapter
from devcake.adapters.gitea_issues.mapping import CANCEL_FOOTER
from devcake.domain.model import ALL_LABELS, MissionRef
from devcake.ports.pmo import PMOPort, PMOTransient

PORT_METHODS = [n for n, v in vars(PMOPort).items()
                if callable(v) and not n.startswith("_")]


def _params(fn):
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_gitea_issues_implements_full_port():
    for name in PORT_METHODS:
        impl = getattr(GiteaIssuesAdapter, name, None)
        assert impl is not None, f"missing {name}"
        assert _params(impl) == _params(getattr(PMOPort, name)), name


def _issue(number=1, state="open", body="", labels=None, title="t"):
    return {
        "id": 1000 + number,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "html_url": f"http://gitea/o/r/issues/{number}",
        "updated_at": "2026-07-20T00:00:00Z",
        "labels": labels or [],
        "pull_request": None,
    }


class Router:
    """Minimal Gitea API mock for contract tests."""

    def __init__(self):
        self.issues = {
            1: _issue(1, labels=[{"id": 1, "name": "DEVCAKE"}]),
            2: _issue(2, body="blocked"),
        }
        self.labels = {
            "DEVCAKE": {"id": 1, "name": "DEVCAKE", "color": "000000"},
        }
        self.comments: dict[int, list] = {1: [], 2: []}
        self.deps: dict[int, list[int]] = {1: [], 2: []}  # blocked → [blockers]
        self.next_label = 10
        self.next_comment = 1
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = urlsplit_path(req.url.path)
        method = req.method.upper()
        self.calls.append(f"{method} {path}")
        body = {}
        if req.content:
            try:
                body = json.loads(req.content)
            except json.JSONDecodeError:
                body = {}

        # PATCH repo (enable dependencies)
        if method == "PATCH" and path == "/api/v1/repos/o/r":
            return httpx.Response(200, json={"name": "r"})

        # labels collection
        if path == "/api/v1/repos/o/r/labels":
            if method == "GET":
                return httpx.Response(200, json=list(self.labels.values()))
            if method == "POST":
                name = (body.get("name") or "").upper()
                self.next_label += 1
                lb = {"id": self.next_label, "name": name, "color": "6e40c9"}
                self.labels[name] = lb
                return httpx.Response(201, json=lb)

        # issues collection
        if path == "/api/v1/repos/o/r/issues":
            if method == "GET":
                state = req.url.params.get("state", "all")
                items = list(self.issues.values())
                if state == "open":
                    items = [i for i in items if i["state"] == "open"]
                return httpx.Response(200, json=items)
            if method == "POST":
                n = max(self.issues) + 1
                labs = []
                for lid in body.get("labels") or []:
                    for lb in self.labels.values():
                        if lb["id"] == lid:
                            labs.append(lb)
                iss = _issue(n, body=body.get("body") or "",
                             labels=labs, title=body.get("title") or "")
                self.issues[n] = iss
                self.comments[n] = []
                self.deps[n] = []
                return httpx.Response(201, json=iss)

        # issue by number
        m = match_issue(path)
        if m:
            num = int(m.group(1))
            rest = m.group(2) or ""
            if rest == "" and method == "GET":
                return httpx.Response(200, json=self.issues[num])
            if rest == "" and method == "PATCH":
                iss = self.issues[num]
                if "state" in body:
                    iss["state"] = body["state"]
                if "body" in body:
                    iss["body"] = body["body"]
                return httpx.Response(200, json=iss)
            if rest == "/comments" and method == "GET":
                return httpx.Response(200, json=self.comments.get(num, []))
            if rest == "/comments" and method == "POST":
                c = {
                    "id": self.next_comment,
                    "body": body.get("body") or "",
                    "created_at": "2026-07-20T12:00:00Z",
                    "user": {"login": "bot"},
                    "assets": [],
                }
                self.next_comment += 1
                self.comments.setdefault(num, []).append(c)
                return httpx.Response(201, json=c)
            if rest == "/labels" and method == "PUT":
                labs = []
                for lid in body.get("labels") or []:
                    for lb in self.labels.values():
                        if lb["id"] == lid:
                            labs.append(lb)
                self.issues[num]["labels"] = labs
                return httpx.Response(200, json=labs)
            if rest == "/dependencies" and method == "GET":
                blockers = self.deps.get(num, [])
                return httpx.Response(
                    200, json=[self.issues[b] for b in blockers if b in self.issues])
            if rest == "/dependencies" and method == "POST":
                blocker = int(body.get("index"))
                if blocker in self.deps.get(num, []):
                    return httpx.Response(
                        500, json={"message": "issue dependency does already exist "
                                   f"[issue id: {num}, dependency id: {blocker}]"})
                self.deps.setdefault(num, []).append(blocker)
                return httpx.Response(201, json=self.issues[num])
            if rest == "/dependencies" and method == "DELETE":
                blocker = int(body.get("index"))
                if blocker in self.deps.get(num, []):
                    self.deps[num] = [x for x in self.deps[num] if x != blocker]
                return httpx.Response(200, json=self.issues[num])
            if rest == "/assets" and method == "GET":
                return httpx.Response(200, json=[])

        if method == "GET" and path.startswith("/attachments/"):
            return httpx.Response(200, content=b"asset-bytes")

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})


def urlsplit_path(path: str) -> str:
    return path


def match_issue(path: str):
    import re
    return re.match(r"^/api/v1/repos/o/r/issues/(\d+)(.*)$", path)


def make_pmo(router: Router | None = None) -> GiteaIssuesAdapter:
    r = router or Router()
    return GiteaIssuesAdapter(
        "http://gitea", "tok", "o/r", instance="gitea",
        transport=httpx.MockTransport(r.handler))


def test_list_and_get_normalize_open_to_backlog():
    pmo = make_pmo()
    missions = run(pmo.list_all("o/r"))
    assert all(m.pmo_kind == "issue" for m in missions)
    m = run(pmo.get(MissionRef("1", "issue")))
    assert m.status == "backlog"
    assert m.key == "o/r#1"
    assert m.pmo_id == "1"
    assert m.instance == "gitea"


def test_closed_with_cancel_footer_is_canceled():
    r = Router()
    r.issues[1] = _issue(1, state="closed", body=f"x\n{CANCEL_FOOTER}\n")
    pmo = make_pmo(r)
    m = run(pmo.get(MissionRef("1", "issue")))
    assert m.status == "canceled"


def test_set_status_done_closes():
    r = Router()
    pmo = make_pmo(r)
    run(pmo.set_status(MissionRef("1", "issue"), "done"))
    assert r.issues[1]["state"] == "closed"
    m = run(pmo.get(MissionRef("1", "issue")))
    assert m.status == "done"


def test_cancel_mission_idempotent():
    r = Router()
    pmo = make_pmo(r)
    run(pmo.cancel_mission(MissionRef("1", "issue")))
    assert r.issues[1]["state"] == "closed"
    assert CANCEL_FOOTER in r.issues[1]["body"]
    run(pmo.cancel_mission(MissionRef("1", "issue")))  # no raise


def test_swap_labels_put_replace():
    r = Router()
    r.labels["DEVCAKE-PLAN"] = {"id": 2, "name": "DEVCAKE-PLAN", "color": "111"}
    pmo = make_pmo(r)
    run(pmo.swap_labels(MissionRef("1", "issue"),
                        remove=set(), add={"DEVCAKE-PLAN"}))
    names = {lb["name"] for lb in r.issues[1]["labels"]}
    assert "DEVCAKE" in names and "DEVCAKE-PLAN" in names
    run(pmo.swap_labels(MissionRef("1", "issue"),
                        remove={"DEVCAKE-PLAN"}, add={"DEVCAKE-EXECUTE"}))
    # EXECUTE label created by ensure path
    names = {lb["name"] for lb in r.issues[1]["labels"]}
    assert "DEVCAKE-PLAN" not in names
    assert "DEVCAKE" in names


def test_create_relation_and_blocked_by():
    r = Router()
    pmo = make_pmo(r)
    run(pmo.create_relation("1", "2"))  # 1 blocks 2
    m2 = run(pmo.get(MissionRef("2", "issue")))
    assert m2.blocked_by == ["1"]
    run(pmo.create_relation("1", "2"))  # duplicate tolerant


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


def test_health_probe_counts_managed_labels_only():
    r = Router()
    r.labels["CUSTOM-EXTRA"] = {"id": 99, "name": "CUSTOM-EXTRA", "color": "fff"}
    pmo = make_pmo(r)
    h = run(pmo.health_probe("o/r"))
    assert h.ok
    assert h.workspace == "o/r"
    assert h.managed_labels_expected == len(ALL_LABELS)
    # ensure created all managed; CUSTOM-EXTRA must not inflate present beyond managed ∩ remote
    assert h.managed_labels_present == len(ALL_LABELS)


def test_capabilities_issue_only():
    caps = make_pmo().capabilities()
    assert caps.projects_supported is False
    assert caps.relations_supported is True
    assert caps.native_label_swap_atomic is True


def test_project_ref_get_raises():
    pmo = make_pmo()
    with pytest.raises(RuntimeError, match="projects"):
        run(pmo.get(MissionRef("1", "project")))


def test_429_is_pmo_transient():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    pmo = GiteaIssuesAdapter(
        "http://gitea", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    with pytest.raises(PMOTransient):
        run(pmo.get(MissionRef("1", "issue")))


def test_app_reachable_url_rewrites_root_url_host():
    """Bundled Gitea ROOT_URL is localhost:3300; app must fetch via api_base."""
    pmo = GiteaIssuesAdapter("http://gitea:3000", "tok", "o/r")
    assert pmo._app_reachable_url(
        "http://localhost:3300/attachments/abc") == (
        "http://gitea:3000/attachments/abc")
    assert pmo._app_reachable_url(
        "http://gitea:3000/attachments/abc") == (
        "http://gitea:3000/attachments/abc")
    # Foreign hosts must NOT collapse onto the origin (allowlist would be vacuous)
    assert pmo._app_reachable_url(
        "https://evil.example/steal") == "https://evil.example/steal"


def test_download_asset_refuses_evil_host():
    """Allowlist runs before rewrite — ticket SSRF hosts are refused."""
    pmo = GiteaIssuesAdapter("http://gitea:3000", "tok", "o/r",
                             transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="refused"):
        run(pmo.download_asset("https://evil.example/secret"))


def test_download_asset_refuses_evil_redirect():
    """Off-allowlist Location must not be followed with the Gitea token."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/good"):
            return httpx.Response(
                302, headers={"Location": "https://evil.example/exfil"})
        return httpx.Response(200, content=b"should-not-reach")

    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="redirect refused"):
        run(pmo.download_asset("http://localhost:3300/attachments/good"))
    assert len(seen) == 1
    assert "evil.example" not in seen[0]
    assert "gitea:3000" in seen[0]


def test_download_asset_allows_same_host_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(
                302, headers={"Location": "/attachments/final"})
        return httpx.Response(200, content=b"payload-bytes")

    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    body = run(pmo.download_asset("http://gitea:3000/attachments/start"))
    assert body == b"payload-bytes"
