"""ForgePort — GitLab adapter (docs/06 §7). Same shape as GitHubForge; MRs
instead of PRs. Live-verified E2E against a gitlab.com sandbox (v0, 2026-07)."""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import httpx

from ...ports.forge import (BranchProtection, ForgeDescriptor, ForgeError, ForgeHealth,
                            PullRequest)

log = logging.getLogger("devcake.forge")


class GitLabForge:
    descriptor = ForgeDescriptor(
        id="gitlab",
        display_name="GitLab",
        pr_instructions=(
            "Merge request (idempotent): `glab mr list --source-branch {branch}` — "
            "if one exists, update it (`glab mr update`) instead of creating; else "
            "`glab mr create --source-branch {branch} --target-branch {default} "
            "--title \"[{key}] {title}\" --description \"<summary + mission URL>\" --yes`. "
            "glab is authenticated via GITLAB_TOKEN; pass --repo if it asks."),
        clone_user="oauth2",
        git_user_name="DevCake",
        git_email="devcake@users.noreply.github.com",  # kept verbatim from v0
        cli_token_envs=["GITLAB_TOKEN"],
        token_env_default="GITLAB_TOKEN",
        secret_env_vars=["GITLAB_TOKEN", "GITLAB_REVIEWER_TOKEN"],
        token_patterns=[r"\bglpat-[A-Za-z0-9_-]{15,}\b"],
        secret_shape_prefixes=["glpat-"],
    )

    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 api_base: str | None = None):
        # api_base overrides; otherwise the instance is the repo URL's origin
        # (identical to the old https://gitlab.com default for gitlab.com repos,
        # and makes self-hosted instances work without extra config)
        origin, url_path = "", repo_url
        if repo_url:
            parts = urlsplit(repo_url)
            if parts.scheme and parts.netloc:
                origin = f"{parts.scheme}://{parts.netloc}"
                url_path = parts.path
        self.base = api_base or origin or "https://gitlab.com"
        path = url_path.strip("/").removesuffix(".git")
        self.project = quote(path, safe="")
        self.token = token
        self.reviewer_token = reviewer_token or None

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.reviewer_token if reviewer else self.token}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method, f"{self.base}/api/v4/projects/{self.project}{path}",
                headers=self._headers(reviewer), **kwargs)
            if resp.status_code >= 400:
                # normalized error type across adapters (docs/06): callers
                # handle ForgeError only, never httpx exceptions
                raise ForgeError(f"{method} {path} → {resp.status_code}: "
                                 f"{resp.text[:200]}", status=resp.status_code)
            return resp.json() if resp.text else None

    async def health_probe(self) -> ForgeHealth:
        try:
            project = await self._req("GET", "")
        except ForgeError as e:
            # 401/403/404 indict the credential; anything else is transient
            definitive = e.status in (401, 403, 404) and not (
                e.status == 403 and "rate limit" in str(e).lower())
            return ForgeHealth(
                ok=False, repository=self.project, transient=not definitive,
                detail=f"repository access failed (HTTP {e.status}); grant api and "
                       "write_repository scopes",
            )
        permissions = project.get("permissions") or {}
        levels = [int((permissions.get(name) or {}).get("access_level") or 0)
                  for name in ("project_access", "group_access")]
        can_push = max(levels, default=0) >= 30
        return ForgeHealth(
            ok=can_push, repository=str(project.get("path_with_namespace") or self.project),
            can_push=can_push,
            detail="" if can_push else "token lacks Developer/write_repository access",
        )

    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]:
        mrs = await self._req("GET", f"/merge_requests?source_branch={branch}"
                                     f"&order_by=created_at&sort=desc")
        if not mrs:
            return None
        return self._to_pr(mrs[0])

    @staticmethod
    def _to_pr(mr: dict) -> PullRequest:
        # MR state "merged" normalizes to closed+merged (port contract)
        return PullRequest(number=mr["iid"], url=mr["web_url"],
                           state="closed" if mr["state"] in ("closed", "merged")
                                 else "open",
                           merged=mr["state"] == "merged")

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        from ...security import redact
        await self._req("POST", f"/merge_requests/{pr_number}/notes",
                        json={"body": redact(markdown)})

    async def approve(self, pr_number: int) -> bool:
        if not self.reviewer_token:
            return False
        await self._req("POST", f"/merge_requests/{pr_number}/approve", reviewer=True)
        return True

    async def merge(self, pr_number: int) -> None:
        """Squash-merge. 409 (SHA/branch race) is a transient race, not a real
        failure — retried in place per the port contract (docs/06 §5)."""
        for attempt in range(3):
            try:
                await self._req("PUT", f"/merge_requests/{pr_number}/merge",
                                json={"squash": True})
                return
            except ForgeError as e:
                if e.status != 409 or attempt == 2:
                    raise
                await asyncio.sleep(3)

    async def mergeable(self, pr_number: int) -> Optional[bool]:
        """Port contract (docs/06 §5) — same tri-state as GitHubForge.mergeable.
        Prefers detailed_merge_status (GitLab ≥ 15.6); falls back to the legacy
        merge_status field on older instances."""
        mr = await self._req("GET", f"/merge_requests/{pr_number}")
        detailed = mr.get("detailed_merge_status")
        if detailed is not None:
            if detailed in ("conflict", "need_rebase"):
                return False
            if detailed == "mergeable":
                return True
            return None  # checking/unchecked/ci_must_pass/ci_still_running/…
        legacy = mr.get("merge_status")
        if legacy == "cannot_be_merged":
            return False
        if legacy == "can_be_merged":
            return True
        return None

    async def pr_state(self, pr_number: int) -> PullRequest:
        mr = await self._req("GET", f"/merge_requests/{pr_number}")
        return self._to_pr(mr)

    async def default_branch_protection(
            self, branch: str = "main") -> Optional[BranchProtection]:
        """Mirror of GitHubForge.default_branch_protection (docs/14). GitLab:
        a 404 on /protected_branches/{branch} means unprotected."""
        try:
            await self._req("GET", f"/protected_branches/{quote(branch, safe='')}")
            return BranchProtection(protected=True, requires_reviews=None)
        except ForgeError as e:
            if e.status == 404:
                return BranchProtection(protected=False, requires_reviews=None)
            return None
        except Exception:
            return None

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        return ("\n\n---\nTo approve and merge this MR yourself:\n"
                f"```\nglab mr approve {pr_url} && glab mr merge {pr_url} --squash\n```")
