"""ForgePort — Gitea adapter (docs/06; docs/16 M11). Serves both the bundled
internal fallback forge and external/user-supplied Gitea instances.

Every divergence below was LIVE-VERIFIED against 1.24.7-rootless (M11 probe):
- the PR list's `head` filter is IGNORED server-side → filter client-side;
- review approval event is "APPROVED" (not GitHub's "APPROVE"); the poster
  cannot approve their own PR (422); approvals count as *official* only when
  the reviewer has write access OR is on the branch protection's approvals
  whitelist — the internal-forge provisioner whitelists devcake-reviewer;
- merge is POST /pulls/{n}/merge {"Do": "squash"}; 405 is OVERLOADED:
  "Please try again later" (async mergeability check — transient, retry) vs
  "Does not have enough approvals" (definitive) vs already-merged — never
  classify by status code alone (the GitHub 405 lesson, docs/06 §5);
- `mergeable` is a plain boolean (no tri-state field): True/False/absent →
  True/False/None; the merge-first ordering in the sweep compensates;
- tokens are 40 hex chars — a redaction regex would mask every git SHA, so
  token_patterns is DELIBERATELY empty; value registration is the only
  redaction line (docs/14 §7);
- clone auth: the token in the URL userinfo authenticates regardless of the
  username half → a static clone_user works.
"""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

import base64

from ...ports.forge import (ApplyProtectionResult, BranchProtection,
                            ForgeCapabilities, ForgeDescriptor, ForgeError,
                            ForgeHealth, PRFile, PRFilesResult, ProtectionShape,
                            PullRequest)

log = logging.getLogger("devcake.forge")

_TRY_AGAIN = "try again later"


