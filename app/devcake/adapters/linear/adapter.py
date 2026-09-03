"""Linear adapter: PMOPort over the GraphQL API (docs/05).

Full surface: cursor-paginated reads (issues + projects of ONE team),
normalization, label bootstrap, feed comments, status/label writes,
attachment upload, and mission/relation creation.
"""

import logging
import mimetypes
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from ...domain.model import (ALL_LABELS, Activity, ActivityEntry, AttachmentRef,
                             Mission, MissionDocument, MissionRef,
                             NormalizedStatus, Priority, canonicalize_labels)
from ...ports.pmo import PMOCapabilities, PMOHealth, PMOTransient
from .._toolkit import label_write_lock
from ..budget import (RateSignal, bind_principal, budget_for, header_float,
                      header_int)

log = logging.getLogger("devcake.linear")

API = "https://api.linear.app/graphql"
API_HOST = "api.linear.app"
# Linear meters requests AND query complexity per user per hour (docs/05 §2a)
RATE_WINDOW_S = 3600


def rate_signal(resp: httpx.Response) -> RateSignal:
    """Linear's quota headers → the vendor-neutral signal (ADR-0040).
    Requests + complexity are per-user hourly buckets (reset in epoch
    MILLISECONDS); the endpoint-specific headers describe the endpoint just
    called and ride along for the health page only. A rejection is either
    a bare 429 or a 400 whose GraphQL errors carry code RATELIMITED with a
    `rateLimitResult` — one request refills in duration/limit ms."""
    h = resp.headers
    limit = header_int(h, "x-ratelimit-requests-limit")
    remaining = header_int(h, "x-ratelimit-requests-remaining")
    reset_ms = header_int(h, "x-ratelimit-requests-reset")
    reset_at = reset_ms / 1000.0 if reset_ms else None
    c_limit = header_int(h, "x-ratelimit-complexity-limit")
    c_remaining = header_int(h, "x-ratelimit-complexity-remaining")
    complexity = (c_remaining / c_limit
                  if c_limit and c_remaining is not None else None)
    endpoint = None
    if h.get("x-ratelimit-endpoint-name"):
        endpoint = {
            "name": h.get("x-ratelimit-endpoint-name"),
            "limit": header_int(h, "x-ratelimit-endpoint-requests-limit"),
            "remaining": header_int(h, "x-ratelimit-endpoint-requests-remaining"),
        }
    limited = resp.status_code == 429
    retry_after = header_float(h, "retry-after")
    if resp.status_code in (400, 429):
        try:
            body = resp.json()
        except ValueError:
            body = None
        errors = body.get("errors") if isinstance(body, dict) else None
        for err in errors or []:
            ext = (err.get("extensions") or {}) if isinstance(err, dict) else {}
            if ("RATELIMITED" in str(ext.get("code", "")).upper()
                    or str(ext.get("type", "")).lower() == "ratelimited"):
                limited = True
                meta = ((ext.get("meta") or {}).get("rateLimitResult") or {})
                duration, lim = meta.get("duration"), meta.get("limit")
                # the same shape reports the COMPLEXITY bucket (a limit in the
                # millions): its refill quantum is meaningless for requests
                # and its numbers must not pose as the request quota
                is_requests = bool(lim) and float(lim) <= 100_000
                if retry_after is None and duration and lim:
                    retry_after = (float(duration) / float(lim) / 1000.0 + 1.0
                                   if is_requests else 5.0)
                if is_requests and limit is None:
                    limit = int(lim)
                if is_requests and remaining is None and meta.get("remaining") is not None:
                    remaining = int(meta["remaining"])
                break
    return RateSignal(limit=limit, remaining=remaining, reset_at=reset_at,
                      window_s=RATE_WINDOW_S, limited=limited,
                      retry_after_s=retry_after, complexity_fraction=complexity,
                      endpoint=endpoint)

# state *type* → normalized (docs/05 §3; teams rename display names freely)
STATE_TYPE_MAP: dict[str, NormalizedStatus] = {
    "triage": "backlog", "backlog": "backlog", "unstarted": "backlog",
    "started": "in_progress", "completed": "done", "canceled": "canceled",
}
PRIORITY_MAP: dict[int, Priority] = {1: "urgent", 2: "high", 0: "medium", 3: "medium", 4: "low"}
# Linear project status categories → normalized (docs/05 §3)
PROJECT_STATUS_MAP: dict[str, NormalizedStatus] = {
    "backlog": "backlog", "planned": "backlog", "started": "in_progress",
    "completed": "done", "canceled": "canceled", "paused": "backlog",
}
_ASSET_RE = re.compile(r"https://uploads\.linear\.app/[^\s)\]>]+")
# markdown-link form of the same asset URLs — recovers the human filename the
# feed carried, so ActivityEntry attachments arrive named (the domain never
# parses vendor asset URLs). Both regexes tolerate the angle-bracket URL form
# `[name](<url>)` — Linear's DOCUMENT serializer emits it (verified live
# 2026-08-04 on the sandbox; comment bodies keep the plain form)
_NAMED_ASSET_RE = re.compile(
    r"\[([^\]]+\.\w{1,8})\]\(<?(https://uploads\.linear\.app/[^\s)>]+?)>?\)")

# inverseRelations page size: Linear returns ALL relation types and we filter
# for `blocks` client-side, so an undersized page can silently evict a blocker
# and blind the scheduling gate (docs/05 §6). A full page is logged loudly.
RELATIONS_PAGE = 50
MAX_RELATION_PAGES = 10  # 10 × 50 = 500 relations fail-loud ceiling (ADR-0012)

# Nested issue/project labels page size. Was first: 20 with no tripwire —
# a heavily labeled issue could omit DEVCAKE-SKIP and re-dispatch. Full-page
# warning matches the relations pattern; get/_get paths also cursor-walk when
# hasNextPage is set.
LABELS_PAGE = 50
MAX_LABEL_PAGES = 5  # 5 × 50 = 250 labels fail-loud ceiling

# get_activity cursor-walk safety ceiling (docs/05 §3): 10 pages × 100 =
# 1,000 comments — a fail-loud valve (~50× DevCake's post-hygiene comment
# rate), not a design limit. Newest pages are fetched first, so the newest
# comments always survive if it trips.
MAX_COMMENT_PAGES = 10
# full-history mode (ADR-0014 D3, activity-folder builder): 100 pages =
# 10,000 comments — a runaway-feed backstop; tripping it sets
# Activity.truncated so the builder renders a loud banner (never raises:
# a raise would starve the Dev's activity.get reply)
MAX_COMMENT_PAGES_FULL = 100

