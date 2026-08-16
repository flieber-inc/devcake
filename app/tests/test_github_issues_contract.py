"""GitHubIssuesAdapter PMOPort conformance (offline MockTransport)."""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import httpx
import pytest

from devcake.adapters.github_issues.adapter import GitHubIssuesAdapter
from devcake.adapters.github_issues.mapping import CANCEL_FOOTER
from devcake.domain.model import ALL_LABELS, MissionRef
from devcake.ports.pmo import PMOPort, PMOTransient

PORT_METHODS = [n for n, v in vars(PMOPort).items()
                if callable(v) and not n.startswith("_")]


def _params(fn):
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_github_issues_implements_full_port():
    for name in PORT_METHODS:
        impl = getattr(GitHubIssuesAdapter, name, None)
        assert impl is not None, f"missing {name}"
        assert _params(impl) == _params(getattr(PMOPort, name)), name


def _issue(number=1, state="open", body="", labels=None, title="t", prid=None):
    labs = [{"name": n} if isinstance(n, str) else n
            for n in (labels or [])]
    return {
        "id": 5000 + number,
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "html_url": f"https://github.com/o/r/issues/{number}",
        "updated_at": "2026-08-15T00:00:00Z",
        "labels": labs,
        "pull_request": prid,
    }


class Router:
    def __init__(self):
        self.issues = {
            1: _issue(1, labels=["DEVCAKE"]),
            2: _issue(2, body="blocked"),
        }
        self.labels = {
            "DEVCAKE": {"id": 1, "name": "DEVCAKE", "color": "000000"},
        }
        self.comments: dict[int, list] = {1: [], 2: []}
        self.deps: dict[int, list[int]] = {1: [], 2: []}
        self.next_label = 10
        self.next_comment = 1
        self.calls: list[str] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        method = req.method.upper()
        self.calls.append(f"{method} {path}")
        body = {}
        if req.content:
            try:
                body = json.loads(req.content)
            except json.JSONDecodeError:
                body = {}

        if path == "/repos/o/r":
            return httpx.Response(200, json={"full_name": "o/r"})

        if path == "/repos/o/r/labels":
            if method == "GET":
                return httpx.Response(200, json=list(self.labels.values()))
            if method == "POST":
                name = (body.get("name") or "").upper()
                self.next_label += 1
                lb = {"id": self.next_label, "name": name, "color": "6e40c9"}
                self.labels[name] = lb
                return httpx.Response(201, json=lb)

        if path == "/repos/o/r/issues":
            if method == "GET":
                state = req.url.params.get("state", "open")
                items = list(self.issues.values())
                if state == "open":
                    items = [i for i in items if i["state"] == "open"]
                return httpx.Response(200, json=items)
            if method == "POST":
                n = max(self.issues) + 1
                iss = _issue(n, body=body.get("body") or "",
                             labels=body.get("labels") or [],
                             title=body.get("title") or "")
                self.issues[n] = iss
                self.comments[n] = []
                self.deps[n] = []
                return httpx.Response(201, json=iss)

        m = re.match(r"^/repos/o/r/issues/(\d+)(.*)$", path)
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
                c = {"id": self.next_comment, "body": body.get("body") or "",
                     "created_at": "2026-08-15T12:00:00Z",
                     "user": {"login": "bot"}}
                self.next_comment += 1
                self.comments.setdefault(num, []).append(c)
                return httpx.Response(201, json=c)
            if rest == "/labels" and method == "PUT":
                names = body.get("labels") or []
                self.issues[num]["labels"] = [{"name": n} for n in names]
                return httpx.Response(200, json=self.issues[num]["labels"])
            if rest == "/dependencies/blocked_by" and method == "GET":
                blockers = self.deps.get(num, [])
                return httpx.Response(
                    200, json=[self.issues[b] for b in blockers
                               if b in self.issues])
            if rest == "/dependencies/blocked_by" and method == "POST":
                blocker_gid = int(body.get("issue_id"))
                blocker_num = next(
                    n for n, iss in self.issues.items()
                    if iss["id"] == blocker_gid)
                if blocker_num in self.deps.get(num, []):
                    return httpx.Response(
                        422, json={"message": "Validation failed: "
                                   "Target issue has already been taken"})
                self.deps.setdefault(num, []).append(blocker_num)
                return httpx.Response(201, json=self.issues[num])

        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})


