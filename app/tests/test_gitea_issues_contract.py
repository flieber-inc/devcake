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

        # labels collection (pagination-aware: the adapter must walk pages —
        # a one-page read feeding the full-set PUT is the audit-F8 bug)
        if path == "/api/v1/repos/o/r/labels":
            if method == "GET":
                page = int(req.url.params.get("page", 1))
                limit = int(req.url.params.get("limit", 50))
                items = list(self.labels.values())
                return httpx.Response(
                    200, json=items[(page - 1) * limit: page * limit])
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
                page = int(req.url.params.get("page", 1))
                limit = int(req.url.params.get("limit", 50))
                all_c = self.comments.get(num, [])
                start = (page - 1) * limit
                chunk = all_c[start:start + limit]
                return httpx.Response(
                    200, json=chunk,
                    headers={"X-Total-Count": str(len(all_c))})
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
            if rest == "/labels" and method == "GET":
                page = int(req.url.params.get("page", 1))
                limit = int(req.url.params.get("limit", 50))
                items = list(self.issues[num].get("labels") or [])
                return httpx.Response(
                    200, json=items[(page - 1) * limit: page * limit])
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


def test_mixed_case_managed_label_normalizes_and_can_be_swapped():
    r = Router()
    r.labels["DEVCAKE-PLAN"] = {
        "id": 2, "name": "Devcake-Plan", "color": "111"}
    r.issues[1]["labels"] = [
        r.labels["DEVCAKE"], r.labels["DEVCAKE-PLAN"]]
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


def test_get_activity_ceiling_keeps_newest_comments(monkeypatch):
    from devcake.adapters.gitea_issues import adapter as gi
    monkeypatch.setattr(gi, "MAX_COMMENT_PAGES", 2)
    monkeypatch.setattr(gi, "COMMENTS_PAGE", 2)
    r = Router()
    r.comments[1] = [
        {"id": i, "body": f"c{i}", "created_at": f"2026-07-20T12:00:{i:02d}Z",
         "user": {"login": "bot"}, "assets": []}
        for i in range(1, 8)
    ]
    act = run(make_pmo(r).get_activity(MissionRef("1", "issue")))
    assert act.truncated is True
    assert [e.body for e in act.entries] == ["c5", "c6", "c7"]


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


def test_create_mission_refuses_unresolved_label():
    """swap_labels fail-loud twin: dropping an unresolved id on create
    would mint an issue without the managed label the caller required."""
    r = Router()
    pmo = make_pmo(r)

    async def silent_ensure(team_ref, names):
        return None

    pmo.ensure_labels = silent_ensure
    pmo._label_ids.clear()
    with pytest.raises(RuntimeError, match="missing"):
        run(pmo.create_mission(
            "o/r", "child", "desc", "high", {"DEVCAKE", "NOPE"}))
    assert not any(c.startswith("POST /api/v1/repos/o/r/issues")
                   for c in r.calls)


def test_health_probe_counts_managed_labels_only():
    """READ-ONLY since audit F3: the probe REPORTS the managed-label deficit
    honestly (healing rides the poll once-latch) and a custom label never
    inflates the count."""
    r = Router()
    r.labels["CUSTOM-EXTRA"] = {"id": 99, "name": "CUSTOM-EXTRA", "color": "fff"}
    pmo = make_pmo(r)
    h = run(pmo.health_probe("o/r"))
    assert h.ok
    assert h.workspace == "o/r"
    assert h.managed_labels_expected == len(ALL_LABELS)
    assert h.managed_labels_present == 1      # only DEVCAKE seeded; no healing
    # a healed repo (the poll latch ran ensure_labels) reports the full set
    run(pmo.ensure_labels("o/r", ALL_LABELS))
    r.calls.clear()
    h = run(pmo.health_probe("o/r"))
    assert h.managed_labels_present == len(ALL_LABELS)
    assert not any(c.startswith(("POST", "PATCH")) for c in r.calls)


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