class GiteaForge:
    descriptor = ForgeDescriptor(
        id="gitea",
        display_name="Gitea",
        pr_instructions=(
            "Pull request via the Gitea API (no CLI needed; idempotent). "
            "Derive the API root from $DEVCAKE_REPO_URL: insert `/api/v1/repos` "
            "after the host and drop the trailing `.git` "
            "(http://host/owner/repo.git → http://host/api/v1/repos/owner/repo); "
            "call it $API below, and authenticate every call with "
            "`-H \"Authorization: token $GITEA_SERVER_TOKEN\"`. First check for "
            "an existing PR: `curl -s $API/pulls?state=all` and look for one "
            "whose head ref is `{branch}` — if found, push your commits and "
            "stop (the PR updates itself). Else create it: "
            "`curl -s -X POST $API/pulls -H 'Content-Type: application/json' "
            "-d '{{\"head\": \"{branch}\", \"base\": \"{default}\", "
            "\"title\": \"[{key}] {title}\"}}'`."),
        clone_user="devcake",   # userinfo username is ignored (live-verified)
        git_user_name="DevCake",
        git_email="devcake@devcake.invalid",
        pr_noun="pull request",
        cli_token_envs=["GITEA_SERVER_TOKEN"],
        secret_env_vars=["GITEA_TOKEN", "GITEA_TOKEN_RO",
                         "GITEA_REVIEWER_TOKEN", "GITEA_SERVER_TOKEN"],
        # DELIBERATELY empty: 40-hex tokens collide with git SHAs (docstring)
        token_patterns=[],
        secret_shape_prefixes=[],
    )
    # observed divergence (M11 probe): no mergeable_state → boolean-only;
    # poster self-approve rejected; protection read needs repo admin; the
    # PR-list head filter is IGNORED server-side (adapter filters client-side)
    capabilities = ForgeCapabilities(
        mergeable_tristate=False, self_approval_blocked=True,
        branch_protection_read="admin", branch_protection_write=True,
        pr_list_head_filter=False)

    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 api_base: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        origin, url_path = "", repo_url
        if repo_url:
            parts = urlsplit(repo_url)
            if parts.scheme and parts.netloc:
                origin = f"{parts.scheme}://{parts.netloc}"
                url_path = parts.path
        path = url_path.strip("/").removesuffix(".git")
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2:
            raise ValueError(
                f"invalid Gitea repository URL {repo_url!r}: need "
                f"https://<host>/owner/repo")
        self.owner, self.repo = segs[-2], segs[-1]
        self.api = (api_base or origin or "").rstrip("/")
        if not self.api:
            raise ValueError(
                f"invalid Gitea repository URL {repo_url!r}: need a host")
        self.token = token
        self.reviewer_token = reviewer_token or None
        self._transport = transport        # tests inject MockTransport
        from ..http import PooledClient
        self._http = PooledClient(timeout=20, transport=transport)  # F16: keep-alive

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        token = self.reviewer_token if reviewer else self.token
        if not (token or "").strip():
            raise ForgeError(
                "Gitea token missing; configure a write token for this repo",
                status=401)
        return {"Authorization": f"token {token}"}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   **kwargs) -> Any:
        from ..http import forge_request
        # THE forge wire call (adapters/http.forge_request, ADR-0034):
        # httpx exceptions become ForgeError(status=None) per the port
        return await forge_request(
            self._http.get(), method,
            f"{self.api}/api/v1/repos/{self.owner}/{self.repo}{path}",
            path_label=path, headers=self._headers(reviewer), **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health_probe(self) -> ForgeHealth:
        repository = f"{self.owner}/{self.repo}"
        try:
            repo = await self._req("GET", "")
        except ForgeError as e:
            # 401/403/404 indict the credential (bad/read-only token, no
            # access, invisible private repo) — no rate-limit carve-out on
            # self-hosted Gitea
            definitive = e.status in (401, 403, 404)
            if e.status is None:
                detail = "repository access failed (network)"
            elif definitive:
                detail = (
                    f"repository access failed (HTTP {e.status}); the "
                    f"token needs write:repository scope and repo access"
                )
            else:
                detail = f"repository access failed (HTTP {e.status})"
            return ForgeHealth(
                ok=False, repository=repository, transient=not definitive,
                detail=detail)
        can_push = bool((repo.get("permissions") or {}).get("push"))
        return ForgeHealth(
            ok=can_push, repository=repository, can_push=can_push,
            can_read=True,          # the repository GET itself succeeded
            detail="" if can_push else (
                "token can read the repository but lacks push permission"),
        )

    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]:
        """Newest PR (any state) whose head is the given branch. The server
        IGNORES a `head` query param (live-verified) — filter client-side,
        paginating so a busy repo (>50 PRs) doesn't hide our PR beyond page 1
        (review finding #10)."""
        from .._toolkit import paginate_rest
        raw, _ = await paginate_rest(
            lambda page: self._req(
                "GET",
                f"/pulls?state=all&sort=recentupdate&limit=50&page={page}"),
            page_size=50, max_pages=20,
            what="gitea get_pr_by_branch", on_ceiling="warn")
        matching = [p for p in raw
                    if ((p.get("head") or {}).get("ref")) == branch]
        if not matching:
            return None
        pr = max(matching, key=lambda p: p.get("number", 0))
        return PullRequest(number=pr["number"], url=pr["html_url"],
                           state="closed" if pr["state"] == "closed" else "open",
                           merged=bool(pr.get("merged")))

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        from ...security import redact
        await self._req("POST", f"/issues/{pr_number}/comments",
                        json={"body": redact(markdown)})

    async def approve(self, pr_number: int) -> bool:
        """Formal approval with the reviewer token; False when none configured.
        NOTE: only counts toward required_approvals when the reviewer is on
        the protection's approvals whitelist or has write access (verified).

        self_approval_blocked=True (docs/06 §4): the write token pasted as
        reviewer is not a distinct reviewer account — return False without a
        wire call rather than letting Gitea reject the self-approve.
        """
        rev = (self.reviewer_token or "").strip()
        if not rev:
            return False
        if rev == (self.token or "").strip():
            return False
        await self._req("POST", f"/pulls/{pr_number}/reviews", reviewer=True,
                        json={"event": "APPROVED",
                              "body": "Approved by DevCake REVIEW."})
        return True

    async def merge(self, pr_number: int) -> None:
        """Squash-merge. Gitea's 405 is overloaded (docstring): retry only the
        transient "Please try again later" (async mergeability check); on any
        other failure probe already-merged first (redelivery after a
        successful merge must not report auto-merge failure)."""
        for attempt in range(3):
            try:
                await self._req("POST", f"/pulls/{pr_number}/merge",
                                json={"Do": "squash"})
                return
            except ForgeError as e:
                if e.status == 405 and _TRY_AGAIN in str(e).lower() and attempt < 2:
                    await asyncio.sleep(3)
                    continue
                if await self._already_merged(pr_number):
                    return
                raise

    async def _already_merged(self, pr_number: int) -> bool:
        try:
            state = await self.pr_state(pr_number)
            return bool(state.merged)
        except Exception:  # noqa: BLE001 — probe contract: failure → False, caller re-raises the original merge error
            return False

    async def mergeable(self, pr_number: int) -> Optional[bool]:
        """Port tri-state from Gitea's plain boolean (no mergeable_state
        equivalent — capability `mergeable_tristate=False`): the sweep's
        merge-FIRST ordering provides the missing wait dimension."""
        pr = await self._req("GET", f"/pulls/{pr_number}")
        m = pr.get("mergeable")
        if m is True:
            return True
        if m is False:
            return False
        return None

    async def pr_state(self, pr_number: int) -> PullRequest:
        pr = await self._req("GET", f"/pulls/{pr_number}")
        return PullRequest(number=pr["number"], url=pr["html_url"],
                           state="closed" if pr["state"] == "closed" else "open",
                           merged=bool(pr.get("merged")),
                           merge_commit_sha=pr.get("merge_commit_sha"))

    async def default_branch_protection(
            self, branch: str = "main") -> Optional[BranchProtection]:
        """Reads the protection rule list (repo-admin scope; None when
        unreadable per the port contract). requires_reviews maps from
        required_approvals ≥ 1."""
        try:
            rules = await self._req("GET", "/branch_protections")
        except ForgeError:
            return None
        rule = next((r for r in (rules or [])
                     if r.get("branch_name") == branch), None)
        if rule is None:
            return BranchProtection(protected=False, requires_reviews=None)
        return BranchProtection(
            protected=True,
            requires_reviews=bool(rule.get("required_approvals", 0) >= 1))

    async def _discover_status_checks(self, branch: str) -> list[str]:
        """Contexts reported on the default-branch HEAD (Gitea statuses API).
        Empty when the repo has no CI — never invent names."""
        try:
            b = await self._req("GET", f"/branches/{branch}")
        except ForgeError:
            return []
        sha = ((b.get("commit") or {}).get("id")
               or (b.get("commit") or {}).get("sha") or "")
        if not sha:
            return []
        try:
            statuses = await self._req("GET", f"/statuses/{sha}") or []
        except ForgeError:
            return []
        return sorted({s.get("context") for s in statuses
                       if isinstance(s, dict) and s.get("context")})

    async def _read_protection_shape(
            self, branch: str) -> ProtectionShape | None:
        try:
            rule = await self._req("GET", f"/branch_protections/{branch}")
        except ForgeError as e:
            if e.status == 404:
                return None
            # list fallback (some Gitea builds prefer the collection)
            try:
                rules = await self._req("GET", "/branch_protections") or []
            except ForgeError:
                return None
            rule = next((r for r in rules
                         if r.get("branch_name") == branch), None)
            if rule is None:
                return None
        if not rule:
            return None
        # Named contexts only — never treat Gitea's "*" (or empty+enabled)
        # as a derived check name. Those mean "any context must succeed" and
        # map onto require_status_checks_unscoped (same role as GitLab Free's
        # only_allow_merge_if_pipeline_succeeds).
        from ...ports.forge import real_status_checks
        checks = real_status_checks(
            list(rule.get("status_check_contexts") or []))
        enable_status = bool(rule.get("enable_status_check"))
        unscoped = enable_status and not checks
        return ProtectionShape(
            require_pull_request=not bool(rule.get("enable_push")),
            allow_force_push=bool(rule.get("enable_force_push")),
            allow_deletions=False,  # protected branch cannot be deleted
            required_status_checks=checks,
            required_approving_review_count=int(
                rule.get("required_approvals") or 0),
            require_status_checks_unscoped=unscoped,
        )

    async def _write_protection_shape(
            self, branch: str, shape: ProtectionShape) -> None:
        from ...ports.forge import real_status_checks
        contexts = real_status_checks(list(shape.required_status_checks))
        enable_status = bool(contexts) or bool(
            shape.require_status_checks_unscoped)
        # Vendor wire form of the unscoped flag when no named contexts exist
        # (Gitea docs: empty list is invalid; use "*" for any-context). Never
        # invent "*" as a DevCake-derived check name — only as this encoding.
        wire_contexts = (
            contexts if contexts
            else (["*"] if shape.require_status_checks_unscoped else [])
        )
        body = {
            "branch_name": branch,
            "enable_push": not shape.require_pull_request,
            "enable_force_push": bool(shape.allow_force_push),
            "required_approvals": int(shape.required_approving_review_count),
            "enable_status_check": enable_status,
            "status_check_contexts": wire_contexts,
        }
        existing = None
        try:
            existing = await self._req("GET", f"/branch_protections/{branch}")
        except ForgeError as e:
            if e.status != 404:
                raise
        if existing is None:
            await self._req("POST", "/branch_protections", json=body)
        else:
            patch = {k: v for k, v in body.items() if k != "branch_name"}
            await self._req(
                "PATCH", f"/branch_protections/{branch}", json=patch)

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
            forge_label="Gitea",
            write_permission="repo admin (write:repository + owner/admin role)",
        )

    async def pr_files(self, pr_number: int) -> PRFilesResult:
        """Changed files across the PR (paginated — large changesets)."""
        from .._toolkit import paginate_rest
        raw, truncated = await paginate_rest(
            lambda page: self._req(
                "GET", f"/pulls/{pr_number}/files?limit=50&page={page}"),
            page_size=50, max_pages=40,
            what=f"gitea pr_files #{pr_number}", on_ceiling="warn")
        return PRFilesResult(
            files=[PRFile(path=f.get("filename", ""),
                          status=f.get("status", "modified"),
                          additions=int(f.get("additions") or 0),
                          deletions=int(f.get("deletions") or 0))
                   for f in raw],
            truncated=truncated)

    async def file_content(self, path: str, ref: str) -> bytes:
        """Raw bytes of a file at a ref (base64-safe — non-code deliverables
        are binary: images, xlsx). Uses the contents API, decoding base64.
        Path/ref are percent-encoded so '#'/'?'/'%' in filenames don't
        corrupt the request (review finding #5)."""
        from urllib.parse import quote
        data = await self._req(
            "GET", f"/contents/{quote(path)}?ref={quote(ref, safe='')}")
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"])
        raise ForgeError(f"unexpected contents payload for {path}")

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        """D14: every REVIEW PR comment ends with the exact copy-pasteable
        way to approve + merge (Gitea: the PR page — no ubiquitous CLI)."""
        return ("\n\n---\nTo approve and merge this PR yourself, open "
                f"{pr_url} and use Review → Approve, then Merge (squash).")
