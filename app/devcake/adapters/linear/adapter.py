"""Linear adapter: PMOPort over the GraphQL API (docs/05).

Full surface: cursor-paginated reads (issues + projects of ONE team),
normalization, label bootstrap, feed comments, status/label writes,
attachment upload, and mission/relation creation.
"""

import logging
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from ...domain.model import (ALL_LABELS, Activity, ActivityEntry, AttachmentRef,
                             Mission, MissionRef, NormalizedStatus, Priority)
from ...ports.pmo import PMOCapabilities, PMOHealth, PMOTransient

log = logging.getLogger("devcake.linear")

API = "https://api.linear.app/graphql"

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
_ASSET_RE = re.compile(r"https://uploads\.linear\.app/[^\s)\]]+")
# markdown-link form of the same asset URLs — recovers the human filename the
# feed carried, so ActivityEntry attachments arrive named (the domain never
# parses vendor asset URLs)
_NAMED_ASSET_RE = re.compile(
    r"\[([^\]]+\.\w{1,8})\]\((https://uploads\.linear\.app/\S+?)\)")

# inverseRelations page size: Linear returns ALL relation types and we filter
# for `blocks` client-side, so an undersized page can silently evict a blocker
# and blind the scheduling gate (docs/05 §6). A full page is logged loudly.
RELATIONS_PAGE = 50

# Nested issue/project labels page size (ISSUES #7). Was first: 20 with no
# tripwire — a heavily labeled issue could omit DEVCAKE-SKIP and re-dispatch.
# Full-page warning matches the relations pattern; get/_get paths also
# cursor-walk when hasNextPage is set.
LABELS_PAGE = 50
MAX_LABEL_PAGES = 5  # 5 × 50 = 250 labels fail-loud ceiling

# get_activity cursor-walk safety ceiling (docs/05 §3): 10 pages × 100 =
# 1,000 comments — a fail-loud valve (~50× DevCake's post-hygiene comment
# rate), not a design limit. Newest pages are fetched first, so the newest
# comments always survive if it trips.
MAX_COMMENT_PAGES = 10