def test_operator_url_rewrites_api_origin_to_the_ui(monkeypatch):
    """Mission.url is operator-clickable — gitea:3000 is not, localhost:3300 is."""
    monkeypatch.setenv("GITEA_UI_URL", "http://localhost:3300")
    pmo = GiteaIssuesAdapter("http://gitea:3000", "tok", "o/r")
    assert pmo._operator_url(
        "http://gitea:3000/devcake-pmo/missions/issues/7") == (
        "http://localhost:3300/devcake-pmo/missions/issues/7")
    assert pmo._operator_url(
        "http://localhost:3300/o/r/issues/1") == (
        "http://localhost:3300/o/r/issues/1")
    assert pmo._operator_url("https://linear.app/x") == "https://linear.app/x"


def test_app_reachable_url_rewrites_gitea_ui_url_host(monkeypatch):
    """GITEA_UI_URL presentation host (same signal as internal forge) rewrites."""
    monkeypatch.setenv("GITEA_UI_URL", "https://gitea.example.com")
    pmo = GiteaIssuesAdapter("http://gitea:3000", "tok", "o/r")
    assert pmo._app_reachable_url(
        "https://gitea.example.com/attachments/abc") == (
        "http://gitea:3000/attachments/abc")


def test_download_asset_refuses_evil_host():
    """Allowlist runs before rewrite — ticket SSRF hosts are refused."""
    pmo = GiteaIssuesAdapter("http://gitea:3000", "tok", "o/r",
                             transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="refused"):
        run(pmo.download_asset("https://evil.example/secret"))


def test_download_asset_refuses_empty_api_base():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"nope")

    pmo = GiteaIssuesAdapter(
        "", "tok", "o/r", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="api_base is empty"):
        run(pmo.download_asset("http://localhost:3300/attachments/x"))
    assert seen == []


def test_download_asset_ui_host_downloads_via_origin(monkeypatch):
    monkeypatch.setenv("GITEA_UI_URL", "https://gitea.example.com")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"via-origin")

    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    body = run(pmo.download_asset(
        "https://gitea.example.com/attachments/uuid-1"))
    assert body == b"via-origin"
    assert seen == ["http://gitea:3000/attachments/uuid-1"]


