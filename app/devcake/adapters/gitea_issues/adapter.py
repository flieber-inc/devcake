"""Gitea Issues PMOPort adapter (docs/05 forge-issue family).

Separate from adapters.gitea (ForgePort): own HTTP client, own token, own
package id `gitea_issues`. team_key is owner/repo; api_base is the Gitea
origin reachable from the app (internal: http://gitea:3000).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

import httpx

from ...domain.model import (ALL_LABELS, Activity, ActivityEntry, AttachmentRef,
                             Mission, MissionRef, NormalizedStatus)
from ...ports.pmo import PMOCapabilities, PMOHealth, PMOTransient
from .mapping import (CANCEL_FOOTER, mission_key, normalize_priority,
                      normalize_status, parse_team_ref)

log = logging.getLogger("devcake.gitea_issues")

# Managed-label colors (cosmetic; names are the contract)
_LABEL_COLOR = "6e40c9"

# get_activity ceilings (mirror Linear: fail-loud, newest-first survival)
MAX_COMMENT_PAGES = 10
MAX_COMMENT_PAGES_FULL = 100
COMMENTS_PAGE = 50
ISSUES_PAGE = 50
MAX_ISSUE_PAGES = 40  # 2000 issues fail-loud

# Attachment host patterns for markdown link recovery
_NAMED_ASSET_RE = re.compile(
    r"\[([^\]]+\.\w{1,8})\]\((https?://[^\s)]+/attachments/[^\s)]+)\)")
_BARE_ASSET_RE = re.compile(r"(https?://[^\s)]+/attachments/[A-Za-z0-9-]+)")

# Bundled Gitea ROOT_URL is browser-facing (localhost:3300); the app reaches
# the container as api_base (gitea:3000). Only these presentation hosts may
# be rewritten onto the origin — never arbitrary foreign hosts (that made
# the download allowlist vacuous). See docs/14 §11.
_REWRITEABLE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class GiteaIssuesAdapter:
    """PMOPort over Gitea Issues REST API (/api/v1). Issue-only missions."""

    def __init__(
        self,
        api_base: str | None,
        token: str,
        team_ref: str = "",
        *,
        instance: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._instance = instance
        self._token = token or ""
        self._transport = transport
        base = (api_base or "").strip().rstrip("/")
        if not base:
            # constructed only when configured; empty base fails loud on use
            self._api = ""
        else:
            # accept either origin or origin/api/v1
            if base.endswith("/api/v1"):
                self._api = base
            else:
                self._api = f"{base}/api/v1"
        self._team_ref = (team_ref or "").strip()
        self._owner, self._repo = ("", "")
        if self._team_ref:
            try:
                self._owner, self._repo = parse_team_ref(self._team_ref)
            except ValueError:
                # leave empty; health_probe reports the parse error
                self._owner, self._repo = ("", "")
        self._label_ids: dict[str, int] = {}  # upper name → id cache
        # Origin used to rewrite ROOT_URL attachment hosts (e.g. localhost:3300
        # in browser_download_url) so the app container can fetch via api_base.
        self._origin = self._api.removesuffix("/api/v1") if self._api else ""

    def _app_reachable_url(self, url: str) -> str:
        """Rewrite browser ROOT_URL hosts to the configured api_base origin.

        Self-hosted Gitea returns attachment URLs with ROOT_URL
        (http://localhost:3300/...) which is unreachable from the app
        container; the docker-network origin (http://gitea:3000) is.

        Only ``_REWRITEABLE_HOSTS`` (localhost / loopback) are rewritten —
        foreign hosts are left unchanged so the download allowlist can
        refuse them (docs/14 §11).
        """
        if not url or not self._origin:
            return url
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        origin_parts = urlsplit(self._origin)
        if not parts.scheme or not parts.netloc:
            return url
        host = (parts.hostname or "").lower()
        if host not in _REWRITEABLE_HOSTS:
            return url
        if parts.netloc == origin_parts.netloc:
            return url
        return urlunsplit((
            origin_parts.scheme, origin_parts.netloc,
            parts.path, parts.query, parts.fragment,
        ))

    def _asset_allowed_hosts(self) -> set[str]:
        """Hosts permitted on download_asset URLs (origin + rewriteable UI)."""
        from urllib.parse import urlsplit
        allowed = set(_REWRITEABLE_HOSTS)
        for base in (self._origin, self._api):
            if not base:
                continue
            h = urlsplit(base).hostname
            if h:
                allowed.add(h.lower())
        return allowed

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        if not self._token.strip():
            raise PMOTransient("gitea_issues: API token missing")
        return {"Authorization": f"token {self._token}"}

    async def _req(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | list | None = None,
        content: bytes | None = None,
        headers: dict | None = None,
        expect_json: bool = True,
    ) -> Any:
        if not self._api:
            raise PMOTransient("gitea_issues: api_base is empty")
        url = f"{self._api}{path}"
        hdrs = {**self._headers(), **(headers or {})}
        try:
            async with httpx.AsyncClient(
                    timeout=30, transport=self._transport) as client:
                resp = await client.request(
                    method, url, params=params, json=json, content=content,
                    headers=hdrs)
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitea_issues network: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            # 500 on duplicate dependency is handled by callers that inspect body
            if resp.status_code == 500 and "does already exist" in (resp.text or ""):
                return {"_duplicate": True, "text": resp.text}
            raise PMOTransient(
                f"gitea_issues {method} {path} → {resp.status_code}: "
                f"{resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitea_issues {method} {path} → {resp.status_code}: "
                f"{resp.text[:300]}")
        if not expect_json or not resp.content:
            return resp.content if not expect_json else None
        return resp.json()

    def _repo_path(self, suffix: str = "") -> str:
        if not self._owner or not self._repo:
            raise RuntimeError(
                f"gitea_issues: invalid team_key {self._team_ref!r} "
                f"(need owner/repo)")
        return f"/repos/{self._owner}/{self._repo}{suffix}"

    # ── normalization ───────────────────────────────────────────────────────

    def _mission(self, issue: dict) -> Mission:
        number = int(issue["number"])
        body = issue.get("body") or ""
        labels = { (lb.get("name") or "") for lb in (issue.get("labels") or [])
                   if lb.get("name") }
        labels = {n for n in labels if n}
        # blocked_by filled by callers that fetch dependencies
        updated = issue.get("updated_at") or issue.get("created_at") or ""
        try:
            updated_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            updated_at = datetime.utcnow()
        return Mission(
            pmo_id=str(number),
            pmo_kind="issue",
            key=mission_key(self._owner, self._repo, number),
            title=issue.get("title") or "",
            description=body,
            status=normalize_status(issue.get("state") or "open", body),
            priority=normalize_priority(),
            labels=labels,
            updated_at=updated_at,
            url=issue.get("html_url") or "",
            parent_ref=None,
            blocked_by=[],
            instance=self._instance,
        )

    async def _blocked_by_ids(self, index: int) -> list[str]:
        """Issue numbers (as pmo_id strings) that block this issue."""
        try:
            deps = await self._req(
                "GET", self._repo_path(f"/issues/{index}/dependencies"))
        except RuntimeError as e:
            # dependencies disabled → empty (ensure_labels enables them)
            if "404" in str(e) or "depend" in str(e).lower():
                return []
            raise
        if not isinstance(deps, list):
            return []
        out: list[str] = []
        for d in deps:
            n = d.get("number")
            if n is not None:
                out.append(str(n))
        return out

    async def _enrich_blocked_by(self, mission: Mission) -> Mission:
        blockers = await self._blocked_by_ids(int(mission.pmo_id))
        return mission.model_copy(update={"blocked_by": blockers})

    # ── reads ───────────────────────────────────────────────────────────────

    async def _list_issues(self, *, state: str = "all") -> list[dict]:
        issues: list[dict] = []
        page = 1
        while page <= MAX_ISSUE_PAGES:
            batch = await self._req(
                "GET", self._repo_path("/issues"),
                params={"state": state, "type": "issues",
                        "page": page, "limit": ISSUES_PAGE})
            if not batch:
                break
            # Gitea may include PRs in /issues — keep pure issues only
            for it in batch:
                if it.get("pull_request"):
                    continue
                issues.append(it)
            if len(batch) < ISSUES_PAGE:
                break
            page += 1
        else:
            log.warning("gitea_issues list_issues hit page ceiling (%s)",
                        MAX_ISSUE_PAGES)
        return issues

    async def list_missions(self, team_ref: str) -> list[Mission]:
        self._apply_team(team_ref)
        out: list[Mission] = []
        for raw in await self._list_issues(state="open"):
            m = self._mission(raw)
            if m.status in ("done", "canceled"):
                continue
            out.append(await self._enrich_blocked_by(m))
        return out

    async def list_all(self, team_ref: str) -> list[Mission]:
        self._apply_team(team_ref)
        out: list[Mission] = []
        for raw in await self._list_issues(state="all"):
            m = self._mission(raw)
            out.append(await self._enrich_blocked_by(m))
        return out

    def _apply_team(self, team_ref: str) -> None:
        ref = (team_ref or self._team_ref or "").strip()
        if ref and ref != self._team_ref:
            self._team_ref = ref
            self._owner, self._repo = parse_team_ref(ref)
            self._label_ids.clear()

    async def get(self, ref: MissionRef) -> Mission:
        if ref.kind != "issue":
            raise RuntimeError(
                "gitea_issues: projects are not supported "
                f"(got kind={ref.kind!r})")
        raw = await self._req(
            "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        return await self._enrich_blocked_by(self._mission(raw))

    async def get_activity(self, ref: MissionRef,
                           full: bool = False) -> Activity:
        if ref.kind != "issue":
            m = Mission(
                pmo_id=ref.pmo_id, pmo_kind="project", key=f"PRJ-{ref.pmo_id}",
                title="", description="", status="backlog", priority="medium",
                labels=set(), updated_at=datetime.utcnow(), url="",
                instance=self._instance)
            return Activity(mission=m, entries=[], mission_attachments=[],
                            truncated=False)
        mission = await self.get(ref)
        max_pages = MAX_COMMENT_PAGES_FULL if full else MAX_COMMENT_PAGES
        entries: list[ActivityEntry] = []
        truncated = False
        page = 1
        while page <= max_pages:
            batch = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}/comments"),
                params={"page": page, "limit": COMMENTS_PAGE})
            if not batch:
                break
            for c in batch:
                entries.append(self._comment_entry(c, full=full))
            if len(batch) < COMMENTS_PAGE:
                break
            page += 1
        else:
            truncated = True
            log.error("gitea_issues get_activity truncated at %s pages",
                      max_pages)
        # Gitea returns oldest-first typically; sort by ts for safety
        entries.sort(key=lambda e: e.ts)
        mission_attachments: list[AttachmentRef] = []
        if full:
            try:
                assets = await self._req(
                    "GET", self._repo_path(f"/issues/{ref.pmo_id}/assets"))
                for a in assets or []:
                    url = self._app_reachable_url(
                        a.get("browser_download_url") or "")
                    if url:
                        mission_attachments.append(AttachmentRef(
                            url=url, name=a.get("name"), kind="file"))
            except Exception as e:  # noqa: BLE001 — full mode best-effort assets
                log.warning("gitea_issues mission assets: %s", e)
        return Activity(mission=mission, entries=entries,
                        mission_attachments=mission_attachments,
                        truncated=truncated)

    def _comment_entry(self, c: dict, *, full: bool) -> ActivityEntry:
        body = c.get("body") or ""
        created = c.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.utcnow()
        user = (c.get("user") or {}).get("login") or "unknown"
        atts = self._attachments_from_body(body)
        for a in c.get("assets") or []:
            url = self._app_reachable_url(a.get("browser_download_url") or "")
            if url:
                atts.append(AttachmentRef(
                    url=url, name=a.get("name"), kind="file"))
        return ActivityEntry(
            ts=ts, author=user, kind="comment", body=body,
            attachments=atts,
            entry_id=str(c["id"]) if full and c.get("id") is not None else None,
            parent_id=None,
        )

    def _attachments_from_body(self, body: str) -> list[AttachmentRef]:
        found: list[AttachmentRef] = []
        seen: set[str] = set()
        for name, url in _NAMED_ASSET_RE.findall(body or ""):
            url = self._app_reachable_url(url)
            if url not in seen:
                seen.add(url)
                found.append(AttachmentRef(url=url, name=name, kind="file"))
        for url in _BARE_ASSET_RE.findall(body or ""):
            url = self._app_reachable_url(url)
            if url not in seen:
                seen.add(url)
                found.append(AttachmentRef(url=url, name=None, kind="file"))
        return found

    async def children_of(self, ref: MissionRef) -> list[Mission]:
        # Issue-only: no project children. Decomposition uses markers + relations.
        return []

    # ── writes ──────────────────────────────────────────────────────────────

    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        if ref.kind != "issue":
            return
        await self._req(
            "POST", self._repo_path(f"/issues/{ref.pmo_id}/comments"),
            json={"body": markdown})

    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None:
        if ref.kind != "issue":
            return
        if status in ("done", "canceled"):
            state = "closed"
            # Gitea 412s close when the issue still has open blockers
            # (live-verified 1.24.7). DevCake must be able to cancel/done
            # regardless — drop dependencies first (scheduler already used them).
            await self._clear_dependencies(int(ref.pmo_id))
        else:
            state = "open"
        body_patch: dict[str, Any] = {"state": state}
        if status == "canceled":
            # ensure cancel footer for durable readback
            cur = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
            body = cur.get("body") or ""
            if CANCEL_FOOTER not in body:
                body_patch["body"] = body.rstrip() + f"\n\n---\n{CANCEL_FOOTER}\n"
        elif status in ("backlog", "in_progress", "done"):
            # opening or completing: strip cancel footer if present so done ≠ canceled
            cur = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
            body = cur.get("body") or ""
            if CANCEL_FOOTER in body:
                cleaned = body.replace(CANCEL_FOOTER, "").replace("\n\n---\n\n", "\n")
                body_patch["body"] = cleaned
        await self._req(
            "PATCH", self._repo_path(f"/issues/{ref.pmo_id}"),
            json=body_patch)

    async def _clear_dependencies(self, index: int) -> None:
        blockers = await self._blocked_by_ids(index)
        for b in blockers:
            result = await self._req(
                "DELETE",
                self._repo_path(f"/issues/{index}/dependencies"),
                json={"owner": self._owner, "repo": self._repo,
                      "index": int(b)})
            _ = result

    async def cancel_mission(self, ref: MissionRef) -> None:
        if ref.kind != "issue":
            return
        cur = await self._req(
            "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        if (cur.get("state") == "closed"
                and CANCEL_FOOTER in (cur.get("body") or "")):
            return  # idempotent
        await self.set_status(ref, "canceled")

    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None:
        if ref.kind != "issue":
            return
        await self._ensure_label_cache()
        cur = await self._req(
            "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        current = { (lb.get("name") or "") for lb in (cur.get("labels") or []) }
        next_names = (current - remove) | add
        ids = []
        for name in next_names:
            lid = self._label_ids.get(name.upper())
            if lid is None:
                await self.ensure_labels(self._team_ref, {name})
                lid = self._label_ids.get(name.upper())
            if lid is not None:
                ids.append(lid)
        # PUT replaces the full set (live-verified 1.24.7)
        await self._req(
            "PUT", self._repo_path(f"/issues/{ref.pmo_id}/labels"),
            json={"labels": ids})

    async def create_mission(
        self, team_ref: str, title: str, description: str,
        priority: str, label_names: set[str],
        parent_ref: Optional[str] = None,
    ) -> tuple[str, str]:
        self._apply_team(team_ref)
        await self.ensure_labels(team_ref, label_names)
        await self._ensure_label_cache()
        label_ids = [self._label_ids[n.upper()]
                     for n in label_names
                     if n.upper() in self._label_ids]
        # parent_ref ignored (issue-only; decomposition uses markers/relations)
        _ = parent_ref
        _ = priority  # Gitea has no priority field
        raw = await self._req(
            "POST", self._repo_path("/issues"),
            json={"title": title, "body": description or "",
                  "labels": label_ids})
        number = int(raw["number"])
        key = mission_key(self._owner, self._repo, number)
        return key, str(number)

    async def create_relation(self, blocker_id: str, blocked_id: str) -> None:
        # Make blocked depend on blocker (blocker blocks blocked).
        # POST .../issues/{blocked}/dependencies  body IssueMeta of blocker
        payload = {
            "owner": self._owner,
            "repo": self._repo,
            "index": int(blocker_id),
        }
        result = await self._req(
            "POST",
            self._repo_path(f"/issues/{int(blocked_id)}/dependencies"),
            json=payload)
        if isinstance(result, dict) and result.get("_duplicate"):
            return  # idempotent

    async def ensure_labels(self, team_ref: str, names: set[str]) -> None:
        self._apply_team(team_ref)
        await self._enable_issue_dependencies()
        existing = await self._req("GET", self._repo_path("/labels"),
                                   params={"limit": 50})
        by_upper = {(lb.get("name") or "").upper(): lb
                    for lb in (existing or [])}
        for name in names:
            u = name.upper()
            if u in by_upper:
                self._label_ids[u] = int(by_upper[u]["id"])
                continue
            created = await self._req(
                "POST", self._repo_path("/labels"),
                json={"name": name.upper(), "color": _LABEL_COLOR,
                      "description": "DevCake managed label"})
            self._label_ids[u] = int(created["id"])
            by_upper[u] = created

    async def _ensure_label_cache(self) -> None:
        if self._label_ids:
            return
        existing = await self._req("GET", self._repo_path("/labels"),
                                   params={"limit": 50})
        for lb in existing or []:
            n = (lb.get("name") or "").upper()
            if n:
                self._label_ids[n] = int(lb["id"])

    async def _enable_issue_dependencies(self) -> None:
        """Dependencies are off by default on new repos (spike 1.24.7)."""
        try:
            await self._req(
                "PATCH", self._repo_path(""),
                json={"internal_tracker": {
                    "enable_time_tracker": False,
                    "allow_only_contributors_to_track_time": False,
                    "enable_issue_dependencies": True,
                }})
        except Exception as e:  # noqa: BLE001 — best-effort enable; create_relation may still work
            log.warning("gitea_issues enable dependencies: %s", e)

    async def append_description(self, ref: MissionRef, text: str) -> None:
        if ref.kind != "issue":
            return
        cur = await self._req(
            "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        body = (cur.get("body") or "") + text
        await self._req(
            "PATCH", self._repo_path(f"/issues/{ref.pmo_id}"),
            json={"body": body})

    # ── assets ──────────────────────────────────────────────────────────────

    async def upload_attachment(self, pmo_id: str, filename: str,
                                data: bytes) -> str:
        # multipart form field name "attachment" (live-verified)
        if not self._api:
            raise PMOTransient("gitea_issues: api_base is empty")
        url = f"{self._api}{self._repo_path(f'/issues/{pmo_id}/assets')}"
        try:
            async with httpx.AsyncClient(
                    timeout=60, transport=self._transport) as client:
                resp = await client.post(
                    url, headers=self._headers(),
                    files={"attachment": (filename, data)})
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitea_issues upload network: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"gitea_issues upload → {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitea_issues upload → {resp.status_code}: {resp.text[:300]}")
        meta = resp.json()
        return self._app_reachable_url(
            meta.get("browser_download_url") or meta.get("uuid") or "")

    async def download_asset(self, url: str) -> bytes:
        """Fetch an attachment; host allowlist before rewrite, no off-host
        redirects with auth (docs/14 §11)."""
        from ...domain.asset_fetch import (
            AssetUrlError, assert_downloadable_asset_url,
            resolve_redirect_location,
        )
        allowed = self._asset_allowed_hosts()
        # 1) refuse evil hosts on the ORIGINAL URL (before rewrite)
        try:
            url = assert_downloadable_asset_url(
                url, allowed_hosts=allowed, allow_http=True)
        except AssetUrlError as e:
            raise RuntimeError(f"gitea_issues download refused: {e}") from e
        # 2) rewrite only localhost/loopback → docker-network origin
        current = self._app_reachable_url(url)
        headers = self._headers()
        try:
            async with httpx.AsyncClient(
                    timeout=60, transport=self._transport,
                    follow_redirects=False) as client:
                for _ in range(5):
                    resp = await client.get(current, headers=headers)
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location") or ""
                        try:
                            nxt = resolve_redirect_location(current, loc)
                            # validate pre-rewrite host, then rewrite loopback
                            nxt = assert_downloadable_asset_url(
                                nxt, allowed_hosts=allowed, allow_http=True)
                        except AssetUrlError as e:
                            raise RuntimeError(
                                f"gitea_issues download redirect refused: {e}"
                            ) from e
                        current = self._app_reachable_url(nxt)
                        continue
                    break
                else:
                    raise RuntimeError("gitea_issues download: too many redirects")
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitea_issues download network: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"gitea_issues download → {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitea_issues download → {resp.status_code}: {resp.text[:200]}")
        return resp.content

    # ── meta ────────────────────────────────────────────────────────────────

    async def health_probe(self, team_ref: str) -> PMOHealth:
        try:
            self._apply_team(team_ref)
            if not self._owner:
                return PMOHealth(
                    ok=False, detail=f"invalid team_key {team_ref!r}: "
                                     f"need owner/repo")
            if not self._api:
                return PMOHealth(ok=False, detail="api_base is empty")
            await self.ensure_labels(team_ref, ALL_LABELS)
            await self._ensure_label_cache()
            present = sum(1 for n in ALL_LABELS if n.upper() in self._label_ids)
            # count only managed intersection on the repo
            raw_labels = await self._req("GET", self._repo_path("/labels"),
                                         params={"limit": 50})
            remote = {(lb.get("name") or "").upper() for lb in (raw_labels or [])}
            present = len(remote & {n.upper() for n in ALL_LABELS})
            return PMOHealth(
                ok=True,
                workspace=f"{self._owner}/{self._repo}",
                managed_labels_present=present,
                managed_labels_expected=len(ALL_LABELS),
                detail="",
            )
        except PMOTransient as e:
            return PMOHealth(ok=False, detail=str(e))
        except Exception as e:  # noqa: BLE001 — probe must never raise to admin UI
            return PMOHealth(ok=False, detail=str(e))

    def capabilities(self) -> PMOCapabilities:
        return PMOCapabilities(
            projects_supported=False,
            project_labels_supported=False,
            attachment_max_bytes=50 * 1024 * 1024,
            native_label_swap_atomic=True,  # PUT full label set
            relations_supported=True,
        )