class LinearAdapter:
    def __init__(self, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self._headers = {"Authorization": api_key, "Content-Type": "application/json"}
        self._team_cache: dict[str, dict[str, Any]] = {}
        self._transport = transport  # tests inject a MockTransport (contract test 9)

    async def _gql(self, query: str, variables: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
                resp = await client.post(API, headers=self._headers,
                                         json={"query": query, "variables": variables or {}})
        except httpx.HTTPError as e:
            raise PMOTransient(f"network: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PMOTransient(f"http {resp.status_code}")
        body = resp.json()
        if body.get("errors"):
            if any("RATELIMITED" in str(e.get("extensions", {}).get("code", ""))
                   for e in body["errors"]):
                raise PMOTransient("RATELIMITED")
            raise RuntimeError(f"linear graphql: {body['errors']}")
        return body["data"]

    # ── team resolution (cached) ─────────────────────────────────────────────

    async def _team(self, team_key: str) -> dict[str, Any]:
        if team_key not in self._team_cache:
            data = await self._gql(
                """query($key: String!) { teams(filter: {key: {eq: $key}}) { nodes {
                     id key
                     states { nodes { id name type position } }
                     labels(first: 100) { nodes { id name } }
                } } }""", {"key": team_key})
            nodes = data["teams"]["nodes"]
            if not nodes:
                raise RuntimeError(f"linear: team {team_key!r} not found")
            self._team_cache[team_key] = nodes[0]
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
        mapper's validator must see the WHOLE team — a first-page-only read
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
                     inverseRelations(first: 50) { nodes { type issue { id } } }
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
        # page is full so DEVCAKE-SKIP cannot hide past page 1 (ISSUES #7).
        for n in issues:
            page = (n.get("labels") or {}).get("pageInfo") or {}
            if page.get("hasNextPage") or len((n.get("labels") or {}).get("nodes") or []) >= LABELS_PAGE:
                await self._paginate_issue_labels(n["id"], n)
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
                 inverseRelations(first: 50) { nodes { type issue { id } } }
            } }""", {"id": pmo_id})
        issue = data["issue"]
        await self._paginate_issue_labels(pmo_id, issue)
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
        """Cursor-walk issue labels when the first page is full (ISSUES #7)."""
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

    async def get_activity(self, ref: MissionRef) -> Activity:
        """Issue: the full comment feed. Project: mission + entries=[] —
        Linear projects have no issue-style comments API (verified live)."""
        if ref.kind == "project":
            return Activity(mission=await self._get_project(ref.pmo_id), entries=[])
        pmo_id = ref.pmo_id
        # Cursor-paginated like every other list read (docs/05 §3; a single
        # page was verified LOSSY at 108 comments on 2026-07-12). orderBy:
        # createdAt is pinned deliberately — verified live to return
        # NEWEST-first — so if the safety ceiling ever trips, the newest
        # comments win: the merge-state and conflict-resolve markers
        # (docs/03 §4.1) live there, and losing them fails quiet.
        data = await self._gql(
            """query($id: String!) { issue(id: $id) {
                 id identifier title description url updatedAt priority
                 state { name type }
                 labels(first: 50) { pageInfo { hasNextPage endCursor }
                                    nodes { name } }
                 project { id }
                 comments(first: 100, orderBy: createdAt) {
                   pageInfo { hasNextPage endCursor }
                   nodes { body createdAt user { name } }
                 }
            } }""", {"id": pmo_id})
        issue = data["issue"]
        await self._paginate_issue_labels(pmo_id, issue)
        conn = issue["comments"]
        nodes = list(conn["nodes"])
        for _ in range(MAX_COMMENT_PAGES - 1):
            if not conn["pageInfo"]["hasNextPage"]:
                break
            page = await self._gql(
                """query($id: String!, $after: String!) { issue(id: $id) {
                     comments(first: 100, orderBy: createdAt, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { body createdAt user { name } }
                     }
                } }""", {"id": pmo_id, "after": conn["pageInfo"]["endCursor"]})
            conn = page["issue"]["comments"]
            nodes.extend(conn["nodes"])
        if conn["pageInfo"]["hasNextPage"]:   # ceiling hit — never silent
            log.warning("get_activity(%s): comment ceiling (%d) hit — oldest "
                        "comments truncated from ACTIVITY.md and marker counts",
                        pmo_id, 100 * MAX_COMMENT_PAGES)
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
            ))
        entries.sort(key=lambda e: e.ts)
        return Activity(mission=mission, entries=entries)

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
        """Shared fail-loud contract for both label-swap write paths: a
        rewrite from a truncated read would delete the overflow labels."""
        if (conn.get("pageInfo") or {}).get("hasNextPage"):
            raise RuntimeError(
                f"{what}: more than {LABELS_PAGE * MAX_LABEL_PAGES} labels — "
                "refusing label swap (rewrite would drop the overflow)")

    async def _swap_issue_labels(self, pmo_id: str, remove: set[str],
                                 add: set[str]) -> None:
        """Single issueUpdate(labelIds) — the closest-to-atomic op Linear offers.
        Read-modify-write: the read MUST see every label or the rewrite deletes
        the overflow, so it paginates and refuses to write past the ceiling
        (ISSUES #7 — the write path is where truncation destroys data)."""
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
        current = {l["name"].upper(): l["id"] for l in issue["labels"]["nodes"]}
        for name in remove:
            current.pop(name.upper(), None)
        for name in add:
            if name.upper() not in by_name:
                raise RuntimeError(f"label {name} missing — ensure_labels not run?")
            current[name.upper()] = by_name[name.upper()]
        await self._gql(
            """mutation($id: String!, $labelIds: [String!]!) {
                 issueUpdate(id: $id, input: {labelIds: $labelIds}) { success } }""",
            {"id": pmo_id, "labelIds": sorted(current.values())})

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
        # via schema introspection) — ensure the same managed set there too
        data = await self._gql(
            """query { projectLabels(first: 100) { nodes { id name } } }""")
        existing_p = {l["name"].upper() for l in data["projectLabels"]["nodes"]}
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
        (ISSUES #7 twin of the per-project read)."""
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
                break
            after = conn["pageInfo"]["endCursor"]
        return by_name

    async def _swap_project_labels(self, project_id: str, remove: set[str],
                                   add: set[str]) -> None:
        """Project labels are a separate workspace-level entity (verified live).
        Same read-modify-write hazard as _swap_issue_labels: paginate the read,
        refuse the rewrite past the ceiling (ISSUES #7)."""
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
        up = (await self._gql(
            """mutation($ct: String!, $fn: String!, $size: Int!) {
                 fileUpload(contentType: $ct, filename: $fn, size: $size) {
                   success
                   uploadFile { uploadUrl assetUrl headers { key value } }
                 } }""",
            {"ct": "text/markdown", "fn": filename, "size": len(data)}))["fileUpload"]
        uf = up["uploadFile"]
        headers = {h["key"]: h["value"] for h in uf["headers"]}
        headers["Content-Type"] = "text/markdown"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.put(uf["uploadUrl"], content=data, headers=headers)
            resp.raise_for_status()
        return uf["assetUrl"]

    async def download_asset(self, url: str) -> bytes:
        """assetUrl downloads require Linear auth (docs/05 §4)."""
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Authorization": self._headers["Authorization"]})
            resp.raise_for_status()
            return resp.content

    def capabilities(self) -> PMOCapabilities:
        return PMOCapabilities(projects_supported=True, project_labels_supported=True,
                               attachment_max_bytes=50 * 1024 * 1024,
                               native_label_swap_atomic=True,
                               relations_supported=True)

    # ── normalization ────────────────────────────────────────────────────────

    def _issue_to_mission(self, n: dict[str, Any]) -> Mission:
        # on issue B, inverseRelations holds relations where B is relatedIssue;
        # a `blocks` node's `issue` is the blocker (verified live, ADR-0007)
        relations = (n.get("inverseRelations") or {}).get("nodes") or []
        if len(relations) >= RELATIONS_PAGE:
            log.warning("issue %s: inverseRelations page is full (%d) — "
                        "blocked_by may be truncated; the gate could miss a blocker",
                        n.get("identifier"), len(relations))
        label_nodes = (n.get("labels") or {}).get("nodes") or []
        label_page = (n.get("labels") or {}).get("pageInfo")
        # hasNextPage after the pagination walk = genuine truncation (ceiling);
        # a length check alone would cry wolf on every fully-walked 50+ set
        if (label_page or {}).get("hasNextPage") or (
                label_page is None and len(label_nodes) >= LABELS_PAGE):
            log.warning("issue %s: labels truncated (%d fetched) — "
                        "DEVCAKE-* labels may be missing; risk of re-dispatch "
                        "(ISSUES #7)",
                        n.get("identifier"), len(label_nodes))
        return Mission(
            pmo_id=n["id"], pmo_kind="issue", key=n["identifier"], title=n["title"],
            description=n.get("description") or "",
            status=STATE_TYPE_MAP.get(n["state"]["type"], "backlog"),
            priority=PRIORITY_MAP.get(int(n.get("priority") or 0), "medium"),
            labels={l["name"].upper() for l in label_nodes},
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
                        "DEVCAKE-* labels may be missing (ISSUES #7)",
                        n.get("name"), len(label_nodes))
        return Mission(
            pmo_id=n["id"], pmo_kind="project", key=f"PRJ-{slug}", title=n["name"],
            # Linear caps project `description` at 255 chars (verified live);
            # the long-form body lives in `content`
            description=n.get("content") or n.get("description") or "",
            status=PROJECT_STATUS_MAP.get(status_type, "backlog"),
            priority=PRIORITY_MAP.get(int(n.get("priority") or 0), "medium"),
            labels={l["name"].upper() for l in label_nodes},
            updated_at=n["updatedAt"], url=n.get("url") or "",
        )