def test_download_asset_wrong_port_rewrites_onto_origin_netloc():
    """Presentation hostname on another port must not be GETted; rewrite first."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"pinned")

    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    body = run(pmo.download_asset("http://gitea:9/attachments/x"))
    assert body == b"pinned"
    assert seen == ["http://gitea:3000/attachments/x"]


def test_download_asset_refuses_api_path():
    """Same-origin API paths are not attachment downloads."""
    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")))
    with pytest.raises(RuntimeError, match="refused"):
        run(pmo.download_asset("http://gitea:3000/api/v1/user"))


def test_download_asset_refuses_redirect_to_api_path():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/start"):
            return httpx.Response(
                302, headers={"Location": "/api/v1/user"})
        return httpx.Response(200, content=b"nope")

    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="redirect refused"):
        run(pmo.download_asset("http://gitea:3000/attachments/start"))
    assert len(seen) == 1


def test_download_asset_refuses_oversized_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x" * 100,
            headers={"Content-Length": "100"})

    pmo = GiteaIssuesAdapter(
        "http://gitea:3000", "tok", "o/r",
        transport=httpx.MockTransport(handler))
    # Monkeypatch capability cap via a tiny wrapper: call enforce with low cap
    # by patching capabilities on the instance.
    pmo.capabilities = lambda: type(  # type: ignore[method-assign]
        "C", (), {"attachment_max_bytes": 50})()
    with pytest.raises(RuntimeError, match="refused"):
        run(pmo.download_asset("http://gitea:3000/attachments/big"))


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


@pytest.mark.parametrize("op", [
    lambda pmo: pmo.get_activity(MissionRef("9", "project"), full=True),
    lambda pmo: pmo.get_activity(MissionRef("9", "project")),
    lambda pmo: pmo.post_feed(MissionRef("9", "project"), "hi"),
    lambda pmo: pmo.set_status(MissionRef("9", "project"), "done"),
    lambda pmo: pmo.swap_labels(MissionRef("9", "project"), set(), {"X"}),
    lambda pmo: pmo.cancel_mission(MissionRef("9", "project")),
    lambda pmo: pmo.append_description(MissionRef("9", "project"), "x"),
])
def test_project_ref_operations_raise_never_fabricate_or_noop(op):
    """projects_supported=False: a project ref is a caller bug (2026-08-12
    audit F1). The old adapter fabricated a Mission for get_activity and
    silently no-opped every write — success reported, no labels swapped, the
    misroute invisible forever. All seven now raise the permanent family."""
    router = Router()
    pmo = make_pmo(router)
    with pytest.raises(RuntimeError, match="projects are not supported"):
        run(op(pmo))
    assert not any(c.startswith(("POST", "PATCH", "PUT", "DELETE"))
                   for c in router.calls), "no write may reach the vendor"


# --- label-registry data safety (2026-08-12 audit F8) -----------------------


def test_swap_labels_reads_all_label_pages_and_preserves_overflow():
    """The destruction case: a human-applied label whose registry entry sits
    past page 1. The full-set PUT rewrite must still carry it — the old
    one-page read dropped its id and erased it from the issue."""
    router = Router()
    for i in range(118):                       # +DEVCAKE = 120 labels, 3 pages
        router.labels[f"L{i:03d}"] = {"id": 2000 + i, "name": f"L{i:03d}",
                                      "color": "ffffff"}
    router.labels["HUMANPICK"] = {"id": 9999, "name": "HUMANPICK",
                                  "color": "ffffff"}
    router.issues[1]["labels"] = [{"id": 1, "name": "DEVCAKE"},
                                  {"id": 9999, "name": "HUMANPICK"}]
    pmo = make_pmo(router)
    run(pmo.swap_labels(MissionRef("1", "issue"),
                        remove=set(), add={"DEVCAKE-PLAN"}))
    names = {lb["name"] for lb in router.issues[1]["labels"]}
    assert "HUMANPICK" in names, "overflow label must survive the rewrite"
    assert "DEVCAKE" in names and "DEVCAKE-PLAN" in names


def test_ensure_labels_finds_or_creates_discovery_label_once():
    """ADR-0033 adds an 11th managed label (DEVCAKE-DISCOVERY): find-or-
    create must locate it across label pages on a crowded registry and a
    second ensure must create nothing (the >50-label repo scenario from the
    2026-08-12 audit, extended to the new label)."""
    router = Router()
    for i in range(60):                        # crowded registry, >1 page
        router.labels[f"F{i:03d}"] = {"id": 4000 + i, "name": f"F{i:03d}",
                                      "color": "ffffff"}
    pmo = make_pmo(router)
    run(pmo.ensure_labels("o/r", ALL_LABELS))
    assert "DEVCAKE-DISCOVERY" in router.labels
    before = dict(router.labels["DEVCAKE-DISCOVERY"])
    total = len(router.labels)
    run(pmo.ensure_labels("o/r", ALL_LABELS))
    assert router.labels["DEVCAKE-DISCOVERY"] == before
    assert len(router.labels) == total         # idempotent — no duplicates


def test_label_ceiling_refuses_loudly_with_zero_writes():
    router = Router()
    for i in range(520):
        router.labels[f"M{i:04d}"] = {"id": 3000 + i, "name": f"M{i:04d}",
                                      "color": "ffffff"}
    pmo = make_pmo(router)
    with pytest.raises(RuntimeError, match="more than 500 labels"):
        run(pmo.swap_labels(MissionRef("1", "issue"),
                            remove=set(), add={"DEVCAKE-PLAN"}))
    assert not any(c.startswith("PUT") for c in router.calls)


def test_swap_labels_never_puts_a_set_with_unresolved_names(monkeypatch):
    """An issue carrying a label absent from the registry (deleted from the
    repo while still attached): if the ensure retry cannot resolve it, the
    swap must raise — the old code PUT the partial set and erased it."""
    router = Router()
    router.issues[1]["labels"] = [{"id": 77, "name": "GHOST"}]
    pmo = make_pmo(router)

    async def ensure_noop(team_ref, names):
        return None

    monkeypatch.setattr(pmo, "ensure_labels", ensure_noop)
    with pytest.raises(RuntimeError, match="label GHOST missing"):
        run(pmo.swap_labels(MissionRef("1", "issue"),
                            remove=set(), add=set()))
    assert not any(c.startswith("PUT") for c in router.calls)


# --- dependency-enrichment cost (2026-08-12 audit F9) ------------------------


def test_list_all_skips_dependency_fetch_for_closed_issues():
    """blocked_by enrichment is one GET per issue per poll cycle; closed
    issues never need it (the gate reads a blocker's status off its own list
    row, and set_status clears dependencies on close) — history must not
    grow the poll's request count."""
    router = Router()
    router.issues[3] = _issue(3, state="closed")
    router.comments[3] = []
    router.deps[3] = []
    pmo = make_pmo(router)
    missions = run(pmo.list_all("o/r"))
    assert {m.pmo_id for m in missions} == {"1", "2", "3"}
    dep_gets = [c for c in router.calls
                if c.startswith("GET") and "/dependencies" in c]
    assert any("/issues/1/" in c for c in dep_gets)
    assert not any("/issues/3/" in c for c in dep_gets)


