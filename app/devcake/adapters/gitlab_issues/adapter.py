"""GitLab Issues PMOPort adapter (docs/05 forge-issue family).

Separate from adapters.gitlab (ForgePort). team_key is path_with_namespace
(owner/repo or group/sub/repo). api_base is the GitLab origin reachable from
the app (default https://gitlab.com).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from ...domain.model import (ALL_LABELS, Activity, ActivityEntry, AttachmentRef,
                             Mission, MissionRef, NormalizedStatus,
                             canonicalize_labels)
from ...ports.pmo import PMOCapabilities, PMOHealth, PMOTransient
from .._toolkit import label_write_lock
from ..forge_issue import CANCEL_FOOTER, apply_cancel_footer, strip_cancel_footer
from .mapping import (mission_key, normalize_priority,
                      normalize_status, parse_team_ref, project_path_encoded)
from ..budget import RateSignal, budget_for, header_float, header_int


# GitLab's throttle headers describe a per-minute window (docs/05 §2a)
RATE_WINDOW_S = 60


def rate_signal(resp: httpx.Response) -> RateSignal:
    """GitLab's RateLimit-* headers (on every response; reset in epoch
    seconds) → the vendor-neutral signal (ADR-0040). A throttled request
    is a 429 carrying Retry-After."""
    h = resp.headers
    reset = header_int(h, "ratelimit-reset")
    return RateSignal(limit=header_int(h, "ratelimit-limit"),
                      remaining=header_int(h, "ratelimit-remaining"),
                      reset_at=float(reset) if reset else None,
                      window_s=RATE_WINDOW_S,
                      limited=resp.status_code == 429,
                      retry_after_s=header_float(h, "retry-after"))

log = logging.getLogger("devcake.gitlab_issues")


class GitLabHTTPError(RuntimeError):
    """Permanent GitLab Issues HTTP failure carrying the status code."""

    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(
            f"gitlab_issues {method} {path} → {status}: {body[:300]}")
        self.status_code = status

_LABEL_COLOR = "#6e40c9"
MAX_COMMENT_PAGES = 10
MAX_COMMENT_PAGES_FULL = 100
COMMENTS_PAGE = 50
ISSUES_PAGE = 50
MAX_ISSUE_PAGES = 40
LABELS_PAGE = 50
MAX_LABEL_PAGES = 10
DEFAULT_API_ORIGIN = "https://gitlab.com"


class GitLabIssuesAdapter:
    """PMOPort over GitLab Issues REST API (/api/v4). Issue-only missions."""

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
        from ..http import PooledClient
        self._http = PooledClient(timeout=30, transport=transport)
        origin = (api_base or "").strip().rstrip("/") or DEFAULT_API_ORIGIN
        if origin.endswith("/api/v4"):
            self._origin = origin[: -len("/api/v4")]
            self._api = origin
        else:
            self._origin = origin
            self._api = f"{origin}/api/v4"
        self._team_ref = (team_ref or "").strip()
        # ADR-0040: one request budget per credential on this API host
        self._budget = budget_for(urlsplit(self._api).hostname or "-", self._token,
                                  system="gitlab_issues", instance=instance)
        self._path = ""
        if self._team_ref:
            try:
                self._path = parse_team_ref(self._team_ref)
            except ValueError:
                self._path = ""
        self._label_names: set[str] = set()  # upper names known on the project
        # None = unprobed (capabilities True so the domain gate will try);
        # True/False latched on create_relation.
        self._relations_write: bool | None = None

    def _headers(self) -> dict[str, str]:
        if not self._token.strip():
            raise PMOTransient("gitlab_issues: API token missing")
        return {"PRIVATE-TOKEN": self._token}

    async def aclose(self) -> None:
        await self._http.aclose()

    def _proj(self, suffix: str = "") -> str:
        if not self._path:
            raise RuntimeError(
                f"gitlab_issues: invalid team_key {self._team_ref!r} "
                f"(need namespace/project)")
        return f"/projects/{project_path_encoded(self._path)}{suffix}"

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
            raise PMOTransient("gitlab_issues: api_base is empty")
        url = f"{self._api}{path}"
        hdrs = {**self._headers(), **(headers or {})}
        try:
            resp = await self._budget.request(          # THE wire call (ADR-0040)
                lambda: self._http.get().request(
                    method, url, params=params, json=json, content=content,
                    headers=hdrs),
                rate_signal, instance=self._instance)
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitlab_issues network: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"gitlab_issues {method} {path} → {resp.status_code}: "
                f"{resp.text[:200]}")
        if resp.status_code >= 400:
            raise GitLabHTTPError(
                method, path, resp.status_code, resp.text or "")
        if not expect_json or not resp.content:
            return resp.content if not expect_json else None
        return resp.json()

    def _require_issue(self, ref: MissionRef) -> None:
        if ref.kind != "issue":
            raise RuntimeError(
                "gitlab_issues: projects are not supported "
                f"(got kind={ref.kind!r})")

    def _apply_team(self, team_ref: str) -> None:
        ref = (team_ref or self._team_ref or "").strip()
        if ref and ref != self._team_ref:
            self._team_ref = ref
            self._path = parse_team_ref(ref)
            self._label_names.clear()
            self._relations_write = None

    def _label_set(self, issue: dict) -> set[str]:
        raw = issue.get("labels") or []
        names: list[str] = []
        for lb in raw:
            if isinstance(lb, str):
                names.append(lb)
            elif isinstance(lb, dict) and lb.get("name"):
                names.append(lb["name"])
        return canonicalize_labels(names)

    def _mission(self, issue: dict) -> Mission:
        iid = int(issue["iid"])
        body = issue.get("description") or ""
        labels = self._label_set(issue)
        updated = issue.get("updated_at") or issue.get("created_at") or ""
        try:
            updated_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            updated_at = datetime.now(timezone.utc)
        return Mission(
            pmo_id=str(iid),
            pmo_kind="issue",
            key=mission_key(self._path, iid),
            title=issue.get("title") or "",
            description=body,
            status=normalize_status(issue.get("state") or "opened", body),
            priority=normalize_priority(),
            labels=labels,
            updated_at=updated_at,
            url=issue.get("web_url") or "",
            parent_ref=None,
            blocked_by=[],
            instance=self._instance,
        )

    def _link_other_iid(self, link: dict, iid: int):
        """GitLab GET /issues/:iid/links puts the related issue at TOP level.

        Nested `target_issue` / `source_issue` is the create/get-single
        shape — accept it as a fallback, never require it.
        """
        other = link.get("iid")
        if other is None:
            nested = link.get("target_issue") or link.get("issue") or {}
            other = nested.get("iid") if isinstance(nested, dict) else None
        if other is None:
            return None
        try:
            other_i = int(other)
        except (TypeError, ValueError):
            return None
        if other_i == iid:
            src = (link.get("source_issue") or {}).get("iid")
            if src is not None and int(src) != iid:
                return int(src)
            return None
        return other_i

    async def _blocked_by_ids(self, iid: int) -> list[str]:
        try:
            links = await self._req(
                "GET", self._proj(f"/issues/{iid}/links"))
        except GitLabHTTPError as e:
            if e.status_code in (403, 404):
                return []
            raise
        if not isinstance(links, list):
            return []
        out: list[str] = []
        for link in links:
            lt = (link.get("link_type") or "").lower()
            if lt != "is_blocked_by":
                continue
            other = self._link_other_iid(link, iid)
            if other is not None:
                out.append(str(other))
        return out

    async def _enrich_blocked_by(self, mission: Mission) -> Mission:
        # Listing links is Free; writing blocking types is Premium. Reads
        # must not wait on a write probe (health_probe stays read-only).
        blockers = await self._blocked_by_ids(int(mission.pmo_id))
        return mission.model_copy(update={"blocked_by": blockers})

    async def _list_issues(self, *, state: str = "all") -> list[dict]:
        from .._toolkit import paginate_rest
        raw, _ = await paginate_rest(
            lambda page: self._req(
                "GET", self._proj("/issues"),
                params={"state": state, "per_page": ISSUES_PAGE, "page": page}),
            page_size=ISSUES_PAGE, max_pages=MAX_ISSUE_PAGES,
            what="gitlab_issues list_issues", on_ceiling="warn")
        return list(raw)

    async def list_missions(self, team_ref: str) -> list[Mission]:
        self._apply_team(team_ref)
        out: list[Mission] = []
        for raw in await self._list_issues(state="opened"):
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
            if m.status not in ("done", "canceled"):
                m = await self._enrich_blocked_by(m)
            out.append(m)
        return out

    async def get(self, ref: MissionRef) -> Mission:
        self._require_issue(ref)
        raw = await self._req("GET", self._proj(f"/issues/{ref.pmo_id}"))
        return await self._enrich_blocked_by(self._mission(raw))

    async def get_activity(self, ref: MissionRef,
                           full: bool = False) -> Activity:
        self._require_issue(ref)
        mission = await self.get(ref)
        from .._toolkit import paginate_rest
        max_pages = MAX_COMMENT_PAGES_FULL if full else MAX_COMMENT_PAGES
        raw_notes, truncated = await paginate_rest(
            lambda page: self._req(
                "GET", self._proj(f"/issues/{ref.pmo_id}/notes"),
                params={"page": page, "per_page": COMMENTS_PAGE,
                        "sort": "desc"}),
            page_size=COMMENTS_PAGE, max_pages=max_pages,
            what="gitlab_issues get_activity", on_ceiling="flag")
        if truncated:
            log.error("gitlab_issues get_activity truncated at %s pages",
                      max_pages)
        entries = [self._note_entry(n, full=full) for n in raw_notes
                   if not n.get("system")]
        entries.sort(key=lambda e: e.ts)
        mission_atts: list[AttachmentRef] = []
        if full:
            seen: set[str] = set()
            for att in self._attachments_from_body(mission.description):
                if att.url not in seen:
                    seen.add(att.url)
                    mission_atts.append(att)
            for e in entries:
                for att in e.attachments:
                    if att.url not in seen:
                        seen.add(att.url)
                        mission_atts.append(att)
        return Activity(mission=mission, entries=entries,
                        mission_attachments=mission_atts, truncated=truncated)

    def _note_entry(self, n: dict, *, full: bool) -> ActivityEntry:
        body = n.get("body") or ""
        created = n.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(timezone.utc)
        author = ((n.get("author") or {}).get("username")
                  or (n.get("author") or {}).get("name") or "unknown")
        return ActivityEntry(
            ts=ts, author=author, kind="comment", body=body,
            attachments=self._attachments_from_body(body),
            entry_id=str(n["id"]) if full and n.get("id") is not None else None,
            parent_id=None,
        )

    def _attachments_from_body(self, body: str) -> list[AttachmentRef]:
        import re
        found: list[AttachmentRef] = []
        seen: set[str] = set()

        def _add(name: str, url: str) -> None:
            if ".." in url or url in seen:
                return
            seen.add(url)
            found.append(AttachmentRef(url=url, name=name, kind="file"))

        for name, secret, fname in re.findall(
                r"\[([^\]]+)\]\(/uploads/([A-Fa-f0-9]+)/([^)]+)\)",
                body or ""):
            if ".." in fname or "/" in fname:
                continue
            _add(name, f"{self._api}{self._proj(f'/uploads/{secret}/{fname}')}")
        for name, url in re.findall(
                r"\[([^\]]+)\]\((https?://[^)\s]+/api/v4/projects/[^)]+/uploads/[^)]+)\)",
                body or ""):
            _add(name, url)
        return found

    async def children_of(self, ref: MissionRef) -> list[Mission]:
        self._require_issue(ref)
        # Issue-only: no project children. Decomposition uses markers + relations.
        return []

    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        self._require_issue(ref)
        await self._req(
            "POST", self._proj(f"/issues/{ref.pmo_id}/notes"),
            json={"body": markdown})

    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None:
        self._require_issue(ref)
        if status in ("done", "canceled"):
            state_event = "close"
        else:
            state_event = "reopen"
        body_patch: dict[str, Any] = {"state_event": state_event}
        cur = await self._req("GET", self._proj(f"/issues/{ref.pmo_id}"))
        body = cur.get("description") or ""
        if status == "canceled":
            body_patch["description"] = apply_cancel_footer(body)
        elif CANCEL_FOOTER in body:
            body_patch["description"] = strip_cancel_footer(body)
        await self._req(
            "PUT", self._proj(f"/issues/{ref.pmo_id}"), json=body_patch)

    async def cancel_mission(self, ref: MissionRef) -> None:
        self._require_issue(ref)
        cur = await self._req("GET", self._proj(f"/issues/{ref.pmo_id}"))
        state = (cur.get("state") or "").lower()
        if state == "closed" and CANCEL_FOOTER in (cur.get("description") or ""):
            return
        await self.set_status(ref, "canceled")

    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None:
        # Read-modify-write over the full label set: hold the per-mission
        # lock so a concurrent swap's read never goes stale mid-write
        # (the CAKE-48 clobber — see _toolkit.label_write_lock).
        self._require_issue(ref)
        async with label_write_lock(ref.pmo_id):
            await self.ensure_labels(self._team_ref, add)
            cur = await self._req("GET", self._proj(f"/issues/{ref.pmo_id}"))
            current = self._label_set(cur)
            next_names = (current - remove) | add
            missing = [n for n in next_names
                       if n.upper() not in self._label_names]
            if missing:
                raise RuntimeError(
                    f"label {missing[0]} missing — ensure_labels not run?")
            # GitLab PUT replaces the full label set by *name*.
            await self._req(
                "PUT", self._proj(f"/issues/{ref.pmo_id}"),
                json={"labels": ",".join(sorted(next_names))})

    async def create_mission(
        self, team_ref: str, title: str, description: str,
        priority: str, label_names: set[str],
        parent_ref: Optional[str] = None,
    ) -> tuple[str, str]:
        self._apply_team(team_ref)
        await self.ensure_labels(team_ref, label_names)
        _ = parent_ref
        _ = priority
        raw = await self._req(
            "POST", self._proj("/issues"),
            json={"title": title, "description": description or "",
                  "labels": ",".join(sorted(label_names))})
        iid = int(raw["iid"])
        return mission_key(self._path, iid), str(iid)

    async def create_relation(self, blocker_id: str, blocked_id: str) -> None:
        # Writing blocks/is_blocked_by is Premium. A 403 here is "unsupported",
        # not a finalize-escaping error — Free boards must no-op.
        proj = await self._req("GET", self._proj(""))
        target_pid = proj.get("id")
        try:
            await self._req(
                "POST", self._proj(f"/issues/{int(blocked_id)}/links"),
                json={"target_project_id": target_pid,
                      "target_issue_iid": int(blocker_id),
                      "link_type": "is_blocked_by"})
        except GitLabHTTPError as e:
            if e.status_code == 409:
                self._relations_write = True
                return
            if e.status_code == 403:
                self._relations_write = False
                return
            raise
        self._relations_write = True

    async def _fetch_all_labels(self) -> list[dict]:
        from .._toolkit import paginate_rest
        labels, _ = await paginate_rest(
            lambda page: self._req(
                "GET", self._proj("/labels"),
                params={"page": page, "per_page": LABELS_PAGE}),
            page_size=LABELS_PAGE, max_pages=MAX_LABEL_PAGES,
            what="gitlab_issues labels", on_ceiling="raise",
            ceiling_error=(
                f"gitlab_issues: more than {MAX_LABEL_PAGES * LABELS_PAGE} "
                f"labels on {self._path} — refusing a truncated rewrite"))
        return labels

    async def ensure_labels(self, team_ref: str, names: set[str]) -> None:
        self._apply_team(team_ref)
        existing = await self._fetch_all_labels()
        by_upper = {(lb.get("name") or "").upper(): lb for lb in existing}
        self._label_names = set(by_upper)
        for name in names:
            u = name.upper()
            if u in by_upper:
                continue
            created = await self._req(
                "POST", self._proj("/labels"),
                json={"name": name.upper(), "color": _LABEL_COLOR})
            by_upper[u] = created
            self._label_names.add(u)

    async def append_description(self, ref: MissionRef, text: str) -> None:
        self._require_issue(ref)
        cur = await self._req("GET", self._proj(f"/issues/{ref.pmo_id}"))
        body = (cur.get("description") or "") + text
        await self._req(
            "PUT", self._proj(f"/issues/{ref.pmo_id}"),
            json={"description": body})

    async def upload_attachment(self, pmo_id: str, filename: str,
                                data: bytes) -> str:
        _ = pmo_id  # GitLab uploads are project-scoped, not issue-scoped
        if not self._api:
            raise PMOTransient("gitlab_issues: api_base is empty")
        url = f"{self._api}{self._proj('/uploads')}"
        try:
            async with httpx.AsyncClient(
                    timeout=60, transport=self._transport) as client:
                resp = await self._budget.request(   # same bucket as _req
                    lambda: client.post(
                        url, headers=self._headers(),
                        files={"file": (filename, data)}),
                    rate_signal, instance=self._instance,
                    op="gitlab_issues upload → ")
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitlab_issues upload network: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"gitlab_issues upload → {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitlab_issues upload → {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        secret_url = payload.get("url") or ""
        # /uploads/<secret>/<filename>
        parts = secret_url.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "uploads":
            raise RuntimeError(
                f"gitlab_issues upload: unexpected url {secret_url!r}")
        secret, fname = parts[1], parts[2]
        return f"{self._api}{self._proj(f'/uploads/{secret}/{fname}')}"

    def _finalize_upload_url(self, url: str) -> str:
        """Pin credentialed GETs to this project's /uploads/ secret/fname."""
        from urllib.parse import unquote
        from ...domain.asset_fetch import (
            AssetUrlError, assert_fetch_netloc)
        current = url.strip()
        assert_fetch_netloc(current, self._api)
        path = unquote(urlsplit(current).path or "")
        if ".." in path.split("/"):
            raise AssetUrlError("asset path must not contain ..")
        prefix = f"/api/v4/projects/{project_path_encoded(self._path)}/uploads/"
        if "/uploads/" not in path:
            raise AssetUrlError(f"asset path {path!r} is not an upload")
        # Accept the encoded project path we emit, or the decoded form
        # GitLab sometimes puts in markdown.
        decoded_prefix = f"/api/v4/projects/{self._path}/uploads/"
        if not (path.startswith(prefix) or path.startswith(decoded_prefix)):
            raise AssetUrlError(
                f"asset path {path!r} is not this project's upload")
        after = path.split("/uploads/", 1)[1]
        segs = [s for s in after.split("/") if s]
        if len(segs) != 2:
            raise AssetUrlError(
                f"upload path must be secret/filename, got {after!r}")
        return current

    async def download_asset(self, url: str) -> bytes:
        if not self._api:
            raise RuntimeError("gitlab_issues: api_base is empty")
        from ...domain.asset_fetch import (
            AssetUrlError, assert_downloadable_asset_url)
        from .._toolkit import fetch_following_safe_redirects
        allowed = {h for h in (
            urlsplit(self._origin).hostname,
            urlsplit(self._api).hostname,
        ) if h}
        try:
            url = assert_downloadable_asset_url(
                url, allowed_hosts=allowed,
                allow_http=self._origin.startswith("http://"))
            url = self._finalize_upload_url(url)
        except AssetUrlError as e:
            raise RuntimeError(f"gitlab_issues download refused: {e}") from e
        try:
            async with httpx.AsyncClient(
                    timeout=60, transport=self._transport,
                    follow_redirects=False) as client:
                resp = await self._budget.request(   # same bucket as _req
                    lambda: fetch_following_safe_redirects(
                        client, url, allowed_hosts=allowed,
                        headers=self._headers(),
                        allow_http=self._origin.startswith("http://"),
                        pin=self._finalize_upload_url),
                    rate_signal, instance=self._instance,
                    op="gitlab_issues download → ")
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitlab_issues download network: {e}") from e
        except AssetUrlError as e:
            raise RuntimeError(f"gitlab_issues download refused: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"gitlab_issues download → {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitlab_issues download → {resp.status_code}: {resp.text[:200]}")
        from ...domain.asset_fetch import (
            AssetUrlError, enforce_download_byte_cap)
        try:
            return enforce_download_byte_cap(
                resp.content,
                content_length=resp.headers.get("content-length"),
                max_bytes=self.capabilities().attachment_max_bytes,
            )
        except AssetUrlError as e:
            raise RuntimeError(f"gitlab_issues download refused: {e}") from e

    async def health_probe(self, team_ref: str) -> PMOHealth:
        self._apply_team(team_ref)
        if not self._path:
            return PMOHealth(ok=False, workspace=team_ref or self._team_ref,
                             detail="invalid team_key")
        try:
            await self._req("GET", self._proj(""))
            labels = await self._fetch_all_labels()
        except PMOTransient as e:
            return PMOHealth(ok=False, workspace=self._path, detail=str(e))
        except RuntimeError as e:
            return PMOHealth(ok=False, workspace=self._path, detail=str(e))
        present = {(lb.get("name") or "").upper() for lb in labels}
        managed = {n.upper() for n in ALL_LABELS}
        if self._relations_write is None:
            rel = "unprobed"
        elif self._relations_write:
            rel = "on"
        else:
            rel = "off"
        return PMOHealth(
            ok=True, workspace=self._path,
            managed_labels_present=len(present & managed),
            managed_labels_expected=len(ALL_LABELS),
            detail=f"relations={rel}",
        )

    def capabilities(self) -> PMOCapabilities:
        return PMOCapabilities(
            projects_supported=False,
            project_labels_supported=False,
            attachment_max_bytes=10 * 1024 * 1024,
            native_label_swap_atomic=True,
            # Unprobed means "try the write" — False only after a 403.
            relations_supported=self._relations_write is not False,
            attachments_supported=True,
            global_ids=False,
        )
