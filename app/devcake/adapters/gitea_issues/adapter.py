"""Gitea Issues PMOPort adapter (docs/05 forge-issue family).

Separate from adapters.gitea (ForgePort): own HTTP client, own token, own
package id `gitea_issues`. team_key is owner/repo; api_base is the Gitea
origin reachable from the app (internal: http://gitea:3000).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from ...domain.model import (ALL_LABELS, Activity, ActivityEntry, AttachmentRef,
                             Mission, MissionRef, NormalizedStatus,
                             canonicalize_labels)
from ...ports.pmo import PMOCapabilities, PMOHealth, PMOTransient
from .._toolkit import gitea_internal_tracker_enable_deps, label_write_lock
from ..forge_issue import CANCEL_FOOTER, apply_cancel_footer, strip_cancel_footer
from .mapping import (mission_key, normalize_priority,
                      normalize_status, parse_team_ref)
from ..budget import RateSignal, budget_for, header_float



def rate_signal(resp: httpx.Response) -> RateSignal:
    """Gitea publishes no quota headers; a reverse proxy in front of it may
    still 429 with Retry-After. Unlimited until such a rejection (ADR-0040)."""
    return RateSignal(limited=resp.status_code == 429,
                      retry_after_s=header_float(resp.headers, "retry-after"))

log = logging.getLogger("devcake.gitea_issues")

# Managed-label colors (cosmetic; names are the contract)
_LABEL_COLOR = "6e40c9"


class GiteaIssuesHTTPError(RuntimeError):
    """Permanent Gitea Issues HTTP failure carrying the status code."""

    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(
            f"gitea_issues {method} {path} → {status}: {body[:300]}")
        self.status_code = status


# get_activity ceilings (mirror Linear: fail-loud, newest-first survival)
MAX_COMMENT_PAGES = 10
MAX_COMMENT_PAGES_FULL = 100
COMMENTS_PAGE = 50
ISSUES_PAGE = 50
MAX_ISSUE_PAGES = 40  # 2000 issues fail-loud
LABELS_PAGE = 50
MAX_LABEL_PAGES = 10  # 500 labels fail-loud — see _fetch_all_labels
DEPS_PAGE = 50
MAX_DEP_PAGES = 10  # 500 blockers fail-loud ceiling (Linear relations twin)

# Attachment host patterns for markdown link recovery
_NAMED_ASSET_RE = re.compile(
    r"\[([^\]]+\.\w{1,8})\]\((https?://[^\s)]+/attachments/[^\s)]+)\)")
_BARE_ASSET_RE = re.compile(r"(https?://[^\s)]+/attachments/[A-Za-z0-9-]+)")

# Browser-facing loopback names for bundled ROOT_URL (localhost:3300). The
# app reaches the container as api_base (gitea:3000). Presentation hosts also
# include GITEA_UI_URL (same signal as internal forge) and the api_base host.
# Foreign hosts are never rewritten onto the origin (docs/14 §11).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Credentialed download_asset path pin — Gitea attachment bytes, not the API.
_ATTACHMENT_PATH_PREFIX = "/attachments/"


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
        from ..http import PooledClient
        self._http = PooledClient(timeout=30, transport=transport)  # F16: keep-alive
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
        # ADR-0040: one request budget per credential on this API host (Gitea
        # publishes no quota headers — paced only after a proxy 429)
        self._budget = budget_for(urlsplit(self._api).hostname or "-", self._token,
                                  system="gitea_issues", instance=instance)
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

    def _presentation_hosts(self) -> set[str]:
        """Hosts that may appear on ticket / browser_download_url.

        Fetch origin (api_base) + operator UI (GITEA_UI_URL, default
        localhost:3300) + loopback. Humans still use Gitea out of band;
        this only teaches the app which faces belong to *this* Gitea.
        """
        hosts = set(_LOOPBACK_HOSTS)
        ui = (os.environ.get("GITEA_UI_URL") or "http://localhost:3300").strip()
        for base in (self._origin, self._api, ui):
            if not base:
                continue
            h = urlsplit(base).hostname
            if h:
                hosts.add(h.lower())
        return hosts

    def _app_reachable_url(self, url: str) -> str:
        """Rewrite presentation hosts to the configured api_base origin.

        Self-hosted Gitea returns attachment URLs with ROOT_URL /
        GITEA_UI_URL which may be unreachable from the app container; the
        docker-network origin (http://gitea:3000) is.

        Only presentation hosts are rewritten — foreign hosts are left
        unchanged so the download allowlist can refuse them (docs/14 §11).
        """
        if not url or not self._origin:
            return url
        parts = urlsplit(url)
        origin_parts = urlsplit(self._origin)
        if not parts.scheme or not parts.netloc:
            return url
        host = (parts.hostname or "").lower()
        if host not in self._presentation_hosts():
            return url
        if parts.netloc.lower() == origin_parts.netloc.lower():
            return url
        return urlunsplit((
            origin_parts.scheme, origin_parts.netloc,
            parts.path, parts.query, parts.fragment,
        ))

    def _operator_url(self, url: str) -> str:
        """Inverse of `_app_reachable_url`: docker-network origin → UI host.

        Gitea `html_url` is built from ROOT_URL, which on a bundled stack is
        the container name (`http://gitea:3000/...`). Operators click from
        the browser, so mission.url must use GITEA_UI_URL.
        """
        if not url or not self._origin:
            return url
        ui = (os.environ.get("GITEA_UI_URL") or "http://localhost:3300").strip()
        ui_parts = urlsplit(ui)
        if not ui_parts.scheme or not ui_parts.netloc:
            return url
        parts = urlsplit(url)
        origin_parts = urlsplit(self._origin)
        if not parts.scheme or not parts.netloc:
            return url
        if parts.netloc.lower() != origin_parts.netloc.lower():
            return url
        return urlunsplit((
            ui_parts.scheme, ui_parts.netloc,
            parts.path, parts.query, parts.fragment,
        ))

    def _asset_allowed_hosts(self) -> set[str]:
        """Hosts permitted on download_asset URLs (presentation set)."""
        return self._presentation_hosts()

    def _finalize_fetch_url(self, url: str) -> str:
        """Rewrite presentation → origin, then pin netloc + attachment path."""
        from ...domain.asset_fetch import (
            assert_fetch_netloc, assert_path_prefix,
        )
        current = self._app_reachable_url(url)
        assert_fetch_netloc(current, self._origin)
        assert_path_prefix(current, _ATTACHMENT_PATH_PREFIX)
        return current

    # ── HTTP ────────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        if not self._token.strip():
            raise PMOTransient("gitea_issues: API token missing")
        return {"Authorization": f"token {self._token}"}

    async def aclose(self) -> None:
        await self._http.aclose()

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
        as_response: bool = False,
    ) -> Any:
        if not self._api:
            raise PMOTransient("gitea_issues: api_base is empty")
        url = f"{self._api}{path}"
        hdrs = {**self._headers(), **(headers or {})}
        try:
            resp = await self._budget.request(          # THE wire call (ADR-0040)
                lambda: self._http.get().request(   # pooled (F16)
                    method, url, params=params, json=json, content=content,
                    headers=hdrs),
                rate_signal, instance=self._instance)
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
            raise GiteaIssuesHTTPError(
                method, path, resp.status_code, resp.text or "")
        if as_response:
            return resp
        if not expect_json or not resp.content:
            return resp.content if not expect_json else None
        return resp.json()

    def _repo_path(self, suffix: str = "") -> str:
        if not self._owner or not self._repo:
            raise RuntimeError(
                f"gitea_issues: invalid team_key {self._team_ref!r} "
                f"(need owner/repo)")
        return f"/repos/{self._owner}/{self._repo}{suffix}"

    def _require_issue(self, ref: MissionRef) -> None:
        """projects_supported=False: a project-kind ref reaching this adapter
        is a caller bug. Raise (permanent family) — never fabricate a Mission
        or silently no-op a write, which would report success while swapping
        no labels and setting no status (2026-08-12 audit F1)."""
        if ref.kind != "issue":
            raise RuntimeError(
                "gitea_issues: projects are not supported "
                f"(got kind={ref.kind!r})")

    # ── normalization ───────────────────────────────────────────────────────

    def _mission(self, issue: dict) -> Mission:
        number = int(issue["number"])
        body = issue.get("body") or ""
        labels = canonicalize_labels(
            lb.get("name") or "" for lb in (issue.get("labels") or []))
        # blocked_by filled by callers that fetch dependencies
        updated = issue.get("updated_at") or issue.get("created_at") or ""
        try:
            updated_at = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            updated_at = datetime.now(timezone.utc)
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
            url=self._operator_url(issue.get("html_url") or ""),
            parent_ref=None,
            blocked_by=[],
            instance=self._instance,
        )

    async def _blocked_by_ids(self, index: int) -> list[str]:
        """Issue numbers (as pmo_id strings) that block this issue.

        Paged with a warn ceiling (Linear inverseRelations / forge list twin).
        Only HTTP 404 (deps endpoint absent / disabled) yields []; other
        permanent failures raise — never match on the path substring
        ``depend`` (always present) or bare ``404`` in the issue index.
        """
        from .._toolkit import paginate_rest
        try:
            deps, _ = await paginate_rest(
                lambda page: self._req(
                    "GET",
                    self._repo_path(f"/issues/{index}/dependencies"),
                    params={"page": page, "limit": DEPS_PAGE}),
                page_size=DEPS_PAGE, max_pages=MAX_DEP_PAGES,
                what=f"gitea_issues issue {index} dependencies",
                on_ceiling="warn")
        except GiteaIssuesHTTPError as e:
            if e.status_code == 404:
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
        from .._toolkit import paginate_rest
        raw, _ = await paginate_rest(
            lambda page: self._req(
                "GET", self._repo_path("/issues"),
                params={"state": state, "type": "issues",
                        "page": page, "limit": ISSUES_PAGE}),
            page_size=ISSUES_PAGE, max_pages=MAX_ISSUE_PAGES,
            what="gitea_issues list_issues", on_ceiling="warn")
        # Gitea may include PRs in /issues — keep pure issues only
        return [it for it in raw if not it.get("pull_request")]

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
            # blocked_by enrichment costs one GET per issue per poll cycle —
            # spend it on OPEN issues only: the scheduler's gate reads a
            # BLOCKER's status off its own list row, never a closed issue's
            # blocked_by, and set_status deletes dependencies on close. The
            # old unconditional enrichment grew monotonically with history
            # (2026-08-12 audit F9).
            if m.status not in ("done", "canceled"):
                m = await self._enrich_blocked_by(m)
            out.append(m)
        return out

    def _apply_team(self, team_ref: str) -> None:
        ref = (team_ref or self._team_ref or "").strip()
        if ref and ref != self._team_ref:
            self._team_ref = ref
            self._owner, self._repo = parse_team_ref(ref)
            self._label_ids.clear()

    async def get(self, ref: MissionRef) -> Mission:
        self._require_issue(ref)
        raw = await self._req(
            "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        return await self._enrich_blocked_by(self._mission(raw))

    async def get_activity(self, ref: MissionRef,
                           full: bool = False) -> Activity:
        # The port's "shallow project ref never raises" clause is scoped to
        # projects_supported vendors — here a project ref is a caller bug,
        # and the old fabricated Mission answer made misroutes invisible.
        self._require_issue(ref)
        mission = await self.get(ref)
        from .._toolkit import last_page_from_headers, paginate_rest_newest
        max_pages = MAX_COMMENT_PAGES_FULL if full else MAX_COMMENT_PAGES

        async def _comments_page(page: int):
            resp = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}/comments"),
                params={"page": page, "limit": COMMENTS_PAGE},
                as_response=True)
            items = resp.json() if resp.content else []
            last = last_page_from_headers(resp.headers, page_size=COMMENTS_PAGE)
            return items or [], last

        raw_comments, truncated = await paginate_rest_newest(
            _comments_page,
            page_size=COMMENTS_PAGE, max_pages=max_pages,
            what="gitea_issues get_activity", on_ceiling="flag")
        if truncated:
            # caller-owned disclosure ('flag'): the freshness gate reads
            # Activity.truncated as material-unknown (ADR-0031 D2)
            log.error("gitea_issues get_activity truncated at %s pages",
                      max_pages)
        entries: list[ActivityEntry] = [
            self._comment_entry(c, full=full) for c in raw_comments]
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
            ts = datetime.now(timezone.utc)
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
        self._require_issue(ref)
        # Issue-only: no project children. Decomposition uses markers + relations.
        return []

    # ── writes ──────────────────────────────────────────────────────────────

    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        self._require_issue(ref)
        await self._req(
            "POST", self._repo_path(f"/issues/{ref.pmo_id}/comments"),
            json={"body": markdown})

    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None:
        self._require_issue(ref)
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
            body_patch["body"] = apply_cancel_footer(body)
        elif status in ("backlog", "in_progress", "done"):
            # opening or completing: strip cancel footer if present so done ≠ canceled
            cur = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
            body = cur.get("body") or ""
            if CANCEL_FOOTER in body:
                body_patch["body"] = strip_cancel_footer(body)
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
        self._require_issue(ref)
        cur = await self._req(
            "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        if (cur.get("state") == "closed"
                and CANCEL_FOOTER in (cur.get("body") or "")):
            return  # idempotent
        await self.set_status(ref, "canceled")

    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None:
        # Read-modify-write over the full label set: hold the per-mission
        # lock so a concurrent swap's read never goes stale mid-write
        # (the CAKE-48 clobber — see _toolkit.label_write_lock).
        self._require_issue(ref)
        async with label_write_lock(ref.pmo_id):
            await self._swap_labels_locked(ref, remove, add)

    async def _swap_labels_locked(self, ref: MissionRef, remove: set[str],
                                  add: set[str]) -> None:
        await self._ensure_label_cache()
        current = await self._issue_label_names(ref.pmo_id)
        next_names = (current - remove) | add
        ids = []
        for name in next_names:
            lid = self._label_ids.get(name.upper())
            if lid is None:
                await self.ensure_labels(self._team_ref, {name})
                lid = self._label_ids.get(name.upper())
            if lid is None:
                # NEVER PUT a set assembled from unresolved names: the PUT
                # replaces the issue's FULL label set, so dropping the id
                # here silently erases the label from the issue (2026-08-12
                # audit F8 — Linear's write path refuses the same way).
                raise RuntimeError(f"label {name} missing — ensure_labels not run?")
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
        missing = [n for n in label_names if n.upper() not in self._label_ids]
        if missing:
            raise RuntimeError(
                f"label {missing[0]} missing — ensure_labels not run?")
        label_ids = [self._label_ids[n.upper()] for n in label_names]
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

    async def _issue_label_names(self, pmo_id: str) -> set[str]:
        """The issue's current labels, paged. The issue GET embed is not
        a pagination contract — a truncated embed feeding a full-set PUT
        is the F8 twin of the registry one-page read."""
        from .._toolkit import paginate_rest
        labels, _ = await paginate_rest(
            lambda page: self._req(
                "GET", self._repo_path(f"/issues/{pmo_id}/labels"),
                params={"page": page, "limit": LABELS_PAGE}),
            page_size=LABELS_PAGE, max_pages=MAX_LABEL_PAGES,
            what=f"gitea_issues issue {pmo_id} labels", on_ceiling="raise",
            ceiling_error=(
                f"gitea_issues: issue {pmo_id} has more than "
                f"{MAX_LABEL_PAGES * LABELS_PAGE} labels — refusing a "
                f"full-set rewrite from a truncated read"))
        return canonicalize_labels(lb.get("name") or "" for lb in labels)

    async def _fetch_all_labels(self) -> list[dict]:
        """Every label on the repo, paginated with a fail-loud ceiling.

        swap_labels PUT-replaces the issue's FULL label set, so any rewrite
        computed from a truncated registry read silently erases labels past
        the page boundary from the vendor system (2026-08-12 audit F8 — the
        truncated-rewrite destruction class; this is the REST twin of Linear's
        `_refuse_truncated_rewrite`). Raise at the ceiling, never truncate:
        every consumer of this read feeds a full-set rewrite, a create
        decision, or an ensure that would mint an uppercase duplicate."""
        from .._toolkit import paginate_rest
        labels, _ = await paginate_rest(
            lambda page: self._req("GET", self._repo_path("/labels"),
                                   params={"page": page,
                                           "limit": LABELS_PAGE}),
            page_size=LABELS_PAGE, max_pages=MAX_LABEL_PAGES,
            what="gitea_issues labels", on_ceiling="raise",
            ceiling_error=(
                f"gitea_issues: more than {MAX_LABEL_PAGES * LABELS_PAGE} "
                f"labels on {self._owner}/{self._repo} — refusing (a label "
                f"past the ceiling would be silently erased by the next "
                f"full-set label rewrite)"))
        return labels

    async def ensure_labels(self, team_ref: str, names: set[str]) -> None:
        self._apply_team(team_ref)
        await self._enable_issue_dependencies()
        existing = await self._fetch_all_labels()
        by_upper = {(lb.get("name") or "").upper(): lb
                    for lb in existing}
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
        for lb in await self._fetch_all_labels():
            n = (lb.get("name") or "").upper()
            if n:
                self._label_ids[n] = int(lb["id"])

    async def _enable_issue_dependencies(self) -> None:
        """Dependencies are off by default on new repos (spike 1.24.7).

        Gitea replaces the whole ``internal_tracker`` (plain bools) on PATCH —
        omitted keys become false. GET the current tracker, then PATCH with
        deps enabled and time-tracker flags preserved
        (``gitea_internal_tracker_enable_deps``).
        """
        try:
            repo = await self._req("GET", self._repo_path(""))
            cur = (repo or {}).get("internal_tracker") if isinstance(
                repo, dict) else None
            await self._req(
                "PATCH", self._repo_path(""),
                json={"internal_tracker":
                      gitea_internal_tracker_enable_deps(cur)})
        except Exception as e:  # noqa: BLE001 — best-effort enable; create_relation may still work
            log.warning("gitea_issues enable dependencies: %s", e)

    async def append_description(self, ref: MissionRef, text: str) -> None:
        self._require_issue(ref)
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
                resp = await self._budget.request(   # same bucket as _req
                    lambda: client.post(
                        url, headers=self._headers(),
                        files={"attachment": (filename, data)}),
                    rate_signal, instance=self._instance,
                    op="gitea_issues upload → ")
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
        """Fetch an attachment; presentation allowlist, rewrite onto api_base,
        path/netloc pin, size cap, no off-allowlist redirects with auth
        (docs/14 §11)."""
        from ...domain.asset_fetch import (
            AssetUrlError, assert_downloadable_asset_url,
            enforce_download_byte_cap,
        )
        if not self._origin:
            raise RuntimeError(
                "gitea_issues download refused: api_base is empty")
        allowed = self._asset_allowed_hosts()
        try:
            # 1) refuse evil hosts on the ORIGINAL URL (before rewrite)
            url = assert_downloadable_asset_url(
                url, allowed_hosts=allowed, allow_http=True)
            # 2) presentation → origin; pin netloc + /attachments/ path
            current = self._finalize_fetch_url(url)
        except AssetUrlError as e:
            raise RuntimeError(f"gitea_issues download refused: {e}") from e
        headers = self._headers()
        cap = self.capabilities().attachment_max_bytes
        try:
            # THE credentialed redirect walk (adapters/_toolkit, ADR-0034):
            # allow_http=True is deliberate — self-hosted Gitea origins are
            # legitimately plain-http (the policy difference vs Linear is
            # stated at the call sites now, not drifted between copies).
            # `pin` re-applies the presentation→origin rewrite + netloc/path
            # pin per hop.
            from .._toolkit import fetch_following_safe_redirects
            async with httpx.AsyncClient(
                    timeout=60, transport=self._transport,
                    follow_redirects=False) as client:
                try:
                    resp = await self._budget.request(   # same bucket as _req
                        lambda: fetch_following_safe_redirects(
                            client, current, allowed_hosts=allowed,
                            headers=headers, allow_http=True,
                            pin=self._finalize_fetch_url),
                        rate_signal, instance=self._instance,
                        op="gitea_issues download → ")
                except AssetUrlError as e:
                    raise RuntimeError(
                        f"gitea_issues download redirect refused: {e}"
                    ) from e
                except RuntimeError:
                    raise RuntimeError(
                        "gitea_issues download: too many redirects")
        except httpx.HTTPError as e:
            raise PMOTransient(f"gitea_issues download network: {e}") from e
        if resp.status_code in (429, 500, 502, 503, 504):
            raise PMOTransient(
                f"gitea_issues download → {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"gitea_issues download → {resp.status_code}: {resp.text[:200]}")
        try:
            return enforce_download_byte_cap(
                resp.content,
                content_length=resp.headers.get("content-length"),
                max_bytes=cap,
            )
        except AssetUrlError as e:
            raise RuntimeError(f"gitea_issues download refused: {e}") from e

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
            # READ-ONLY (PMOHealth contract; 2026-08-12 audit F3): the
            # old probe ran ensure_labels — a repo-settings PATCH + label
            # POSTs fired at the SPA's 10 s /health cadence, forever. The
            # healing now rides the poll cycle's per-config-generation
            # once-latch (poll.poll_instance); the probe only reads.
            raw_labels = await self._fetch_all_labels()
            remote = {(lb.get("name") or "").upper() for lb in raw_labels}
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
            attachments_supported=True,
            global_ids=False,  # issue numbers collide across Gitea instances
        )