def test_health_probe_is_strictly_read_only():
    """PMOHealth contract (2026-08-12 audit F3): the probe rides the SPA's
    10 s /health poll — the old one ran ensure_labels (repo-settings PATCH +
    label POSTs) per call, forever. Reads only now; healing lives on the
    poll cycle's once-latch."""
    router = Router()
    pmo = make_pmo(router)
    health = run(pmo.health_probe("o/r"))
    assert health.ok is True
    writes = [c for c in router.calls
              if c.startswith(("POST", "PATCH", "PUT", "DELETE"))]
    assert not writes, f"probe must not write: {writes}"


# --- issue-label pagination (2026-08-12 review: _issue_label_names) ----------


def test_swap_labels_walks_all_issue_label_PAGES_and_preserves_overflow():
    """The F8 embed-read twin: the issue's OWN labels are read from the paged
    /issues/{id}/labels endpoint, not the single-page GET embed. A label on
    page 2 must survive the full-set PUT — the old embed read dropped it."""
    router = Router()
    # 60 labels ON issue #1 → 2 pages of 50; registry carries them all
    issue_labels = []
    for i in range(60):
        name = f"L{i:03d}"
        router.labels[name] = {"id": 4000 + i, "name": name, "color": "fff"}
        issue_labels.append({"id": 4000 + i, "name": name})
    router.issues[1]["labels"] = issue_labels
    pmo = make_pmo(router)
    run(pmo.swap_labels(MissionRef("1", "issue"),
                        remove=set(), add={"DEVCAKE-PLAN"}))
    # the PUT carried every one of the 60 overflow labels + the added one
    put = [c for c in router.calls if c.startswith("PUT") and "/labels" in c]
    assert put, "a PUT must have happened"
    names = {lb["name"] for lb in router.issues[1]["labels"]}
    assert "L058" in names and "L059" in names, "page-2 labels must survive"
    assert "DEVCAKE-PLAN" in names
    # and it genuinely paged the issue's labels (page>=2 requested)
    assert any("/issues/1/labels" in c for c in router.calls)


def test_swap_labels_refuses_when_issue_has_more_than_the_ceiling():
    """>500 labels on the issue → refuse the full-set rewrite from a
    truncated read, zero writes (the ceiling-raise path of _issue_label_names)."""
    router = Router()
    router.issues[1]["labels"] = [
        {"id": 5000 + i, "name": f"M{i:04d}"} for i in range(520)]
    pmo = make_pmo(router)
    with pytest.raises(RuntimeError, match="more than 500 labels"):
        run(pmo.swap_labels(MissionRef("1", "issue"),
                            remove=set(), add={"DEVCAKE-PLAN"}))
    assert not any(c.startswith("PUT") for c in router.calls)