def make_pmo(router: Router | None = None) -> GitHubIssuesAdapter:
    r = router or Router()
    return GitHubIssuesAdapter(
        "https://api.github.com", "tok", "o/r", instance="gh",
        transport=httpx.MockTransport(r.handler))


def test_list_and_get_normalize_open_to_backlog():
    m = run(make_pmo().get(MissionRef("1", "issue")))
    assert m.status == "backlog"
    assert m.key == "o/r#1"
    assert m.instance == "gh"


def test_list_filters_pull_requests():
    r = Router()
    r.issues[9] = _issue(9, title="pr", prid={"url": "https://x"})
    missions = run(make_pmo(r).list_all("o/r"))
    assert all(m.pmo_id != "9" for m in missions)


def test_cancel_mission_idempotent():
    r = Router()
    pmo = make_pmo(r)
    run(pmo.cancel_mission(MissionRef("1", "issue")))
    assert r.issues[1]["state"] == "closed"
    assert CANCEL_FOOTER in r.issues[1]["body"]
    run(pmo.cancel_mission(MissionRef("1", "issue")))


def test_post_feed_marker_round_trip():
    pmo = make_pmo()
    marker = "step `devcake:v1` ok"
    run(pmo.post_feed(MissionRef("1", "issue"), marker))
    act = run(pmo.get_activity(MissionRef("1", "issue")))
    assert act.entries[-1].body == marker


def test_create_relation_uses_global_id_and_is_duplicate_tolerant():
    pmo = make_pmo()
    run(pmo.create_relation("1", "2"))
    m2 = run(pmo.get(MissionRef("2", "issue")))
    assert m2.blocked_by == ["1"]
    run(pmo.create_relation("1", "2"))


def test_upload_attachment_refuses():
    pmo = make_pmo()
    with pytest.raises(RuntimeError, match="not supported"):
        run(pmo.upload_attachment("1", "x.md", b"x"))


def test_capabilities_option_b():
    caps = make_pmo().capabilities()
    assert caps.attachments_supported is False
    assert caps.comment_max_chars == 65536
    assert caps.relations_supported is True
    assert caps.projects_supported is False
    assert caps.global_ids is False


def test_health_probe_is_read_only():
    r = Router()
    pmo = make_pmo(r)
    h = run(pmo.health_probe("o/r"))
    assert h.ok
    assert h.managed_labels_present == 1
    assert not any(c.startswith(("POST", "PATCH", "PUT")) for c in r.calls)


def test_429_is_pmo_transient():
    pmo = GitHubIssuesAdapter(
        "https://api.github.com", "tok", "o/r",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, text="slow")))
    with pytest.raises(PMOTransient):
        run(pmo.get(MissionRef("1", "issue")))


def test_403_rate_limit_is_pmo_transient():
    pmo = GitHubIssuesAdapter(
        "https://api.github.com", "tok", "o/r",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(403, text="API rate limit exceeded")))
    with pytest.raises(PMOTransient):
        run(pmo.get(MissionRef("1", "issue")))


def test_403_permission_is_permanent():
    pmo = GitHubIssuesAdapter(
        "https://api.github.com", "tok", "o/r",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(403, text="Resource not accessible")))
    with pytest.raises(RuntimeError, match="403"):
        run(pmo.get(MissionRef("1", "issue")))


def test_project_ref_raises():
    with pytest.raises(RuntimeError, match="projects"):
        run(make_pmo().get(MissionRef("1", "project")))
