"""ForgePort — GitLab adapter (docs/06 §7). Same shape as GitHubForge; MRs
instead of PRs. Live-verified E2E against a gitlab.com sandbox (v0, 2026-07)."""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import httpx

from ...ports.forge import (ApplyProtectionResult, BranchProtection,
                            ForgeCapabilities, ForgeDescriptor, ForgeError,
                            ForgeHealth, PRFile, PRFilesResult, ProtectionShape,
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
        # GitLab's real noreply form needs a numeric user id, so it can't be
        # static — use a neutral non-deliverable identity (RFC 2606)
        git_email="devcake@devcake.invalid",
        pr_noun="merge request",
        cli_token_envs=["GITLAB_TOKEN"],
        secret_env_vars=["GITLAB_TOKEN", "GITLAB_REVIEWER_TOKEN",
                         "GITLAB_TOKEN_RO"],
        token_patterns=[r"\bglpat-[A-Za-z0-9_-]{15,}\b"],
        secret_shape_prefixes=["glpat-"],
    )
    # GitLab allows a merge-request author to approve their own MR by default
    # (self_approval_blocked=False); mergeable via detailed_merge_status is a
    # real tri-state; MR-list source_branch filters server-side
    capabilities = ForgeCapabilities(
        mergeable_tristate=True, self_approval_blocked=False,
        branch_protection_read="maintainer", branch_protection_write=True,
        pr_list_head_filter=True)

    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 api_base: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
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
        if not path:
            raise ValueError(
                f"invalid GitLab repository URL {repo_url!r}: need a project "
                f"path (e.g. https://gitlab.com/group/project)")
        self.project = quote(path, safe="")
        self.token = token
        self.reviewer_token = reviewer_token or None
        self._transport = transport        # tests inject MockTransport
        from ..http import PooledClient
        self._http = PooledClient(timeout=20, transport=transport)  # F16: keep-alive

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        token = self.reviewer_token if reviewer else self.token
        if not (token or "").strip():
            raise ForgeError(
                "GitLab token missing; configure a write token for this repo",
                status=401)
        return {"PRIVATE-TOKEN": token}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   raw: bool = False, **kwargs) -> Any:
        from ..http import forge_request
        # THE forge wire call (adapters/http.forge_request, ADR-0034):
        # callers handle ForgeError only, never httpx exceptions — now true
        return await forge_request(
            self._http.get(), method,
            f"{self.base}/api/v4/projects/{self.project}{path}",
            path_label=path, headers=self._headers(reviewer), raw=raw,
            **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health_probe(self) -> ForgeHealth:
        try:
            project = await self._req("GET", "")
        except ForgeError as e:
            # 401/403/404 indict the credential; anything else is transient
            definitive = e.status in (401, 403, 404) and not (
                e.status == 403 and "rate limit" in str(e).lower())
            if e.status is None:
                detail = "repository access failed (network)"
            elif definitive:
                detail = (
                    f"repository access failed (HTTP {e.status}); grant api and "
                    "write_repository scopes"
                )
            else:
                detail = f"repository access failed (HTTP {e.status})"
            return ForgeHealth(
                ok=False, repository=self.project, transient=not definitive,
                detail=detail,
            )
        permissions = project.get("permissions") or {}
        levels = [int((permissions.get(name) or {}).get("access_level") or 0)
                  for name in ("project_access", "group_access")]
        can_push = max(levels, default=0) >= 30
        return ForgeHealth(
            ok=can_push, repository=str(project.get("path_with_namespace") or self.project),
            can_push=can_push,
            can_read=True,          # the project GET itself succeeded
            detail="" if can_push else "token lacks Developer/write_repository access",
        )

    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]:
        # Percent-encode so `#` in forge-issue branch names is not a URL
        # fragment (same trap as GitHub's `head=` filter).
        from urllib.parse import quote
        source = quote(branch, safe="")
        mrs = await self._req("GET", f"/merge_requests?source_branch={source}"
                                     f"&order_by=created_at&sort=desc")
        if not mrs:
            return None
        return self._to_pr(mrs[0])

    @staticmethod
    def _to_pr(mr: dict) -> PullRequest:
        # MR state "merged" normalizes to closed+merged (port contract);
        # squash merges carry the sha on squash_commit_sha instead
        return PullRequest(number=mr["iid"], url=mr["web_url"],
                           state="closed" if mr["state"] in ("closed", "merged")
                                 else "open",
                           merged=mr["state"] == "merged",
                           merge_commit_sha=mr.get("merge_commit_sha")
                                            or mr.get("squash_commit_sha"))

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        from ...security import redact
        await self._req("POST", f"/merge_requests/{pr_number}/notes",
                        json={"body": redact(markdown)})

    async def approve(self, pr_number: int) -> bool:
        """Formal approval with the reviewer token; False when none configured.

        self_approval_blocked=False (docs/06 §4/§7): GitLab allows an MR
        author to approve by default, so a write token reused as reviewer
        still posts the approve call.
        """
        if not (self.reviewer_token or "").strip():
            return False
        await self._req("POST", f"/merge_requests/{pr_number}/approve", reviewer=True)
        return True

    async def merge(self, pr_number: int) -> None:
        """Squash-merge. 409 (SHA/branch race) is a transient race, not a real
        failure — retried in place per the port contract (docs/06 §5).
        Already-merged is success."""
        for attempt in range(3):
            try:
                await self._req("PUT", f"/merge_requests/{pr_number}/merge",
                                json={"squash": True})
                return
            except ForgeError as e:
                if e.status == 409 and attempt < 2:
                    await asyncio.sleep(3)
                    continue
                if e.status != 409:
                    try:
                        state = await self.pr_state(pr_number)
                        if state.merged:
                            return
                    except Exception:  # noqa: BLE001 — best-effort merged check; original error re-raised
                        pass
                raise

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
        except Exception:  # noqa: BLE001 — probe contract: failure → None (protection unknown); never raises into the poll loop
            return None

    async def _discover_status_checks(self, branch: str) -> list[str]:
        """Commit status / job names on the default-branch HEAD. Empty when
        the project has no CI — GitLab maps a non-empty discovery to
        only_allow_merge_if_pipeline_succeeds (no per-context require on Free)."""
        try:
            b = await self._req(
                "GET", f"/repository/branches/{quote(branch, safe='')}")
        except ForgeError:
            return []
        sha = ((b.get("commit") or {}).get("id") or "")
        if not sha:
            return []
        try:
            statuses = await self._req(
                "GET", f"/repository/commits/{quote(sha, safe='')}/statuses"
            ) or []
        except ForgeError:
            return []
        return sorted({s.get("name") for s in statuses
                       if isinstance(s, dict) and s.get("name")})

    async def _read_protection_shape(
            self, branch: str) -> ProtectionShape | None:
        try:
            pb = await self._req(
                "GET", f"/protected_branches/{quote(branch, safe='')}")
        except ForgeError as e:
            if e.status == 404:
                return None
            return None
        approvals = 0
        pipeline_checks: list[str] = []
        try:
            proj = await self._req("GET", "")
            approvals = int(proj.get("approvals_before_merge") or 0)
            if proj.get("only_allow_merge_if_pipeline_succeeds"):
                # preserve non-empty discovered names if we can; else marker
                pipeline_checks = ["*pipeline*"]
        except ForgeError:
            pass
        return ProtectionShape(
            require_pull_request=True,
            allow_force_push=bool(pb.get("allow_force_push")),
            allow_deletions=False,
            required_status_checks=pipeline_checks,
            required_approving_review_count=approvals,
        )

    async def _write_protection_shape(
            self, branch: str, shape: ProtectionShape) -> None:
        # Idempotent protect: drop existing rule then recreate (GitLab has no
        # full PATCH for access levels on Free).
        qbranch = quote(branch, safe="")
        try:
            await self._req("DELETE", f"/protected_branches/{qbranch}")
        except ForgeError as e:
            if e.status not in (404,):
                raise
        await self._req(
            "POST", "/protected_branches",
            json={
                "name": branch,
                "push_access_level": 0,       # No one — changes via MR only
                "merge_access_level": 40,     # Maintainer
                "allow_force_push": bool(shape.allow_force_push),
            })
        # Project-level merge gates (approvals + pipeline) — best-effort on
        # Free/CE; 403 remaps via run_apply.
        proj_body: dict[str, Any] = {
            "approvals_before_merge": int(shape.required_approving_review_count),
            "only_allow_merge_if_pipeline_succeeds": bool(
                shape.required_status_checks),
        }
        await self._req("PUT", "", json=proj_body)

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
            forge_label="GitLab",
            write_permission="Maintainer+ (api + protect branch / project settings)",
        )

    async def pr_files(self, pr_number: int) -> PRFilesResult:
        """MR changed-file list. GitLab's /changes may set ``overflow`` when
        size limits withhold paths; retry once with access_raw_diffs=true
        (Gitaly-backed) and surface residual truncation on the DTO."""
        path = f"/merge_requests/{pr_number}/changes"
        data = await self._req("GET", path) or {}
        if data.get("overflow"):
            data = await self._req("GET", f"{path}?access_raw_diffs=true") or {}
        truncated = bool(data.get("overflow"))
        if truncated:
            log.warning(
                "gitlab pr_files #%s: overflow remains after access_raw_diffs "
                "retry — changed-file list incomplete", pr_number)
        out: list[PRFile] = []
        for ch in (data.get("changes") or []):
            status = ("added" if ch.get("new_file") else
                      "removed" if ch.get("deleted_file") else
                      "renamed" if ch.get("renamed_file") else "modified")
            out.append(PRFile(path=ch.get("new_path") or ch.get("old_path") or "",
                              status=status))
        return PRFilesResult(files=out, truncated=truncated)

    async def file_content(self, path: str, ref: str) -> bytes:
        from urllib.parse import quote
        # ref percent-encoded too (audit A16): a '#'/'?'/space in a branch
        # name corrupted the request — the GitHub/Gitea fix missed this one
        raw = await self._req("GET",
            f"/repository/files/{quote(path, safe='')}/raw"
            f"?ref={quote(ref, safe='')}", raw=True)
        return raw

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        return ("\n\n---\nTo approve and merge this MR yourself:\n"
                f"```\nglab mr approve {pr_url} && glab mr merge {pr_url} --squash\n```")
