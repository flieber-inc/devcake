"""ForgePort — GitLab adapter (docs/06 §7). Same shape as GitHubForge; MRs
instead of PRs. Live verification requires a GitLab sandbox (M6 checklist)."""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ...ports.forge import ForgeError

log = logging.getLogger("devcake.forge")


class GitLabForge:
    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 base: str = "https://gitlab.com"):
        path = repo_url.removeprefix(base).strip("/").removesuffix(".git")
        self.project = quote(path, safe="")
        self.base = base
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

    async def get_pr_by_branch(self, branch: str) -> Optional[dict]:
        mrs = await self._req("GET", f"/merge_requests?source_branch={branch}"
                                     f"&order_by=created_at&sort=desc")
        if not mrs:
            return None
        mr = mrs[0]
        # normalize to the GitHubForge dict shape the callers rely on
        return {"number": mr["iid"], "html_url": mr["web_url"], "state": mr["state"]}

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

    async def pr_state(self, pr_number: int) -> dict:
        mr = await self._req("GET", f"/merge_requests/{pr_number}")
        return {"state": "closed" if mr["state"] in ("closed", "merged") else "open",
                "merged": mr["state"] == "merged", "url": mr["web_url"],
                "number": mr["iid"]}

    async def default_branch_protection(self, branch: str = "main") -> Optional[dict]:
        """Mirror of GitHubForge.default_branch_protection (docs/14). GitLab:
        a 404 on /protected_branches/{branch} means unprotected."""
        try:
            await self._req("GET", f"/protected_branches/{quote(branch, safe='')}")
            return {"protected": True, "requires_reviews": None}
        except ForgeError as e:
            if e.status == 404:
                return {"protected": False, "requires_reviews": None}
            return None
        except Exception:
            return None

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        return ("\n\n---\nTo approve and merge this MR yourself:\n"
                f"```\nglab mr approve {pr_url} && glab mr merge {pr_url} --squash\n```")
