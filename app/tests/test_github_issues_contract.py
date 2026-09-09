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
from devcake.ports.pmo import FOLD_DETAILS, PMOPort, PMOTransient

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

        # comment by id (repository-scoped: the issue is not in the path)
        if path.startswith("/repos/o/r/issues/comments/") and method == "PATCH":
            cid = int(path.rsplit("/", 1)[1])
            for cs in self.comments.values():
                for c in cs:
                    if c["id"] == cid:
                        c["body"] = body.get("body") or ""
                        return httpx.Response(200, json=c)
            return httpx.Response(404, json={"message": "Not Found"})

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
                page = int(req.url.params.get("page", 1))
                per = int(req.url.params.get("per_page", 50))
                all_c = self.comments.get(num, [])
                start = (page - 1) * per
                chunk = all_c[start:start + per]
                last = max(1, (len(all_c) + per - 1) // per) if all_c else 1
                headers = {
                    "Link": f'<https://api.github.com/x?page={last}>; rel="last"',
                }
                return httpx.Response(200, json=chunk, headers=headers)
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


def test_uncancel_keeps_human_horizontal_rules():
    r = Router()
    r.issues[1]["body"] = "Intro\n\n---\n\nSection"
    pmo = make_pmo(r)
    run(pmo.cancel_mission(MissionRef("1", "issue")))
    run(pmo.set_status(MissionRef("1", "issue"), "done"))
    body = r.issues[1]["body"]
    assert CANCEL_FOOTER not in body
    assert "---" in body
    assert "Section" in body


def test_cancel_mission_idempotent():
    r = Router()
    pmo = make_pmo(r)
    run(pmo.cancel_mission(MissionRef("1", "issue")))
    assert r.issues[1]["state"] == "closed"
    assert CANCEL_FOOTER in r.issues[1]["body"]
    run(pmo.cancel_mission(MissionRef("1", "issue")))


def test_get_activity_ceiling_keeps_newest_comments(monkeypatch):
    from devcake.adapters.github_issues import adapter as gh
    monkeypatch.setattr(gh, "MAX_COMMENT_PAGES", 2)
    monkeypatch.setattr(gh, "COMMENTS_PAGE", 2)
    r = Router()
    r.comments[1] = [
        {"id": i, "body": f"c{i}", "created_at": f"2026-08-15T12:00:{i:02d}Z",
         "user": {"login": "bot"}}
        for i in range(1, 8)
    ]
    act = run(make_pmo(r).get_activity(MissionRef("1", "issue")))
    assert act.truncated is True
    bodies = [e.body for e in act.entries]
    assert bodies == ["c5", "c6", "c7"]


def test_post_feed_returns_the_entry_id_and_ignores_reply_to():
    """docs/03 §8: a flat vendor (feed_threads False) accepts the port's
    reply_to keyword, posts top level, and returns the comment id the
    full activity read reports for it."""
    pmo = make_pmo()
    assert pmo.capabilities().feed_threads is False
    cid = run(pmo.post_feed(MissionRef("1", "issue"), "first"))
    nested = run(pmo.post_feed(MissionRef("1", "issue"), "second",
                               reply_to=cid))
    act = run(pmo.get_activity(MissionRef("1", "issue"), full=True))
    assert [e.body for e in act.entries[-2:]] == ["first", "second"]
    assert [e.entry_id for e in act.entries[-2:]] == [cid, nested]
    assert all(e.parent_id is None for e in act.entries[-2:])


def test_post_feed_marker_round_trip():
    pmo = make_pmo()
    marker = "step `devcake:v1` ok"
    run(pmo.post_feed(MissionRef("1", "issue"), marker))
    act = run(pmo.get_activity(MissionRef("1", "issue")))
    assert act.entries[-1].body == marker


def test_edit_feed_replaces_the_body_in_place():
    """ADR-0042 §3: an edit is a whole-body replace of DevCake's own comment
    through `PATCH …/issues/comments/{id}` — same entry_id, same ts, the
    feed has no new entry, and a fold with a marker inside round-trips. A
    vanished comment is the adapter's permanent error, never PMOTransient."""
    r = Router()
    pmo = make_pmo(r)
    assert pmo.capabilities().feed_collapsible == FOLD_DETAILS
    ref = MissionRef("1", "issue")
    cid = run(pmo.post_feed(ref, "card `devcake:v1`"))
    before = run(pmo.get_activity(ref, full=True))
    new = ("card\n\n<details><summary>Details</summary>\n\n"
           "`devcake:discovery-routed:v1 step=1 to=-`\n\n</details>\n\n`devcake:v1`")
    run(pmo.edit_feed(ref, cid, new))
    assert r.calls[-1] == f"PATCH /repos/o/r/issues/comments/{cid}"
    after = run(pmo.get_activity(ref, full=True))
    assert len(after.entries) == len(before.entries)
    (entry,) = [e for e in after.entries if e.entry_id == cid]
    assert entry.body == new
    assert entry.ts == before.entries[-1].ts
    with pytest.raises(RuntimeError) as ei:
        run(pmo.edit_feed(ref, "999", "gone"))
    assert not isinstance(ei.value, PMOTransient)


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


def test_children_of_project_ref_raises_never_empty_list():
    """projects_supported=False: children_of must not silently return [] on a
    project ref (port F1 — every method raises the permanent family)."""
    with pytest.raises(RuntimeError, match="projects are not supported"):
        run(make_pmo().children_of(MissionRef("1", "project")))
    assert run(make_pmo().children_of(MissionRef("1", "issue"))) == []


def test_mixed_case_managed_label_normalizes_and_can_be_swapped():
    """GitHub folds case. A human `Devcake-Plan` must not wedge the stage."""
    r = Router()
    r.labels["DEVCAKE-PLAN"] = {
        "id": 2, "name": "Devcake-Plan", "color": "6e40c9"}
    r.issues[1] = _issue(1, labels=["DEVCAKE", "Devcake-Plan"])
    pmo = make_pmo(r)
    m = run(pmo.get(MissionRef("1", "issue")))
    assert "DEVCAKE-PLAN" in m.labels
    assert "Devcake-Plan" not in m.labels
    run(pmo.swap_labels(MissionRef("1", "issue"),
                        {"DEVCAKE-PLAN"}, {"DEVCAKE-EXECUTE"}))
    names = {lb["name"] for lb in r.issues[1]["labels"]}
    assert "Devcake-Plan" not in names
    assert "DEVCAKE-PLAN" not in names
    assert "DEVCAKE-EXECUTE" in names
    assert "DEVCAKE" in names


def test_declares_no_feed_delta():
    """No team-wide feed-changes read is wired: the capability is off and
    the port method raises, so the poll keeps its per-mission reads."""
    from datetime import datetime, timezone
    pmo = make_pmo()
    assert pmo.capabilities().feed_delta is False
    with pytest.raises(NotImplementedError):
        run(pmo.feed_changes_since(
            "o/r", datetime(2026, 1, 1, tzinfo=timezone.utc), limit_pages=1))
