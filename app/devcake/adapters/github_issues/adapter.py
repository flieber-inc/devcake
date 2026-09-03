"""GitHub Issues PMOPort adapter (docs/05 forge-issue family).

Separate from adapters.github (ForgePort). team_key is owner/repo. No
official issue-file upload API (2026-08-15 spike) — attachments_supported
is False; upload_attachment raises; the feed chokepoint posts inline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from urllib.parse import urlsplit

import httpx

from ...domain.model import (ALL_LABELS, Activity, ActivityEntry,
                             Mission, MissionRef, NormalizedStatus,
                             canonicalize_labels)
from ...ports.pmo import PMOCapabilities, PMOHealth, PMOTransient
from .._toolkit import label_write_lock
from ..forge_issue import CANCEL_FOOTER, apply_cancel_footer, strip_cancel_footer
from .mapping import (mission_key, normalize_priority,
                      normalize_status, parse_team_ref)
from ..budget import RateSignal, budget_for, header_float, header_int


# GitHub meters the REST API per user per hour (docs/05 §2a)
RATE_WINDOW_S = 3600


def rate_signal(resp: httpx.Response) -> RateSignal:
    """GitHub's primary-limit headers (x-ratelimit-*, reset in epoch seconds)
    → the vendor-neutral signal (ADR-0040). Only the `core` resource meters
    the Issues API. A 403 whose body names the rate limit is a rejection; a
    secondary-limit rejection (retry-after present, or the body says so) is
    honoured for at least 60 s, per GitHub's own guidance."""
    h = resp.headers
    body = (resp.text or "").lower()
    limited = resp.status_code == 429 or (
        resp.status_code == 403 and "rate limit" in body)
    retry_after = header_float(h, "retry-after")
    if limited and (retry_after is not None or "secondary" in body):
        retry_after = max(retry_after or 0.0, 60.0)
    limit = remaining = None
    reset_at = None
    if (h.get("x-ratelimit-resource") or "core").lower() == "core":
        limit = header_int(h, "x-ratelimit-limit")
        remaining = header_int(h, "x-ratelimit-remaining")
        reset = header_int(h, "x-ratelimit-reset")
        reset_at = float(reset) if reset else None
    return RateSignal(limit=limit, remaining=remaining, reset_at=reset_at,
                      window_s=RATE_WINDOW_S, limited=limited,
                      retry_after_s=retry_after)

log = logging.getLogger("devcake.github_issues")

_LABEL_COLOR = "6e40c9"
MAX_COMMENT_PAGES = 10
MAX_COMMENT_PAGES_FULL = 100
COMMENTS_PAGE = 50
ISSUES_PAGE = 50
MAX_ISSUE_PAGES = 40
LABELS_PAGE = 50
MAX_LABEL_PAGES = 10
DEFAULT_API = "https://api.github.com"