# Project full-mode enrichment ceilings (project-fidelity fix). Small pages
# because document/update bodies are unbounded text — the budget concern is
# response SIZE, not query complexity (the combined first-page query measured
# x-complexity 1349 live, ~7× under the ~10k budget that blew the team query).
PROJECT_DOCS_PAGE = 25
MAX_PROJECT_DOC_PAGES = 4      # 100 documents fail-loud ceiling
PROJECT_UPDATES_PAGE = 25
MAX_PROJECT_UPDATE_PAGES = 20  # 500 updates — feed semantics: trips truncated
UPDATE_COMMENTS_PAGE = 50
MAX_UPDATE_COMMENT_PAGES = 10  # per update, on the overflow walk


class LinearAdapter:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None,
                 instance: str = ""):
        self._headers = {"Authorization": api_key, "Content-Type": "application/json"}
        self._team_cache: dict[str, dict[str, Any]] = {}
        self._transport = transport  # tests inject a MockTransport (contract test 9)
        from ..http import PooledClient
        self._http = PooledClient(timeout=20, transport=transport)  # F16: keep-alive
        # the configured PMO-instance name this adapter serves (schema v3):
        # stamped on every Mission at normalization, so provenance can never
        # be missed by a fetch path
        self._instance = instance
        # ADR-0040: the request budget for this credential — shared with every
        # adapter on the same key, re-keyed by user once `_team` learns it
        self._budget = budget_for(API_HOST, api_key, system="linear",
                                  instance=instance)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        payload = {"query": query, "variables": variables or {}}
        try:
            # THE wire call — governed by the request budget (ADR-0040):
            # paced/refused for routine callers, waited + retried once after
            # a definitive rejection for critical ones; pooled client (F16)
            resp = await self._budget.request(
                lambda: self._http.get().post(API, headers=self._headers,
                                              json=payload),
                rate_signal, instance=self._instance)
        except httpx.HTTPError as e:
            raise PMOTransient(f"network: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PMOTransient(f"http {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as e:
            # a non-JSON body must never leak a decode error upward (that
            # would latch poll_degraded as a permanent instance failure)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"linear http {resp.status_code}: {resp.text[:200]}") from e
            raise PMOTransient(
                f"linear: non-JSON {resp.status_code} response") from e
        if not isinstance(body, dict):
            raise RuntimeError(f"linear: unexpected response shape "
                               f"({resp.status_code})")
        if body.get("errors"):
            if any("RATELIMITED" in str(e.get("extensions", {}).get("code", ""))
                   for e in body["errors"]):
                raise PMOTransient("RATELIMITED")
            raise RuntimeError(f"linear graphql: {body['errors']}")
        return body["data"]

    # ── team resolution (cached) ─────────────────────────────────────────────

    async def _team(self, team_key: str) -> dict[str, Any]:
        if team_key not in self._team_cache:
            # SPLIT queries (the M2 field note: Linear's ~10k query-
            # complexity budget). Nesting labels(first:100) WITH pageInfo
            # under teams(filter:) blew the budget ('Query too complex',
            # complexity 15560 — live-reproduced on the connection test), so
            # the team shell is one cheap query and the labels cursor-walk
            # rides the single-team query below (audit A12 pagination kept).
            # `viewer` rides the same query at zero extra cost: the key's
            # user is the quota principal (every key of one user shares one
            # bucket), so the budget is re-keyed by it (ADR-0040)
            data = await self._gql(
                """query($key: String!) { viewer { id }
                   teams(filter: {key: {eq: $key}}) { nodes {
                     id key
                     states { nodes { id name type position } }
                } } }""", {"key": team_key})
            viewer = data.get("viewer") if isinstance(data, dict) else None
            if isinstance(viewer, dict) and viewer.get("id"):
                self._budget = bind_principal(self._budget, str(viewer["id"]))
            nodes = data["teams"]["nodes"]
            if not nodes:
                raise RuntimeError(f"linear: team {team_key!r} not found")
            team = nodes[0]
            # every consumer — label swaps, create_mission, ensure_labels,
            # the health probe — reads this cache, so a managed label past
            # 100 team labels must be visible here. Fail loud past the
            # ceiling (a silently missing DEVCAKE-* label breaks swaps).
            team["labels"] = {"nodes": []}
            info: dict = {"hasNextPage": True, "endCursor": None}
            pages = 0
            while info.get("hasNextPage"):
                if pages >= MAX_LABEL_PAGES:
                    raise RuntimeError(
                        f"linear: team {team_key} has more than "
                        f"{100 * MAX_LABEL_PAGES} labels — refusing (a "
                        f"managed label past the ceiling would silently "
                        f"fail to resolve)")
                page = await self._gql(
                    """query($id: String!, $after: String) { team(id: $id) {
                         labels(first: 100, after: $after) {
                           nodes { id name }
                           pageInfo { hasNextPage endCursor } } } }""",
                    {"id": team["id"], "after": info.get("endCursor")})
                conn = page["team"]["labels"]
                team["labels"]["nodes"].extend(conn["nodes"])
                info = conn["pageInfo"] or {}
                pages += 1
            self._team_cache[team_key] = team
        return self._team_cache[team_key]

    def _invalidate_team_cache(self) -> None:
        self._team_cache.clear()

    # ── reads ────────────────────────────────────────────────────────────────

    async def list_missions(self, team_ref: str) -> list[Mission]:
        """Non-terminal Projects + Issues of the ONE configured team (docs/05 §1)."""
        missions = await self.list_all(team_ref)
        return [m for m in missions if m.status not in ("done", "canceled")]

    async def _paginate(self, query: str, root: str, variables: dict) -> list[dict]:
        """Cursor-paginate a top-level connection. The scheduling gate and the
        steward's validator must see the WHOLE team — a first-page-only read
        turns silent truncation into wrong scheduling decisions (docs/05 §6)."""
        nodes: list[dict] = []
        after: str | None = None
        while True:
            data = await self._gql(query, {**variables, "after": after})
            page = data[root]
            nodes.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                return nodes
            after = page["pageInfo"]["endCursor"]

    async def list_all(self, team_ref: str) -> list[Mission]:
        """Terminal included — the /api/v1/missions debug view (derivation row 5 visible)."""
        team = await self._team(team_ref)
        issues = await self._paginate(
            """query($teamId: ID!, $after: String) {
                 issues(first: 100, after: $after, filter: {team: {id: {eq: $teamId}}}) {
                   pageInfo { hasNextPage endCursor }
                   nodes {
                     id identifier title description url updatedAt priority
                     state { name type }
                     labels(first: 50) { pageInfo { hasNextPage endCursor }
                                        nodes { name } }
                     project { id }
                     inverseRelations(first: 50) { pageInfo { hasNextPage endCursor }
                                            nodes { type issue { id } } }
                   } } }""", "issues", {"teamId": team["id"]})
        projects = await self._paginate(
            """query($teamId: ID!, $after: String) {
                 projects(first: 50, after: $after,
                          filter: {accessibleTeams: {id: {eq: $teamId}}}) {
                   pageInfo { hasNextPage endCursor }
                   nodes {
                     id name description content url updatedAt priority
                     status { name type }
                     labels(first: 50) { pageInfo { hasNextPage endCursor }
                                        nodes { name } }
                   } } }""", "projects", {"teamId": team["id"]})
        # list_all is the scheduler's hot path — paginate labels when the first
        # page is full so DEVCAKE-SKIP cannot hide past page 1.
        for n in issues:
            page = (n.get("labels") or {}).get("pageInfo") or {}
            if page.get("hasNextPage") or len((n.get("labels") or {}).get("nodes") or []) >= LABELS_PAGE:
                await self._paginate_issue_labels(n["id"], n)
            if ((n.get("inverseRelations") or {}).get("pageInfo") or {}).get("hasNextPage"):
                await self._paginate_issue_relations(n["id"], n)
        for n in projects:
            page = (n.get("labels") or {}).get("pageInfo") or {}
            if page.get("hasNextPage") or len((n.get("labels") or {}).get("nodes") or []) >= LABELS_PAGE:
                await self._paginate_project_labels(n["id"], n)
        missions = [self._issue_to_mission(n) for n in issues]
        missions += [self._project_to_mission(n) for n in projects]
        return missions

    async def get(self, ref: MissionRef) -> Mission:
        """Unified read — the issue/project duality is dispatched HERE, never
        in the domain (docs/05)."""
        if ref.kind == "project":
            return await self._get_project(ref.pmo_id)
        return await self._get_issue(ref.pmo_id)

    async def _get_issue(self, pmo_id: str) -> Mission:
        data = await self._gql(
            """query($id: String!) { issue(id: $id) {
                 id identifier title description url updatedAt priority
                 state { name type }
                 labels(first: 50) { pageInfo { hasNextPage endCursor }
                                    nodes { name } }
                 project { id }
                 inverseRelations(first: 50) { pageInfo { hasNextPage endCursor }
                                            nodes { type issue { id } } }
            } }""", {"id": pmo_id})
        issue = data["issue"]
        await self._paginate_issue_labels(pmo_id, issue)
        if ((issue.get("inverseRelations") or {}).get("pageInfo") or {}).get("hasNextPage"):
            await self._paginate_issue_relations(pmo_id, issue)
        return self._issue_to_mission(issue)

    async def _get_project(self, pmo_id: str) -> Mission:
        data = await self._gql(
            """query($id: String!) { project(id: $id) {
                 id name description content url updatedAt priority
                 status { name type }
                 labels(first: 50) { pageInfo { hasNextPage endCursor }
                                    nodes { name } }
            } }""", {"id": pmo_id})
        project = data["project"]
        await self._paginate_project_labels(pmo_id, project)
        return self._project_to_mission(project)

    async def _paginate_issue_labels(self, pmo_id: str, issue: dict) -> None:
        """Cursor-walk issue labels when the first page is full."""
        conn = issue.get("labels") or {}
        nodes = list(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        for _ in range(MAX_LABEL_PAGES - 1):
            if not page_info.get("hasNextPage"):
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { issue(id: $id) {
                     labels(first: 50, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id name }
                     } } }""",
                {"id": pmo_id, "after": page_info["endCursor"]})
            conn = page["issue"]["labels"]
            nodes.extend(conn.get("nodes") or [])
            page_info = conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            log.warning("issue %s: label ceiling (%d) hit — labels may be truncated",
                        issue.get("identifier") or pmo_id,
                        LABELS_PAGE * MAX_LABEL_PAGES)
        issue["labels"] = {"nodes": nodes, "pageInfo": page_info}

    async def _paginate_issue_relations(self, pmo_id: str, issue: dict) -> None:
        """Cursor-walk inverseRelations past a full first page (ADR-0012): a
        truncated read under-blocks the scheduling gate AND silently skips
        decomposition edge inheritance, so the rare 50+-relation issue pays
        a follow-up query instead of dropping blockers."""
        conn = issue.get("inverseRelations") or {}
        nodes = list(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        for _ in range(MAX_RELATION_PAGES - 1):
            if not page_info.get("hasNextPage"):
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { issue(id: $id) {
                     inverseRelations(first: 50, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { type issue { id } }
                     } } }""",
                {"id": pmo_id, "after": page_info["endCursor"]})
            conn = page["issue"]["inverseRelations"]
            nodes.extend(conn.get("nodes") or [])
            page_info = conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            log.warning("issue %s: relation ceiling (%d) hit — blocked_by may "
                        "be truncated; the gate could miss a blocker",
                        issue.get("identifier") or pmo_id,
                        RELATIONS_PAGE * MAX_RELATION_PAGES)
        issue["inverseRelations"] = {"nodes": nodes, "pageInfo": page_info}

    async def _paginate_project_labels(self, pmo_id: str, project: dict) -> None:
        conn = project.get("labels") or {}
        nodes = list(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        for _ in range(MAX_LABEL_PAGES - 1):
            if not page_info.get("hasNextPage"):
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { project(id: $id) {
                     labels(first: 50, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id name }
                     } } }""",
                {"id": pmo_id, "after": page_info["endCursor"]})
            conn = page["project"]["labels"]
            nodes.extend(conn.get("nodes") or [])
            page_info = conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            log.warning("project %s: label ceiling (%d) hit — labels may be truncated",
                        pmo_id, LABELS_PAGE * MAX_LABEL_PAGES)
        project["labels"] = {"nodes": nodes, "pageInfo": page_info}

    async def get_activity(self, ref: MissionRef, full: bool = False) -> Activity:
        """Issue: the comment feed. Project shallow: mission + entries=[] —
        projects have no issue-style comments API, and no production caller
        rides the shallow project path (marker scans are issue-only). Project
        FULL (project-fidelity fix): mirrors the project-native feed — updates
        + their comments as entries, documents inline, externalLinks + native
        attachments + content-embedded uploads as mission_attachments.

        Shallow (default): the cheap recent-window query the marker-scan call
        paths ride — FIELD-identical to the pre-ADR-0014 query (whitespace
        aside; the cost pin is field-based), MAX_COMMENT_PAGES valve. Full (ADR-0014 D3,
        activity-folder builder only): entire history, reply ids, description
        assets + the native attachments connection; the hard stop sets
        Activity.truncated instead of raising (a raise would starve the Dev's
        activity.get reply)."""
        if ref.kind == "project":
            if full:
                return await self._get_project_activity_full(ref.pmo_id)
            return Activity(mission=await self._get_project(ref.pmo_id), entries=[])
        pmo_id = ref.pmo_id
        # Cursor-paginated like every other list read (docs/05 §3; a single
        # page was verified LOSSY at 108 comments on 2026-07-12). orderBy:
        # createdAt is pinned deliberately — verified live to return
        # NEWEST-first — so if the safety ceiling ever trips, the newest
        # comments win: the merge-state and conflict-resolve markers
        # (docs/03 §4.1) live there, and losing them fails quiet.
        comment_fields = ("id parent { id } body createdAt user { name }"
                          if full else "body createdAt user { name }")
        attachments_part = ("attachments(first: 50) { pageInfo { hasNextPage }"
                            " nodes { url title } }" if full else "")
        data = await self._gql(
            """query($id: String!) { issue(id: $id) {
                 id identifier title description url updatedAt priority
                 state { name type }
                 labels(first: 50) { pageInfo { hasNextPage endCursor }
                                    nodes { name } }
                 project { id }
                 %s
                 comments(first: 100, orderBy: createdAt) {
                   pageInfo { hasNextPage endCursor }
                   nodes { %s }
                 }
            } }""" % (attachments_part, comment_fields), {"id": pmo_id})
        issue = data["issue"]
        await self._paginate_issue_labels(pmo_id, issue)
        conn = issue["comments"]
        nodes = list(conn["nodes"])
        max_pages = MAX_COMMENT_PAGES_FULL if full else MAX_COMMENT_PAGES
        for _ in range(max_pages - 1):
            if not conn["pageInfo"]["hasNextPage"]:
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { issue(id: $id) {
                     comments(first: 100, orderBy: createdAt, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { %s }
                     }
                } }""" % comment_fields,
                {"id": pmo_id, "after": conn["pageInfo"]["endCursor"]})
            conn = page["issue"]["comments"]
            nodes.extend(conn["nodes"])
        truncated = False
        if conn["pageInfo"]["hasNextPage"]:   # ceiling hit — never silent
            if full:
                truncated = True              # builder renders the loud banner
                log.error("get_activity(%s): full-history hard stop (%d "
                          "comments) — the activity folder is INCOMPLETE",
                          pmo_id, 100 * max_pages)
            else:
                log.warning("get_activity(%s): comment ceiling (%d) hit — "
                            "oldest comments truncated from marker counts",
                            pmo_id, 100 * max_pages)
        mission = self._issue_to_mission(issue)
        entries = []
        for c in nodes:
            body = c["body"] or ""
            names = {url: name for name, url in _NAMED_ASSET_RE.findall(body)}
            entries.append(ActivityEntry(
                ts=c["createdAt"], author=(c.get("user") or {}).get("name") or "unknown",
                kind="comment", body=c["body"],
                attachments=[AttachmentRef(url=u, name=names.get(u))
                             for u in _ASSET_RE.findall(body)],
                entry_id=c.get("id"),
                parent_id=(c.get("parent") or {}).get("id"),
            ))
        entries.sort(key=lambda e: e.ts)
        return Activity(mission=mission, entries=entries,
                        mission_attachments=self._mission_attachments(issue)
                        if full else [],
                        truncated=truncated)

    @staticmethod
    def _collect_attachment_refs(texts: list[str],
                                 native_nodes: list[dict],
                                 native_overflow: bool) -> list[AttachmentRef]:
        """ADR-0014 D3 core: text-embedded uploads (named via markdown links)
        + a native attachment/link node list, url-deduped in that order.
        uploads.linear.app = downloadable file; anything else in the native
        list is an external link. Shared by the issue path (description +
        attachments connection) and the project full path (content + document
        bodies + externalLinks + project attachments)."""
        seen: set[str] = set()
        refs: list[AttachmentRef] = []
        for text in texts:
            text = text or ""
            names = {url: name for name, url in _NAMED_ASSET_RE.findall(text)}
            for u in _ASSET_RE.findall(text):
                if u not in seen:
                    seen.add(u)
                    refs.append(AttachmentRef(url=u, name=names.get(u), kind="file"))
        for node in native_nodes:
            u = (node or {}).get("url") or ""
            if not u or u in seen:
                continue
            seen.add(u)
            refs.append(AttachmentRef(
                url=u, name=node.get("title"),
                kind="file" if _ASSET_RE.match(u) else "link"))
        if native_overflow:  # never silent
            log.warning("get_activity: native attachment list exceeds 50 — "
                        "remainder omitted from the activity folder")
        return refs

    @classmethod
    def _mission_attachments(cls, issue: dict) -> list[AttachmentRef]:
        """ADR-0014 D3: assets the mission itself references — description-
        embedded uploads + the native attachments connection, url-deduped."""
        conn = issue.get("attachments") or {}
        return cls._collect_attachment_refs(
            [issue.get("description") or ""],
            list(conn.get("nodes") or []),
            bool((conn.get("pageInfo") or {}).get("hasNextPage")))

    # ── project full-history mode (project-fidelity fix) ─────────────────────

    async def _get_project_activity_full(self, pmo_id: str) -> Activity:
        """Full-history mode for project refs: the project-native feed
        (updates + their comments) as entries, Documents inline, and
        externalLinks + native attachments + content/document-embedded
        uploads as mission_attachments.

        The base project read stays FAIL-CLOSED (an unreadable mission must
        not dispatch — same as issues). The enrichment is FAIL-OPEN: any
        failure degrades to an empty section with a loud warning, because
        the mission must still dispatch and a raise would starve the Dev's
        activity.get reply (the gitea_issues best-effort asset read is the
        precedent). All field spellings verified live 2026-08-04
        (x-complexity 1349 for the combined first-page query — the budget
        concern here is response SIZE, hence the small pages)."""
        mission = await self._get_project(pmo_id)
        try:
            data = await self._gql(
                """query($id: String!) { project(id: $id) {
                     documents(first: %d) { pageInfo { hasNextPage endCursor }
                       nodes { id title content url } }
                     projectUpdates(first: %d, orderBy: createdAt) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id body createdAt user { name }
                         comments(first: 10) { pageInfo { hasNextPage endCursor }
                           nodes { id parent { id } body createdAt user { name } } } } }
                     externalLinks(first: 50) { pageInfo { hasNextPage }
                       nodes { url label } }
                     attachments(first: 50) { pageInfo { hasNextPage }
                       nodes { url title } }
                } }""" % (PROJECT_DOCS_PAGE, PROJECT_UPDATES_PAGE),
                {"id": pmo_id})
            project = data["project"]
            doc_nodes = await self._walk_project_documents(pmo_id, project)
            update_nodes, truncated = await self._walk_project_updates(pmo_id, project)
        except Exception as e:  # noqa: BLE001 — fail-open: dispatch with the brief alone
            log.warning("project %s: activity enrichment failed (%s: %s) — "
                        "dispatching with the brief only; documents/updates/"
                        "links omitted from the activity folder",
                        pmo_id, type(e).__name__, str(e)[:180])
            return Activity(mission=mission, entries=[])

        entries: list[ActivityEntry] = []
        for u in update_nodes:
            body = u.get("body") or ""
            names = {url: name for name, url in _NAMED_ASSET_RE.findall(body)}
            entries.append(ActivityEntry(
                ts=u["createdAt"],
                # author is free text; provenance stays sentinel-based
                # (docs/03 §8a), so the suffix is annotation, not identity
                author=f"{(u.get('user') or {}).get('name') or 'unknown'} (project update)",
                kind="comment", body=body,
                attachments=[AttachmentRef(url=url, name=names.get(url))
                             for url in _ASSET_RE.findall(body)],
                entry_id=u.get("id"), parent_id=None,
            ))
            for c in (u.get("comments") or {}).get("nodes") or []:
                cbody = c.get("body") or ""
                cnames = {url: name for name, url in _NAMED_ASSET_RE.findall(cbody)}
                entries.append(ActivityEntry(
                    ts=c["createdAt"],
                    author=(c.get("user") or {}).get("name") or "unknown",
                    kind="comment", body=cbody,
                    attachments=[AttachmentRef(url=url, name=cnames.get(url))
                                 for url in _ASSET_RE.findall(cbody)],
                    entry_id=c.get("id"),
                    # threaded replies keep their comment parent; top-level
                    # update comments thread under the update itself
                    parent_id=(c.get("parent") or {}).get("id") or u.get("id"),
                ))
        entries.sort(key=lambda e: e.ts)

        documents = [MissionDocument(title=d.get("title") or "untitled",
                                     content=d.get("content") or "",
                                     url=d.get("url") or "")
                     for d in doc_nodes]
        ext = project.get("externalLinks") or {}
        att = project.get("attachments") or {}
        native = ([{"url": n.get("url"), "title": n.get("label")}
                   for n in (ext.get("nodes") or [])]
                  + list(att.get("nodes") or []))
        overflow = bool((ext.get("pageInfo") or {}).get("hasNextPage")
                        or (att.get("pageInfo") or {}).get("hasNextPage"))
        refs = self._collect_attachment_refs(
            # mission.description is `content or description` (normalization);
            # document bodies are scanned too so their embedded uploads land
            [mission.description] + [d.content for d in documents],
            native, overflow)
        return Activity(mission=mission, entries=entries,
                        mission_attachments=refs, documents=documents,
                        truncated=truncated)

    async def _walk_project_documents(self, pmo_id: str, project: dict) -> list[dict]:
        """Cursor-walk documents past the first page, fail-loud ceiling."""
        conn = project.get("documents") or {}
        nodes = list(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        for _ in range(MAX_PROJECT_DOC_PAGES - 1):
            if not page_info.get("hasNextPage"):
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { project(id: $id) {
                     documents(first: %d, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id title content url }
                     } } }""" % PROJECT_DOCS_PAGE,
                {"id": pmo_id, "after": page_info["endCursor"]})
            conn = page["project"]["documents"]
            nodes.extend(conn.get("nodes") or [])
            page_info = conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):  # never silent
            log.warning("project %s: document ceiling (%d) hit — remainder "
                        "omitted from the activity folder", pmo_id,
                        PROJECT_DOCS_PAGE * MAX_PROJECT_DOC_PAGES)
        return nodes

    async def _walk_project_updates(self, pmo_id: str,
                                    project: dict) -> tuple[list[dict], bool]:
        """Cursor-walk the update feed (+ per-update comment overflow).
        The update feed carries FEED semantics: tripping its ceiling sets
        Activity.truncated so the builder renders the loud banner."""
        conn = project.get("projectUpdates") or {}
        nodes = list(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        for _ in range(MAX_PROJECT_UPDATE_PAGES - 1):
            if not page_info.get("hasNextPage"):
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { project(id: $id) {
                     projectUpdates(first: %d, after: $after, orderBy: createdAt) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id body createdAt user { name }
                         comments(first: 10) { pageInfo { hasNextPage endCursor }
                           nodes { id parent { id } body createdAt user { name } } } }
                     } } }""" % PROJECT_UPDATES_PAGE,
                {"id": pmo_id, "after": page_info["endCursor"]})
            conn = page["project"]["projectUpdates"]
            nodes.extend(conn.get("nodes") or [])
            page_info = conn.get("pageInfo") or {}
        truncated = False
        if page_info.get("hasNextPage"):
            truncated = True
            log.error("project %s: update-feed hard stop (%d updates) — the "
                      "activity folder is INCOMPLETE", pmo_id,
                      PROJECT_UPDATES_PAGE * MAX_PROJECT_UPDATE_PAGES)
        for u in nodes:
            cconn = u.get("comments") or {}
            if (cconn.get("pageInfo") or {}).get("hasNextPage"):
                await self._walk_update_comments(u)
        return nodes, truncated

    async def _walk_update_comments(self, update: dict) -> None:
        """Overflow walk for one update's comment thread via the top-level
        projectUpdate(id:) query (verified live: id is String!)."""
        conn = update.get("comments") or {}
        nodes = list(conn.get("nodes") or [])
        page_info = conn.get("pageInfo") or {}
        for _ in range(MAX_UPDATE_COMMENT_PAGES):
            if not page_info.get("hasNextPage"):
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { projectUpdate(id: $id) {
                     comments(first: %d, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id parent { id } body createdAt user { name } }
                     } } }""" % UPDATE_COMMENTS_PAGE,
                {"id": update["id"], "after": page_info["endCursor"]})
            conn = page["projectUpdate"]["comments"]
            nodes.extend(conn.get("nodes") or [])
            page_info = conn.get("pageInfo") or {}
        if page_info.get("hasNextPage"):  # never silent
            log.warning("project update %s: comment ceiling (%d) hit — "
                        "remainder omitted", update.get("id"),
                        UPDATE_COMMENTS_PAGE * MAX_UPDATE_COMMENT_PAGES)
        update["comments"] = {"nodes": nodes, "pageInfo": page_info}

    # ── writes ───────────────────────────────────────────────────────────────

    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        """Issue → comment; project → project update (Linear's project-native
        feed — projects have no comments API, verified live)."""
        if ref.kind == "project":
            await self._gql(
                """mutation($p: String!, $b: String!) {
                     projectUpdateCreate(input: {projectId: $p, body: $b}) { success } }""",
                {"p": ref.pmo_id, "b": markdown})
            return
        await self._gql(
            """mutation($id: String!, $body: String!) {
                 commentCreate(input: {issueId: $id, body: $body}) { success } }""",
            {"id": ref.pmo_id, "body": markdown})

    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None:
        if ref.kind == "project":
            await self._set_project_status(ref.pmo_id, status)
        else:
            await self._set_issue_status(ref.pmo_id, status)

    async def cancel_mission(self, ref: MissionRef) -> None:
        # Linear's abandonment IS the canceled workflow/project state, so this
        # delegates to the same mutation; idempotent (already-canceled is a
        # no-op state write). The port seam exists for PMOs where abandonment
        # is archive/close instead (docs/05 §0d).
        await self.set_status(ref, "canceled")

    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None:
        if ref.kind == "project":
            await self._swap_project_labels(ref.pmo_id, remove, add)
        else:
            await self._swap_issue_labels(ref.pmo_id, remove, add)

    async def children_of(self, ref: MissionRef) -> list[Mission]:
        if ref.kind != "project":
            return []          # Linear issues have no child missions
        nodes = await self._paginate(
            """query($pid: ID!, $after: String) {
                 issues(first: 100, after: $after, filter: {project: {id: {eq: $pid}}}) {
                   pageInfo { hasNextPage endCursor }
                   nodes {
                     id identifier title description url updatedAt priority
                     state { name type }
                     labels(first: 50) { pageInfo { hasNextPage endCursor }
                                        nodes { name } }
                     project { id }
                   } } }""", "issues", {"pid": ref.pmo_id})
        for n in nodes:
            page = (n.get("labels") or {}).get("pageInfo") or {}
            if page.get("hasNextPage") or len((n.get("labels") or {}).get("nodes") or []) >= LABELS_PAGE:
                await self._paginate_issue_labels(n["id"], n)
        return [self._issue_to_mission(n) for n in nodes]

    async def _set_issue_status(self, pmo_id: str, status: NormalizedStatus) -> None:
        team_key = (await self._get_issue(pmo_id)).key.split("-")[0]
        team = await self._team(team_key)
        wanted = {"backlog": "backlog", "in_progress": "started",
                  "done": "completed", "canceled": "canceled"}[status]
        states = sorted(team["states"]["nodes"], key=lambda s: s.get("position") or 0)
        state = next(s for s in states if s["type"] == wanted)
        await self._gql(
            """mutation($id: String!, $stateId: String!) {
                 issueUpdate(id: $id, input: {stateId: $stateId}) { success } }""",
            {"id": pmo_id, "stateId": state["id"]})

    @staticmethod
    def _refuse_truncated_rewrite(conn: dict, what: str) -> None:
        """Shared fail-loud contract for both label-swap paths: a project
        rewrite from a truncated read would delete the overflow labels, and
        an issue removal cannot be verified against one."""
        if (conn.get("pageInfo") or {}).get("hasNextPage"):
            raise RuntimeError(
                f"{what}: more than {LABELS_PAGE * MAX_LABEL_PAGES} labels — "
                "refusing label swap (rewrite would drop the overflow)")

    async def _swap_issue_labels(self, pmo_id: str, remove: set[str],
                                 add: set[str]) -> None:
        """Per-label ``issueAddLabel`` / ``issueRemoveLabel`` mutations
        (schema-verified live). No full-set rewrite exists on this path any
        more, so a concurrent writer's labels can never be clobbered and a
        truncated read can no longer delete overflow labels — the paginated
        read only resolves removal ids and skips no-op mutations. The ceiling
        refusal stays: past it a removal cannot be verified.
        Serialized per mission like every swap path (the CAKE-48 race:
        finalize's stage-label add and the discovery sweep's gate retire
        landed in the same second, and the sweep's stale full-set rewrite
        deleted the fresh stage label)."""
        async with label_write_lock(pmo_id):
            data = await self._gql(
                """query($id: String!) { issue(id: $id) {
                     team { key }
                     labels(first: 50) { pageInfo { hasNextPage endCursor }
                                        nodes { id name } } } }""", {"id": pmo_id})
            issue = data["issue"]
            await self._paginate_issue_labels(pmo_id, issue)
            self._refuse_truncated_rewrite(issue["labels"], f"issue {pmo_id}")
            team = await self._team(issue["team"]["key"])
            by_name = {l["name"].upper(): l["id"] for l in team["labels"]["nodes"]}
            # Presence map is mutated through the remove/add loops so
            # remove∩add ends PRESENT (add wins) — same as project path
            # `(current − remove) ∪ add` and forge-issue adapters.
            current = {l["name"].upper(): l["id"]
                       for l in issue["labels"]["nodes"]}
            for name in sorted(remove):
                lid = current.get(name.upper())
                if lid is None:
                    continue      # absent — nothing to remove
                await self._gql(
                    """mutation($id: String!, $lid: String!) {
                         issueRemoveLabel(id: $id, labelId: $lid) { success } }""",
                    {"id": pmo_id, "lid": lid})
                current.pop(name.upper(), None)
            for name in sorted(add):
                lid = by_name.get(name.upper())
                if lid is None:
                    raise RuntimeError(
                        f"label {name} missing — ensure_labels not run?")
                if name.upper() in current:
                    continue      # already present — the add would no-op
                await self._gql(
                    """mutation($id: String!, $lid: String!) {
                         issueAddLabel(id: $id, labelId: $lid) { success } }""",
                    {"id": pmo_id, "lid": lid})
                current[name.upper()] = lid

    async def ensure_labels(self, team_ref: str, names: set[str] = frozenset(ALL_LABELS)) -> None:
        # team-scoped issue labels
        team = await self._team(team_ref)
        existing = {l["name"].upper() for l in team["labels"]["nodes"]}
        for name in sorted(names):
            if name.upper() in existing:
                continue
            await self._gql(
                """mutation($name: String!, $teamId: String!) {
                     issueLabelCreate(input: {name: $name, teamId: $teamId}) { success } }""",
                {"name": name, "teamId": team["id"]})
            log.info("linear: created label %s in team %s", name, team_ref)
        # project labels are a SEPARATE workspace-level entity in Linear (verified
        # via schema introspection) — ensure the same managed set there too.
        # Paginated (audit A12): an unpaginated first-100 read re-created any
        # managed project label living past page 1 on every boot.
        existing_p = set((await self._all_project_labels()).keys())
        for name in sorted(names):
            if name.upper() in existing_p:
                continue
            await self._gql(
                """mutation($name: String!) {
                     projectLabelCreate(input: {name: $name}) { success } }""",
                {"name": name})
            log.info("linear: created project label %s", name)
        self._invalidate_team_cache()

    async def create_mission(self, team_ref: str, title: str, description: str,
                             priority: str, label_names: set[str],
                             parent_ref: str | None = None) -> tuple[str, str]:
        """Returns (identifier, id) — the id is needed to wire relation edges.
        parent_ref is the containing project's pmo_id, when there is one."""
        team = await self._team(team_ref)
        by_name = {l["name"].upper(): l["id"] for l in team["labels"]["nodes"]}
        prio = {"urgent": 1, "high": 2, "medium": 3, "low": 4}[priority]
        missing = [n for n in label_names if n.upper() not in by_name]
        if missing:
            # a bare KeyError would surface as a 500; RuntimeError keeps it in
            # the "PMO refused us" family the API layer maps to 502 (a team
            # whose label bootstrap failed can lack managed labels)
            raise RuntimeError(
                f"label(s) {sorted(missing)} not present on team {team_ref!r} "
                "— label bootstrap incomplete?")
        inp: dict = {"teamId": team["id"], "title": title, "description": description,
                     "priority": prio,
                     "labelIds": [by_name[n.upper()] for n in label_names]}
        if parent_ref:
            inp["projectId"] = parent_ref
        data = await self._gql(
            """mutation($input: IssueCreateInput!) {
                 issueCreate(input: $input) { issue { id identifier } } }""",
            {"input": inp})
        issue = data["issueCreate"]["issue"]
        return issue["identifier"], issue["id"]

    async def health_probe(self, team_ref: str) -> PMOHealth:
        """Connection + label-bootstrap probe. Counts DevCake's MANAGED label
        set (intersection with ALL_LABELS), not any DEVCAKE-prefixed name."""
        team = await self._team(team_ref)
        present = {l["name"].upper() for l in team["labels"]["nodes"]} & ALL_LABELS
        return PMOHealth(ok=True, workspace=team.get("key") or team_ref,
                         managed_labels_present=len(present),
                         managed_labels_expected=len(ALL_LABELS))

    async def append_description(self, ref: MissionRef, text: str) -> None:
        """Read-modify-write append (port contract: append-only, issues
        only). The read-to-write window is unguarded — acceptable for the
        lineage-note caller, which appends a marker-shaped footer."""
        if ref.kind != "issue":
            raise ValueError(
                "append_description targets issues only (Linear caps project "
                "`description` at 255 chars; no project caller exists)")
        current = (await self._get_issue(ref.pmo_id)).description or ""
        await self._gql(
            """mutation($id: String!, $description: String!) {
                 issueUpdate(id: $id, input: {description: $description}) { success } }""",
            {"id": ref.pmo_id, "description": current + text})

    async def create_relation(self, blocker_id: str, blocked_id: str) -> None:
        """`issueId blocks relatedIssueId` (docs/05 §6, ADR-0007). Duplicate
        relations are tolerated so decomposition resume stays idempotent."""
        try:
            await self._gql(
                """mutation($a: String!, $b: String!) {
                     issueRelationCreate(input: {issueId: $a, relatedIssueId: $b,
                                                 type: blocks}) { success } }""",
                {"a": blocker_id, "b": blocked_id})
        except RuntimeError as e:
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                log.info("linear: relation %s blocks %s already exists",
                         blocker_id, blocked_id)
                return
            raise

    async def _all_project_labels(self) -> dict[str, str]:
        """NAME→id over the whole workspace projectLabels registry, cursor-
        paginated: a DEVCAKE-* project label beyond page 1 must still resolve
        (twin of the per-project read). Past the ceiling it raises instead of
        returning a silent truncation (audit A29 — an unresolvable managed
        label with no signal)."""
        by_name: dict[str, str] = {}
        after = None
        for _ in range(MAX_LABEL_PAGES):
            page = await self._gql(
                """query($after: String) { projectLabels(first: 100, after: $after) {
                     pageInfo { hasNextPage endCursor }
                     nodes { id name } } }""", {"after": after})
            conn = page["projectLabels"]
            by_name.update({l["name"].upper(): l["id"] for l in conn["nodes"]})
            if not conn["pageInfo"].get("hasNextPage"):
                return by_name
            after = conn["pageInfo"]["endCursor"]
        raise RuntimeError(
            f"linear: more than {100 * MAX_LABEL_PAGES} project labels — "
            f"refusing (a DEVCAKE-* label past the ceiling would silently "
            f"fail to resolve)")

    async def _swap_project_labels(self, project_id: str, remove: set[str],
                                   add: set[str]) -> None:
        """Project labels are a separate workspace-level entity (verified live)
        with no per-label mutation counterpart, so this path keeps the full-set
        read-modify-write: paginate the read, refuse the rewrite past the
        ceiling, and hold the per-mission lock so a concurrent writer's label
        is never rewritten away."""
        async with label_write_lock(project_id):
            return await self._swap_project_labels_locked(project_id, remove, add)

    async def _swap_project_labels_locked(self, project_id: str,
                                          remove: set[str],
                                          add: set[str]) -> None:
        by_name = await self._all_project_labels()
        proj = await self._gql(
            """query($id: String!) { project(id: $id) {
                 labels(first: 50) { pageInfo { hasNextPage endCursor }
                                    nodes { id name } } } }""", {"id": project_id})
        project = proj["project"]
        await self._paginate_project_labels(project_id, project)
        self._refuse_truncated_rewrite(project["labels"], f"project {project_id}")
        current = {l["name"].upper(): l["id"] for l in project["labels"]["nodes"]}
        for name in remove:
            current.pop(name.upper(), None)
        for name in add:
            if name.upper() not in by_name:
                raise RuntimeError(
                    f"project label {name} missing — ensure_labels not run?")
            current[name.upper()] = by_name[name.upper()]
        await self._gql(
            """mutation($id: String!, $l: [String!]!) {
                 projectUpdate(id: $id, input: {labelIds: $l}) { success } }""",
            {"id": project_id, "l": sorted(current.values())})

    async def _set_project_status(self, project_id: str, status: NormalizedStatus) -> None:
        wanted = {"backlog": "backlog", "in_progress": "started",
                  "done": "completed", "canceled": "canceled"}[status]
        data = await self._gql("""query { projectStatuses { nodes { id name type } } }""")
        st = next(s for s in data["projectStatuses"]["nodes"]
                  if s["type"].lower() == wanted)
        await self._gql(
            """mutation($id: String!, $s: String!) {
                 projectUpdate(id: $id, input: {statusId: $s}) { success } }""",
            {"id": project_id, "s": st["id"]})

    async def upload_attachment(self, pmo_id: str, filename: str, data: bytes) -> str:
        """Linear 3-step upload (docs/05 §4): fileUpload → PUT bytes → assetUrl."""
        ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        up = (await self._gql(
            """mutation($ct: String!, $fn: String!, $size: Int!) {
                 fileUpload(contentType: $ct, filename: $fn, size: $size) {
                   success
                   uploadFile { uploadUrl assetUrl headers { key value } }
                 } }""",
            {"ct": ct, "fn": filename, "size": len(data)}))["fileUpload"]
        uf = up["uploadFile"]
        headers = {h["key"]: h["value"] for h in uf["headers"]}
        headers["Content-Type"] = ct
        try:
            async with httpx.AsyncClient(timeout=60,
                                         transport=self._transport) as client:
                resp = await client.put(uf["uploadUrl"], content=data,
                                        headers=headers)
        except httpx.HTTPError as e:
            raise PMOTransient(f"linear upload network: {e}") from e
        # Status map mirrors download_asset: only 429/5xx are retryable.
        # Do not wrap raise_for_status() in HTTPError → PMOTransient —
        # HTTPStatusError ⊆ HTTPError and permanent 4xx would be retried.
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"linear upload → {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"linear upload → {resp.status_code}: "
                f"{(resp.text or '')[:200]}")
        return uf["assetUrl"]

    async def download_asset(self, url: str) -> bytes:
        """assetUrl downloads require Linear auth (docs/05 §4).

        Host allowlist + no off-host redirects + size cap (docs/14 §11)."""
        from ...domain.asset_fetch import (
            AssetUrlError, assert_downloadable_asset_url,
            enforce_download_byte_cap,
        )
        allowed = {"uploads.linear.app"}
        try:
            url = assert_downloadable_asset_url(url, allowed_hosts=allowed)
        except AssetUrlError as e:
            raise RuntimeError(f"linear download refused: {e}") from e
        # THE credentialed redirect walk (adapters/_toolkit, ADR-0034):
        # every hop re-validated on the allowlist — never follow an open
        # redirect with the Linear Authorization header. allow_http=False:
        # uploads.linear.app is https-only (the policy is stated HERE, not
        # drifted — the gitea_issues twin legitimately passes True).
        from .._toolkit import fetch_following_safe_redirects
        headers = {"Authorization": self._headers["Authorization"]}
        cap = self.capabilities().attachment_max_bytes
        async with httpx.AsyncClient(
                timeout=60, transport=self._transport,
                follow_redirects=False) as client:
            try:
                resp = await fetch_following_safe_redirects(
                    client, url, allowed_hosts=allowed, headers=headers,
                    allow_http=False)
            except AssetUrlError as e:
                raise RuntimeError(
                    f"linear download redirect refused: {e}") from e
            except httpx.HTTPError as e:
                raise PMOTransient(f"linear download network: {e}") from e
            except RuntimeError:
                raise RuntimeError("linear download: too many redirects")
            if resp.status_code in (429, 500, 502, 503, 504):
                raise PMOTransient(
                    f"linear download → {resp.status_code}")
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"linear download → {resp.status_code}: "
                    f"{(resp.text or '')[:200]}")
            try:
                return enforce_download_byte_cap(
                    resp.content,
                    content_length=resp.headers.get("content-length"),
                    max_bytes=cap,
                )
            except AssetUrlError as e:
                raise RuntimeError(
                    f"linear download refused: {e}") from e

    def capabilities(self) -> PMOCapabilities:
        return PMOCapabilities(projects_supported=True, project_labels_supported=True,
                               attachment_max_bytes=50 * 1024 * 1024,
                               native_label_swap_atomic=True,
                               relations_supported=True,
                               global_ids=True)   # Linear pmo_ids are UUIDs

    # ── normalization ────────────────────────────────────────────────────────

    def _issue_to_mission(self, n: dict[str, Any]) -> Mission:
        # on issue B, inverseRelations holds relations where B is relatedIssue;
        # a `blocks` node's `issue` is the blocker (verified live, ADR-0007)
        relations = (n.get("inverseRelations") or {}).get("nodes") or []
        rel_page = (n.get("inverseRelations") or {}).get("pageInfo")
        # hasNextPage after the pagination walk = ceiling hit; a pageInfo-less
        # node (query variants that skip relations) falls back to the length
        # heuristic so genuine truncation is never silent
        if (rel_page or {}).get("hasNextPage") or (
                rel_page is None and len(relations) >= RELATIONS_PAGE):
            log.warning("issue %s: inverseRelations truncated (%d fetched) — "
                        "blocked_by may be incomplete; the gate could miss a "
                        "blocker and decomposition could miss a dependent",
                        n.get("identifier"), len(relations))
        label_nodes = (n.get("labels") or {}).get("nodes") or []
        label_page = (n.get("labels") or {}).get("pageInfo")
        # hasNextPage after the pagination walk = genuine truncation (ceiling);
        # a length check alone would cry wolf on every fully-walked 50+ set
        if (label_page or {}).get("hasNextPage") or (
                label_page is None and len(label_nodes) >= LABELS_PAGE):
            log.warning("issue %s: labels truncated (%d fetched) — "
                        "DEVCAKE-* labels may be missing; risk of re-dispatch",
                        n.get("identifier"), len(label_nodes))
        return Mission(
            pmo_id=n["id"], pmo_kind="issue", instance=self._instance,
            key=n["identifier"], title=n["title"],
            description=n.get("description") or "",
            status=STATE_TYPE_MAP.get(n["state"]["type"], "backlog"),
            priority=PRIORITY_MAP.get(int(n.get("priority") or 0), "medium"),
            labels=canonicalize_labels(l["name"] for l in label_nodes),
            updated_at=n["updatedAt"], url=n.get("url") or "",
            parent_ref=(n.get("project") or {}).get("id"),
            blocked_by=[r["issue"]["id"] for r in relations
                        if r.get("type") == "blocks" and r.get("issue")],
        )

    def _project_to_mission(self, n: dict[str, Any]) -> Mission:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", n["name"]).strip("-").lower()[:24]
        status_type = ((n.get("status") or {}).get("type") or "backlog").lower()
        label_nodes = (n.get("labels") or {}).get("nodes") or []
        label_page = (n.get("labels") or {}).get("pageInfo")
        if (label_page or {}).get("hasNextPage") or (
                label_page is None and len(label_nodes) >= LABELS_PAGE):
            log.warning("project %s: labels truncated (%d fetched) — "
                        "DEVCAKE-* labels may be missing",
                        n.get("name"), len(label_nodes))
        return Mission(
            pmo_id=n["id"], pmo_kind="project", instance=self._instance,
            key=f"PRJ-{slug}", title=n["name"],
            # Linear caps project `description` at 255 chars (verified live);
            # the long-form body lives in `content`
            description=n.get("content") or n.get("description") or "",
            status=PROJECT_STATUS_MAP.get(status_type, "backlog"),
            priority=PRIORITY_MAP.get(int(n.get("priority") or 0), "medium"),
            labels=canonicalize_labels(l["name"] for l in label_nodes),
            updated_at=n["updatedAt"], url=n.get("url") or "",
        )
