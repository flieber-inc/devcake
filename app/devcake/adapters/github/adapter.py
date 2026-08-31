"""ForgePort — GitHub adapter (docs/06). App-side, decision-bearing operations:
PR lookup, review comments, formal approval (optional 2nd token), squash merge,
health probe. The GitLab twin is adapters/gitlab (docs/06 §7)."""

import asyncio
import logging
from typing import Any, Optional

import httpx

from ...ports.forge import (ApplyProtectionResult, BranchProtection,
                            ForgeCapabilities, ForgeDescriptor, ForgeError,
                            ForgeHealth, PRFile, PRFilesResult, ProtectionShape,
                            PullRequest)

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
        branch_protection_read="admin", branch_protection_write=True,
        pr_list_head_filter=True)

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
        from ..http import forge_request
        # THE forge wire call (adapters/http.forge_request, ADR-0034)
        return await forge_request(
            self._http.get(), method,
            f"{self.api}/repos/{self.owner}/{self.repo}{path}",
            path_label=path, headers=self._headers(reviewer), **kwargs)

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
            if e.status is None:
                detail = "repository access failed (network)"
            elif definitive:
                hint = ("; for a fine-grained PAT, select this repository and grant "
                        "Contents and Pull requests read/write")
                detail = f"repository access failed (HTTP {e.status}){hint}"
            else:
                detail = f"repository access failed (HTTP {e.status})"
            return ForgeHealth(ok=False, repository=repository, transient=not definitive,
                               detail=detail)
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
        """Newest PR (any state) whose head is the given branch.

        Branch names from forge-issue PMO keys carry ``#`` (``owner/repo#N``).
        Percent-encode the head value so ``#`` is not treated as a URL
        fragment — otherwise GitHub never sees the filter and merge_sweep
        cannot complete after a merge.
        """
        from urllib.parse import quote
        head = quote(f"{self.owner}:{branch}", safe="")
        prs = await self._req(
            "GET", f"/pulls?head={head}&state=all&sort=created&direction=desc")
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
        """Formal approval with the reviewer token; False when none configured.

        self_approval_blocked=True (docs/06 §4): the write token pasted as
        reviewer is not a distinct reviewer account — return False without a
        wire call rather than letting GitHub reject the self-approve.
        """
        rev = (self.reviewer_token or "").strip()
        if not rev:
            return False
        if rev == (self.token or "").strip():
            return False
        await self._req("POST", f"/pulls/{pr_number}/reviews", reviewer=True,
                        json={"event": "APPROVE",
                              "body": "Approved by DevCake REVIEW."})
        return True

    async def merge(self, pr_number: int) -> None:
        """Squash-merge. 409 ("head branch was modified") is a transient race,
        not a real failure — retried in place per the port contract (docs/06 §5)
        so racy-but-healthy merges never park on DEVCAKE-MERGE. Already-merged
        is success: redelivery after a successful merge must not report
        auto-merge failure."""
        for attempt in range(3):
            try:
                await self._req("PUT", f"/pulls/{pr_number}/merge",
                                json={"merge_method": "squash"})
                return
            except ForgeError as e:
                # 409: transient race — retry without probing. Other statuses
                # (405/422 already-merged) probe PR state; redelivery must not
                # report failure on a merged PR.
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

    async def _discover_status_checks(self, branch: str) -> list[str]:
        """Combined status contexts + check-run names on branch HEAD."""
        try:
            b = await self._req("GET", f"/branches/{branch}")
        except ForgeError:
            return []
        sha = ((b.get("commit") or {}).get("sha") or "")
        if not sha:
            return []
        names: set[str] = set()
        try:
            combined = await self._req("GET", f"/commits/{sha}/status") or {}
            for s in combined.get("statuses") or []:
                ctx = s.get("context")
                if ctx:
                    names.add(ctx)
        except ForgeError:
            pass
        try:
            runs = await self._req(
                "GET", f"/commits/{sha}/check-runs?per_page=100") or {}
            for r in runs.get("check_runs") or []:
                name = r.get("name")
                if name:
                    names.add(name)
        except ForgeError:
            pass
        return sorted(names)

    async def _read_protection_shape(
            self, branch: str) -> ProtectionShape | None:
        try:
            prot = await self._req("GET", f"/branches/{branch}/protection")
        except ForgeError as e:
            if e.status in (404, 403):
                # unprotected or unreadable detail — treat as no classic rule
                try:
                    b = await self._req("GET", f"/branches/{branch}")
                    if not b.get("protected"):
                        return None
                except ForgeError:
                    return None
                # protected flag but no detail → unknown richness; not as-strict
                return ProtectionShape(
                    require_pull_request=True,
                    allow_force_push=True,   # unknown → force apply to lock down
                    allow_deletions=True,
                    required_status_checks=[],
                    required_approving_review_count=0,
                )
            return None
        reviews = prot.get("required_pull_request_reviews") or {}
        checks_obj = prot.get("required_status_checks") or {}
        contexts = list(checks_obj.get("contexts") or [])
        for c in checks_obj.get("checks") or []:
            if isinstance(c, dict) and c.get("context"):
                contexts.append(c["context"])
        force = prot.get("allow_force_pushes") or {}
        deletions = prot.get("allow_deletions") or {}
        return ProtectionShape(
            require_pull_request=True,  # classic protection implies PR path
            allow_force_push=bool(force.get("enabled")),
            allow_deletions=bool(deletions.get("enabled")),
            required_status_checks=sorted({c for c in contexts if c}),
            required_approving_review_count=int(
                reviews.get("required_approving_review_count") or 0),
        )

    async def _write_protection_shape(
            self, branch: str, shape: ProtectionShape) -> None:
        reviews = None
        if shape.required_approving_review_count > 0:
            reviews = {
                "required_approving_review_count":
                    int(shape.required_approving_review_count),
                "dismiss_stale_reviews": False,
                "require_code_owner_reviews": False,
            }
        status_checks = None
        if shape.required_status_checks:
            status_checks = {
                "strict": True,
                "contexts": list(shape.required_status_checks),
            }
        body = {
            "required_status_checks": status_checks,
            "enforce_admins": True,
            "required_pull_request_reviews": reviews,
            "restrictions": None,
            "allow_force_pushes": bool(shape.allow_force_push),
            "allow_deletions": bool(shape.allow_deletions),
        }
        # GitHub requires required_pull_request_reviews object (or null) when
        # require_pr; null disables reviews but branch stays protected.
        if shape.require_pull_request and reviews is None:
            body["required_pull_request_reviews"] = {
                "required_approving_review_count": 0,
                "dismiss_stale_reviews": False,
                "require_code_owner_reviews": False,
            }
        await self._req("PUT", f"/branches/{branch}/protection", json=body)

    async def apply_default_branch_protection(
            self, branch: str = "main") -> ApplyProtectionResult:
        from ..protection_apply import run_apply_default_branch_protection
        return await run_apply_default_branch_protection(
            capabilities=self.capabilities,
            write_token=self.token,
            reviewer_token=self.reviewer_token,
            branch=branch,
            discover_status_checks=self._discover_status_checks,
            read_protection_shape=self._read_protection_shape,
            write_protection_shape=self._write_protection_shape,
            forge_label="GitHub",
            write_permission="Administration (edit repository rules) on the write token",
        )

    async def pr_files(self, pr_number: int) -> PRFilesResult:
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
        # GitHub pagination walks to completion — no vendor truncation flag.
        return PRFilesResult(files=out, truncated=False)

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