class GitHubIssuesAdapter:
    """PMOPort over GitHub Issues REST API. Issue-only missions."""

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
        base = (api_base or "").strip().rstrip("/") or DEFAULT_API
        self._api = base
        self._team_ref = (team_ref or "").strip()
        self._owner, self._repo = ("", "")
        if self._team_ref:
            try:
                self._owner, self._repo = parse_team_ref(self._team_ref)
            except ValueError:
                self._owner, self._repo = ("", "")
        self._label_names: set[str] = set()
        # ADR-0040: one request budget per credential on this API host
        self._budget = budget_for(urlsplit(self._api).hostname or "-", self._token,
                                  system="github_issues", instance=instance)

    def _headers(self) -> dict[str, str]:
        if not self._token.strip():
            raise PMOTransient("github_issues: API token missing")
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def aclose(self) -> None:
        await self._http.aclose()

    def _repo_path(self, suffix: str = "") -> str:
        if not self._owner or not self._repo:
            raise RuntimeError(
                f"github_issues: invalid team_key {self._team_ref!r} "
                f"(need owner/repo)")
        return f"/repos/{self._owner}/{self._repo}{suffix}"

    async def _req(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | list | None = None,
        as_response: bool = False,
    ) -> Any:
        if not self._api:
            raise PMOTransient("github_issues: api_base is empty")
        url = f"{self._api}{path}"
        try:
            resp = await self._budget.request(          # THE wire call (ADR-0040)
                lambda: self._http.get().request(
                    method, url, params=params, json=json,
                    headers=self._headers()),
                rate_signal, instance=self._instance)
        except httpx.HTTPError as e:
            raise PMOTransient(f"github_issues network: {e}") from e
        if (resp.status_code == 422
                and "/dependencies/" in path
                and "already been taken" in (resp.text or "").lower()):
            return {"_duplicate": True}
        body = resp.text or ""
        if resp.status_code in (429, 500, 502, 503, 504) or (
                resp.status_code == 403 and "rate limit" in body.lower()):
            raise PMOTransient(
                f"github_issues {method} {path} → {resp.status_code}: "
                f"{body[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"github_issues {method} {path} → {resp.status_code}: "
                f"{body[:300]}")
        if as_response:
            return resp
        if not resp.content:
            return None
        return resp.json()

    def _require_issue(self, ref: MissionRef) -> None:
        if ref.kind != "issue":
            raise RuntimeError(
                "github_issues: projects are not supported "
                f"(got kind={ref.kind!r})")

    def _apply_team(self, team_ref: str) -> None:
        ref = (team_ref or self._team_ref or "").strip()
        if ref and ref != self._team_ref:
            self._team_ref = ref
            self._owner, self._repo = parse_team_ref(ref)
            self._label_names.clear()

    def _label_set(self, issue: dict) -> set[str]:
        return canonicalize_labels(
            lb.get("name") or "" for lb in (issue.get("labels") or []))

    def _mission(self, issue: dict) -> Mission:
        number = int(issue["number"])
        body = issue.get("body") or ""
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
            labels=self._label_set(issue),
            updated_at=updated_at,
            url=issue.get("html_url") or "",
            parent_ref=None,
            blocked_by=[],
            instance=self._instance,
        )

    async def _blocked_by_ids(self, number: int) -> list[str]:
        try:
            deps = await self._req(
                "GET",
                self._repo_path(f"/issues/{number}/dependencies/blocked_by"))
        except RuntimeError as e:
            if "404" in str(e):
                return []
            raise
        if not isinstance(deps, list):
            return []
        return [str(d["number"]) for d in deps if d.get("number") is not None]

    async def _enrich_blocked_by(self, mission: Mission) -> Mission:
        blockers = await self._blocked_by_ids(int(mission.pmo_id))
        return mission.model_copy(update={"blocked_by": blockers})

    async def _list_issues(self, *, state: str = "all") -> list[dict]:
        from .._toolkit import paginate_rest
        raw, _ = await paginate_rest(
            lambda page: self._req(
                "GET", self._repo_path("/issues"),
                params={"state": state, "per_page": ISSUES_PAGE, "page": page}),
            page_size=ISSUES_PAGE, max_pages=MAX_ISSUE_PAGES,
            what="github_issues list_issues", on_ceiling="warn")
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
            if m.status not in ("done", "canceled"):
                m = await self._enrich_blocked_by(m)
            out.append(m)
        return out

    async def get(self, ref: MissionRef) -> Mission:
        self._require_issue(ref)
        raw = await self._req("GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        if raw.get("pull_request"):
            raise RuntimeError(
                f"github_issues: {ref.pmo_id} is a pull request, not an issue")
        return await self._enrich_blocked_by(self._mission(raw))

    async def get_activity(self, ref: MissionRef,
                           full: bool = False) -> Activity:
        self._require_issue(ref)
        mission = await self.get(ref)
        from .._toolkit import last_page_from_headers, paginate_rest_newest
        max_pages = MAX_COMMENT_PAGES_FULL if full else MAX_COMMENT_PAGES

        async def _comments_page(page: int):
            resp = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}/comments"),
                params={"page": page, "per_page": COMMENTS_PAGE},
                as_response=True)
            items = resp.json() if resp.content else []
            last = last_page_from_headers(resp.headers, page_size=COMMENTS_PAGE)
            return items or [], last

        raw_comments, truncated = await paginate_rest_newest(
            _comments_page,
            page_size=COMMENTS_PAGE, max_pages=max_pages,
            what="github_issues get_activity", on_ceiling="flag")
        if truncated:
            log.error("github_issues get_activity truncated at %s pages",
                      max_pages)
        entries = [self._comment_entry(c, full=full) for c in raw_comments]
        entries.sort(key=lambda e: e.ts)
        return Activity(mission=mission, entries=entries,
                        mission_attachments=[], truncated=truncated)

    def _comment_entry(self, c: dict, *, full: bool) -> ActivityEntry:
        body = c.get("body") or ""
        created = c.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(timezone.utc)
        user = (c.get("user") or {}).get("login") or "unknown"
        return ActivityEntry(
            ts=ts, author=user, kind="comment", body=body,
            attachments=[],
            entry_id=str(c["id"]) if full and c.get("id") is not None else None,
            parent_id=None,
        )

    async def children_of(self, ref: MissionRef) -> list[Mission]:
        self._require_issue(ref)
        # Issue-only: no project children. Decomposition uses markers + relations.
        return []

    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        self._require_issue(ref)
        await self._req(
            "POST", self._repo_path(f"/issues/{ref.pmo_id}/comments"),
            json={"body": markdown})

    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None:
        self._require_issue(ref)
        state = "closed" if status in ("done", "canceled") else "open"
        body_patch: dict[str, Any] = {"state": state}
        cur = await self._req("GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        body = cur.get("body") or ""
        if status == "canceled":
            body_patch["body"] = apply_cancel_footer(body)
        elif CANCEL_FOOTER in body:
            body_patch["body"] = strip_cancel_footer(body)
        await self._req(
            "PATCH", self._repo_path(f"/issues/{ref.pmo_id}"), json=body_patch)

    async def cancel_mission(self, ref: MissionRef) -> None:
        self._require_issue(ref)
        cur = await self._req("GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        if ((cur.get("state") or "").lower() == "closed"
                and CANCEL_FOOTER in (cur.get("body") or "")):
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
            cur = await self._req(
                "GET", self._repo_path(f"/issues/{ref.pmo_id}"))
            next_names = (self._label_set(cur) - remove) | add
            await self._req(
                "PUT", self._repo_path(f"/issues/{ref.pmo_id}/labels"),
                json={"labels": sorted(next_names)})

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
            "POST", self._repo_path("/issues"),
            json={"title": title, "body": description or "",
                  "labels": sorted(label_names)})
        number = int(raw["number"])
        return mission_key(self._owner, self._repo, number), str(number)

    async def create_relation(self, blocker_id: str, blocked_id: str) -> None:
        # API wants the blocker's global numeric id, not the issue number.
        raw = await self._req(
            "GET", self._repo_path(f"/issues/{int(blocker_id)}"))
        issue_id = int(raw["id"])
        result = await self._req(
            "POST",
            self._repo_path(f"/issues/{int(blocked_id)}/dependencies/blocked_by"),
            json={"issue_id": issue_id})
        if isinstance(result, dict) and result.get("_duplicate"):
            return

    async def _fetch_all_labels(self) -> list[dict]:
        from .._toolkit import paginate_rest
        labels, _ = await paginate_rest(
            lambda page: self._req(
                "GET", self._repo_path("/labels"),
                params={"page": page, "per_page": LABELS_PAGE}),
            page_size=LABELS_PAGE, max_pages=MAX_LABEL_PAGES,
            what="github_issues labels", on_ceiling="raise",
            ceiling_error=(
                f"github_issues: more than {MAX_LABEL_PAGES * LABELS_PAGE} "
                f"labels on {self._owner}/{self._repo} — refusing a "
                f"truncated rewrite"))
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
                "POST", self._repo_path("/labels"),
                json={"name": name.upper(), "color": _LABEL_COLOR})
            by_upper[u] = created
            self._label_names.add(u)

    async def append_description(self, ref: MissionRef, text: str) -> None:
        self._require_issue(ref)
        cur = await self._req("GET", self._repo_path(f"/issues/{ref.pmo_id}"))
        body = (cur.get("body") or "") + text
        await self._req(
            "PATCH", self._repo_path(f"/issues/{ref.pmo_id}"),
            json={"body": body})

    async def upload_attachment(self, pmo_id: str, filename: str,
                                data: bytes) -> str:
        raise RuntimeError(
            "github_issues: attachments are not supported — GitHub has no "
            "official issue-file upload API (docs/05 §0 (d); option B)")

    async def download_asset(self, url: str) -> bytes:
        raise RuntimeError(
            "github_issues: attachments are not supported — no download path")

    async def health_probe(self, team_ref: str) -> PMOHealth:
        self._apply_team(team_ref)
        if not self._owner:
            return PMOHealth(ok=False, workspace=team_ref or self._team_ref,
                             detail="invalid team_key")
        try:
            await self._req("GET", self._repo_path(""))
            labels = await self._fetch_all_labels()
        except PMOTransient as e:
            return PMOHealth(ok=False, workspace=f"{self._owner}/{self._repo}",
                             detail=str(e))
        except RuntimeError as e:
            return PMOHealth(ok=False, workspace=f"{self._owner}/{self._repo}",
                             detail=str(e))
        present = {(lb.get("name") or "").upper() for lb in labels}
        managed = {n.upper() for n in ALL_LABELS}
        return PMOHealth(
            ok=True, workspace=f"{self._owner}/{self._repo}",
            managed_labels_present=len(present & managed),
            managed_labels_expected=len(ALL_LABELS),
        )

    def capabilities(self) -> PMOCapabilities:
        return PMOCapabilities(
            projects_supported=False,
            project_labels_supported=False,
            attachment_max_bytes=0,
            native_label_swap_atomic=True,
            relations_supported=True,
            attachments_supported=False,
            comment_max_chars=65536,
            global_ids=False,
        )
