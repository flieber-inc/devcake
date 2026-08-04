"""ForgePort — GitHub adapter (docs/06). App-side, decision-bearing operations:
PR lookup, review comments, formal approval (optional 2nd token), squash merge,
health probe. The GitLab twin is adapters/gitlab (docs/06 §7)."""

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from ...ports.forge import (BranchProtection, ForgeCapabilities, ForgeDescriptor,
                            ForgeError, ForgeHealth, PRFile, PullRequest)

log = logging.getLogger("devcake.forge")

API = "https://api.github.com"


class GitHubForge:
    descriptor = ForgeDescriptor(
        id="github",
        display_name="GitHub",
        pr_instructions=(
            "Pull request (idempotent): `gh pr view {branch} --json url` — if one "
            "exists, update it (`gh pr edit`) instead of creating; else "
            "`gh pr create --head {branch} --title \"[{key}] {title}\" "
            "--body \"<summary + mission URL>\"`."),
        clone_user="x-access-token",
        git_user_name="DevCake",
        git_email="devcake@users.noreply.github.com",
        cli_token_envs=["GH_TOKEN"],
        secret_env_vars=["GITHUB_TOKEN", "GITHUB_REVIEWER_TOKEN",
                         "GITHUB_TOKEN_RO"],
        token_patterns=[r"\bghp_[A-Za-z0-9]{20,}\b",
                        r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"],
        secret_shape_prefixes=["ghp_", "github_pat_"],
    )
    capabilities = ForgeCapabilities(
        mergeable_tristate=True, self_approval_blocked=True,
        branch_protection_read="admin", pr_list_head_filter=True)

    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 api_base: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        # https://github.com/{owner}/{repo}
        parts = [p for p in repo_url.rstrip("/").removesuffix(".git").split("/") if p]
        if len(parts) < 2 or not parts[-1] or not parts[-2]:
            raise ValueError(
                f"invalid GitHub repository URL {repo_url!r}: need "
                f"https://github.com/owner/repo")
        self.owner, self.repo = parts[-2], parts[-1]
        self.api = api_base or API          # override unlocks GitHub Enterprise
        self.token = token
        self.reviewer_token = reviewer_token or None
        self._transport = transport        # tests inject MockTransport
        from ..http import PooledClient
        self._http = PooledClient(timeout=20, transport=transport)  # F16: keep-alive

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        token = self.reviewer_token if reviewer else self.token
        if not (token or "").strip():
            # Avoid illegal empty Authorization values (ADR-0011 class)
            raise ForgeError(
                "GitHub token missing; configure a write token for this repo",
                status=401)
        return {"Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json"}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   **kwargs) -> Any:
        client = self._http.get()   # pooled (F16); adapter aclose() owns it
        resp = await client.request(
            method, f"{self.api}/repos/{self.owner}/{self.repo}{path}",
            headers=self._headers(reviewer), **kwargs)
        if resp.status_code >= 400:
            raise ForgeError(f"{method} {path} → {resp.status_code}: "
                             f"{resp.text[:200]}", status=resp.status_code)
        return resp.json() if resp.text else None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health_probe(self) -> ForgeHealth:
        repository = f"{self.owner}/{self.repo}"
        try:
            repo = await self._req("GET", "")
        except ForgeError as e:
            # 401/403/404 indict the credential (bad token, no access, invisible
            # repo) — except GitHub's rate-limit 403, which is transient
            definitive = e.status in (401, 403, 404) and not (
                e.status == 403 and "rate limit" in str(e).lower())
            hint = ("; for a fine-grained PAT, select this repository and grant "
                    "Contents and Pull requests read/write")
            return ForgeHealth(ok=False, repository=repository, transient=not definitive,
                               detail=f"repository access failed (HTTP {e.status}){hint}")
        can_push = bool((repo.get("permissions") or {}).get("push"))
        return ForgeHealth(
            ok=can_push,
            repository=repository,
            can_push=can_push,
            can_read=True,          # the repository GET itself succeeded
            detail="" if can_push else (
                "token can read the repository but lacks push permission; grant "
                "Contents and Pull requests read/write"),
        )

    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]:
        """Newest PR (any state) whose head is the given branch."""
        prs = await self._req(
            "GET", f"/pulls?head={self.owner}:{branch}&state=all&sort=created&direction=desc")
        if not prs:
            return None
        pr = prs[0]
        # list payloads carry merged_at, not merged (full-GET only)
        return PullRequest(number=pr["number"], url=pr["html_url"],
                           state=pr["state"], merged=bool(pr.get("merged_at")))

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        from ...security import redact
        await self._req("POST", f"/issues/{pr_number}/comments",
                        json={"body": redact(markdown)})

    async def approve(self, pr_number: int) -> bool:
        """Formal approval with the reviewer token; False when none configured."""
        if not self.reviewer_token:
            return False
        await self._req("POST", f"/pulls/{pr_number}/reviews", reviewer=True,
                        json={"event": "APPROVE",
                              "body": "Approved by DevCake REVIEW."})
        return True

    async def merge(self, pr_number: int) -> None:
        """Squash-merge. 409 ("head branch was modified") is a transient race,
        not a real failure — retried in place per the port contract (docs/06 §5)
        so racy-but-healthy merges never park on DEVCAKE-MERGE. Already-merged
        is success (ISSUES #6): redelivery after a successful merge must not
        report auto-merge failure."""
        for attempt in range(3):
            try:
                await self._req("PUT", f"/pulls/{pr_number}/merge",
                                json={"merge_method": "squash"})
                return
            except ForgeError as e:
                # 409: transient race — retry without probing. Other statuses
                # (405/422 already-merged) probe PR state; redelivery must not
                # report failure on a merged PR (ISSUES #6).
                if e.status == 409 and attempt < 2:
                    await asyncio.sleep(3)
                    continue
                if e.status != 409 and await self._already_merged(pr_number):
                    return
                raise

    async def _already_merged(self, pr_number: int) -> bool:
        try:
            state = await self.pr_state(pr_number)
            return bool(state.merged)
        except Exception:  # noqa: BLE001 — probe contract: failure → False, caller re-raises the original merge error
            return False

    async def mergeable(self, pr_number: int) -> Optional[bool]:
        """Port contract (docs/06 §5) — single-shot, non-blocking tri-state:
        False = auto-resolvable by a branch sync (conflict, or stale branch
        behind an up-to-date protection rule); True = ready to merge now;
        None = wait (mergeability still computing, required checks/CI pending,
        or any unrecognized state — the safe default; the merge sweep provides
        the time dimension, never this call)."""
        pr = await self._req("GET", f"/pulls/{pr_number}")
        state = pr.get("mergeable_state")
        if state in ("dirty", "behind") or pr.get("mergeable") is False:
            return False
        if pr.get("mergeable") is True and state in ("clean", "unstable", "has_hooks"):
            return True
        return None  # null=computing, "blocked"=CI/approvals pending, or unknown

    async def pr_state(self, pr_number: int) -> PullRequest:
        pr = await self._req("GET", f"/pulls/{pr_number}")
        return PullRequest(number=pr["number"], url=pr["html_url"],
                           state=pr["state"], merged=bool(pr.get("merged")),
                           merge_commit_sha=pr.get("merge_commit_sha"))

    async def default_branch_protection(
            self, branch: str = "main") -> Optional[BranchProtection]:
        """{"protected": bool, "requires_reviews": bool|None} for the default
        branch, or None when unreadable. Branch protection is the ONLY effective
        control against a Dev merging with its own token (docs/14, ADR-0007
        addendum) — push-branch and merge both need contents:write."""
        try:
            b = await self._req("GET", f"/branches/{branch}")
        except ForgeError:
            return None
        protected = bool(b.get("protected"))
        requires_reviews = None
        try:  # classic protection detail (may 403/404 without admin scope)
            prot = await self._req("GET", f"/branches/{branch}/protection")
            requires_reviews = bool(prot.get("required_pull_request_reviews"))
        except ForgeError:
            pass
        try:  # repository rulesets (the modern mechanism)
            rules = await self._req("GET", f"/rules/branches/{branch}")
            if isinstance(rules, list) and rules:
                protected = True
                if requires_reviews is None:
                    requires_reviews = any(r.get("type") == "pull_request"
                                           for r in rules)
        except ForgeError:
            pass
        return BranchProtection(protected=protected, requires_reviews=requires_reviews)

    async def pr_files(self, pr_number: int) -> list[PRFile]:
        out: list[PRFile] = []
        page = 1
        while True:
            batch = await self._req(
                "GET", f"/pulls/{pr_number}/files?per_page=100&page={page}")
            if not batch:
                break
            out.extend(PRFile(path=f["filename"], status=f.get("status", "modified"),
                              additions=int(f.get("additions") or 0),
                              deletions=int(f.get("deletions") or 0))
                       for f in batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    async def file_content(self, path: str, ref: str) -> bytes:
        import base64
        from urllib.parse import quote
        data = await self._req(
            "GET", f"/contents/{quote(path)}?ref={quote(ref, safe='')}")
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"])
        raise ForgeError(f"unexpected contents payload for {path}")

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        """D14 (confirmed decision): every REVIEW PR comment ends with the exact
        copy-pasteable command to approve + merge."""
        return ("\n\n---\nTo approve and merge this PR yourself:\n"
                f"```\ngh pr review --approve {pr_url} && gh pr merge --squash {pr_url}\n```")
