"""Internal-forge provisioner for the bundled Gitea (docs/16 M11, F4).

Admin-credentialed (GITEA_ADMIN_* — the stack bootstrap secret): creates the
private org, service accounts, per-mission repos/users/token pairs. Secrets
land under /data/secrets/internal_forge/ — deliberately TWO path levels so
security._known_values()'s existing glob("*/*") scan auto-redacts every token
(plus explicit register_runtime_secret at mint/load for immediate coverage).

Isolation honesty (docs/14 §2a, live-verified): Gitea tokens are USER-scoped.
One machine user per mission, collaborator on ONLY its repo inside a private
org, holding a write+read `*:repository`-scoped token pair → the token
reaches exactly one repo (cross-repo = 404), and non-EXECUTE stages get the
read token (403 on writes). The admin credential used HERE is the sharpest
credential on the stack — it never leaves the app.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
from pathlib import Path

import httpx

from ... import security
from ...ports.internal_forge import (InternalRepo, MissionRepoCredentials,
                                     internal_repo_name)

log = logging.getLogger("devcake.internal_forge")

ORG = "devcake-internal"
# operator-CREATED repos (item 4, "gitea (internal)" repo cards) live in
# their own org: the per-mission admin surface (list/Clear/Clear data)
# walks devcake-internal only, so operator repos can never be swept or
# resurrected by the mission lifecycle
OPERATOR_ORG = "devcake-repos"
APP_USER = "devcake-app"            # org owner: app-side PR ops + merges
REVIEWER_USER = "devcake-reviewer"  # formal approvals (whitelisted per repo)
ACTIVITY_RO_USER = "devcake-activity-ro"  # shared RO clone account for
                                          # activity-* repos (ADR-0014 D4)
# The skill store (docs/16 skill store v1): one operator-editable repo in
# OPERATOR_ORG holding Claude Code skills. Deliberately NO branch protection,
# NO machine user, NO tokens — operators push skills straight to main via the
# Gitea UI; the app reads it with the admin credential. `skill-store` cannot
# collide with a repo card: card names must match ^[a-z][a-z0-9]{0,11}$
# (no hyphen — config._INSTANCE_NAME_RE).
SKILL_REPO = "skill-store"


def _secrets_dir() -> Path:
    return Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets" / "internal_forge"


def _svc_user(repo_name: str) -> str:
    """The per-mission machine-user name — Gitea caps usernames at 40 chars,
    but repo names run to 60, so a naive `svc-{repo}[:40]` truncation could
    collide two missions to one user (revoking each other's tokens; review
    finding #3). Suffix a deterministic hash of the FULL repo name so the
    name is collision-free while staying readable + ≤40 chars."""
    import hashlib
    digest = hashlib.sha1(repo_name.encode()).hexdigest()[:8]
    return f"svc-{repo_name[:27]}-{digest}"


class GiteaProvisioner:
    def __init__(self, url: str | None = None,
                 admin_user: str | None = None,
                 admin_password: str | None = None,
                 public_url: str | None = None,
                 transport: "httpx.AsyncBaseTransport | None" = None):
        # runtime-network origin (Devs clone here); the html links use the
        # operator-facing ROOT_URL (loopback :3300)
        self.url = (url or os.environ.get("DEVCAKE_GITEA_URL",
                                          "http://gitea:3000")).rstrip("/")
        self.public_url = (public_url or os.environ.get(
            "GITEA_UI_URL", "http://localhost:3300")).rstrip("/")
        self._auth = (admin_user or os.environ.get("GITEA_ADMIN_USER", ""),
                      admin_password or os.environ.get("GITEA_ADMIN_PASSWORD", ""))
        self._transport = transport      # test injection (LinearAdapter pattern)

    async def _req(self, method: str, path: str, ok=(200, 201, 204),
                   tolerate=(), **kw):
        async with httpx.AsyncClient(timeout=20, auth=self._auth,
                                     transport=self._transport) as client:
            resp = await client.request(method, f"{self.url}/api/v1{path}", **kw)
        if resp.status_code in ok:
            return resp.json() if resp.text else None
        if resp.status_code in tolerate:
            return None
        raise RuntimeError(f"internal forge: {method} {path} → "
                           f"{resp.status_code}: {resp.text[:200]}")

    # ── boot provisioning ────────────────────────────────────────────────────

    async def ensure_service_accounts(self) -> None:
        await self._req("POST", "/orgs",
                        json={"username": ORG, "visibility": "private"},
                        tolerate=(409, 422))
        for user in (APP_USER, REVIEWER_USER, ACTIVITY_RO_USER):
            await self._req("POST", "/admin/users", tolerate=(409, 422),
                            json={"username": user,
                                  "email": f"{user}@devcake.example",
                                  "password": f"Xx1!{pysecrets.token_urlsafe(24)}",
                                  "must_change_password": False})
        # devcake-app joins the org owners team → PR ops + merges everywhere
        teams = await self._req("GET", f"/orgs/{ORG}/teams")
        owners = next(t["id"] for t in teams if t["name"] == "Owners")
        await self._req("PUT", f"/teams/{owners}/members/{APP_USER}")
        svc = self._load("service.json") or {}
        # re-mint any token that no longer exists in Gitea (review finding #6:
        # a recreated gitea_data volume leaves stored tokens stale forever —
        # every internal merge then fails "not enough approvals"). Validated
        # admin-side by token_last_eight (the token's own scopes can't hit a
        # generic auth endpoint, and the reviewer isn't an org member).
        if not await self._service_token_live(APP_USER, "devcake-app",
                                              svc.get("app_token")):
            svc["app_token"] = await self._mint(
                APP_USER, "devcake-app",
                ["write:repository", "write:issue", "write:organization"])
        if not await self._service_token_live(REVIEWER_USER, "devcake-reviewer",
                                              svc.get("reviewer_token")):
            svc["reviewer_token"] = await self._mint(
                REVIEWER_USER, "devcake-reviewer",
                ["write:repository", "write:issue"])
        # ADR-0014 D4: ONE read-only token clones every activity-* repo
        # (per-repo collaborator grants happen at ensure_activity_repo)
        if not await self._service_token_live(ACTIVITY_RO_USER,
                                              "devcake-activity-ro",
                                              svc.get("activity_ro_token")):
            svc["activity_ro_token"] = await self._mint(
                ACTIVITY_RO_USER, "devcake-activity-ro", ["read:repository"])
        self._store("service.json", svc)
        self._register(svc)

    async def _service_token_live(self, user: str, name: str,
                                  token: str | None) -> bool:
        """Is the stored service token still the live one? Admin lists the
        user's tokens and we match on token_last_eight (a token value never
        appears again after mint, so this is the only way to compare)."""
        if not token:
            return False
        try:
            toks = await self._req("GET", f"/users/{user}/tokens",
                                   tolerate=(404,)) or []
        except Exception:
            return False
        return any(t.get("name") == name
                   and t.get("token_last_eight") == token[-8:] for t in toks)

    async def _mint(self, user: str, name: str, scopes: list[str]) -> str:
        # re-minting an existing token name 400s — delete then create
        await self._req("DELETE", f"/users/{user}/tokens/{name}",
                        tolerate=(404, 422))
        out = await self._req("POST", f"/users/{user}/tokens",
                              json={"name": name, "scopes": scopes})
        return out["sha1"]

    async def _ensure_protection(self, owner_repo: str) -> None:
        """Main-branch protection with the reviewer approvals whitelist.
        MUST run AFTER devcake-reviewer is a collaborator: Gitea silently
        drops non-collaborator names from approvals_whitelist_username
        (live-verified 2026-07-15 — every repo shipped with an EMPTY enabled
        whitelist, so required_approvals could never be satisfied, reviews
        were stamped official=false at creation, and merges 405'd forever).
        An existing protection whose whitelist lost the reviewer is PATCHed
        back (the repair path for adopted/legacy repos); reviews stamped
        unofficial before the repair stay unofficial — a fresh approval is
        needed for pre-existing PRs."""
        desired = {"branch_name": "main", "required_approvals": 1,
                   "enable_approvals_whitelist": True,
                   "approvals_whitelist_username": [REVIEWER_USER]}
        existing = await self._req(
            "GET", f"/repos/{owner_repo}/branch_protections/main",
            tolerate=(404,))
        if existing is None:
            await self._req("POST", f"/repos/{owner_repo}/branch_protections",
                            tolerate=(403, 409, 422), json=desired)
            existing = await self._req(
                "GET", f"/repos/{owner_repo}/branch_protections/main",
                tolerate=(404,))
        if existing is not None and REVIEWER_USER not in (
                existing.get("approvals_whitelist_username") or []):
            await self._req(
                "PATCH", f"/repos/{owner_repo}/branch_protections/main",
                tolerate=(403, 404, 422),
                json={"enable_approvals_whitelist": True,
                      "approvals_whitelist_username": [REVIEWER_USER]})

    # ── per-mission lifecycle ────────────────────────────────────────────────

    async def ensure_mission_repo(self, instance: str, mission_key: str
                                  ) -> MissionRepoCredentials:
        repo = internal_repo_name(instance, mission_key)
        svc_user = _svc_user(repo)
        stored = self._load(f"mission-{repo}.json")
        if stored:
            creds = MissionRepoCredentials(**stored)
            self._register_mission(creds)
            # probe BOTH tokens (audit A13: a revoked write token behind a
            # live read token was reused silently → EXECUTE died at push)
            read_ok = await self._token_works(creds.token_read, repo)
            write_ok = await self._token_works(creds.token_write, repo)
            if read_ok is True and write_ok is True:
                return creds
            if read_ok is not False and write_ok is not False:
                # transient probe (timeout/5xx): NEVER re-mint — the mint is
                # delete-then-create, so it would revoke the pair an
                # in-flight Dev already fetched via its runspec (audit A13)
                log.warning("internal repo %s: token probe transient — "
                            "reusing stored creds without re-mint", repo)
                return creds
            log.warning("internal repo %s: stored token rejected — re-minting",
                        repo)
        await self._req("POST", f"/orgs/{ORG}/repos", tolerate=(409,),
                        json={"name": repo, "private": True, "auto_init": True,
                              "default_branch": "main"})
        await self._req("POST", "/admin/users", tolerate=(409, 422),
                        json={"username": svc_user,
                              "email": f"{svc_user}@devcake.example",
                              "password": f"Xx1!{pysecrets.token_urlsafe(24)}",
                              "must_change_password": False})
        await self._req("PUT", f"/repos/{ORG}/{repo}/collaborators/{svc_user}",
                        json={"permission": "write"})
        await self._req("PUT", f"/repos/{ORG}/{repo}/collaborators/{REVIEWER_USER}",
                        json={"permission": "read"})
        # main protection AFTER the collaborators: the mission Dev can never
        # self-merge; the reviewer must be whitelisted for approvals to count,
        # and the whitelist only sticks once the reviewer is a collaborator
        # (see _ensure_protection)
        await self._ensure_protection(f"{ORG}/{repo}")
        creds = MissionRepoCredentials(
            repo_name=repo,
            clone_url=f"{self.url}/{ORG}/{repo}.git",
            username=svc_user,
            token_write=await self._mint(svc_user, "devcake-w",
                                         ["write:repository"]),
            token_read=await self._mint(svc_user, "devcake-r",
                                        ["read:repository"]),
        )
        self._store(f"mission-{repo}.json", creds.model_dump())
        self._register_mission(creds)
        log.info("internal repo provisioned: %s (user %s)", repo, svc_user)
        return creds

    async def create_operator_repo(self, name: str) -> dict:
        """Operator-created repo on the bundled Gitea (item 4): the target of
        a NORMAL "gitea (internal)" repo card. Mints the card's full token
        set and stores it under repo:{name} in the secret store, so saving a
        card with this name and the returned clone_url is all that's left:
        - svc write token (write:repository + write:issue — the card token is
          used app-side for PR comments and merges, unlike mission repos)
        - read-only token for non-EXECUTE stages (token_ro)
        - a reviewer token on the shared devcake-reviewer account, which is
          whitelisted in the branch protection (formal approvals count).

        An EXISTING devcake-repos repo is ADOPTED, not refused (founder
        report 2026-07-15): removing a repo card deletes its stored tokens
        while the Gitea repo lives on, so re-adding needs a re-mint path —
        the repo is left untouched, guardrails are re-ensured, and a fresh
        token set replaces whatever the store lost (_mint is delete-then-
        create, so stale same-named tokens are revoked, never duplicated)."""
        from ... import secrets as secrets_store
        await self._req("POST", "/orgs", tolerate=(409, 422),
                        json={"username": OPERATOR_ORG, "visibility": "private"})
        adopted = await self._req("GET", f"/repos/{OPERATOR_ORG}/{name}",
                                  tolerate=(404,)) is not None
        if not adopted:
            await self._req("POST", f"/orgs/{OPERATOR_ORG}/repos",
                            json={"name": name, "private": True,
                                  "auto_init": True, "default_branch": "main"})
        svc_user = _svc_user(f"op-{name}")
        await self._req("POST", "/admin/users", tolerate=(409, 422),
                        json={"username": svc_user,
                              "email": f"{svc_user}@devcake.example",
                              "password": f"Xx1!{pysecrets.token_urlsafe(24)}",
                              "must_change_password": False})
        await self._req("PUT", f"/repos/{OPERATOR_ORG}/{name}/collaborators/{svc_user}",
                        json={"permission": "write"})
        await self._req("PUT", f"/repos/{OPERATOR_ORG}/{name}/collaborators/{REVIEWER_USER}",
                        json={"permission": "read"})
        # protection AFTER the collaborators — the whitelist only sticks once
        # the reviewer is a collaborator; also repairs adopted/legacy repos
        # whose whitelist Gitea stripped (see _ensure_protection)
        await self._ensure_protection(f"{OPERATOR_ORG}/{name}")
        fields = {
            "token": await self._mint(svc_user, f"op-{name}-w",
                                      ["write:repository", "write:issue"]),
            "token_ro": await self._mint(svc_user, f"op-{name}-r",
                                         ["read:repository"]),
            "reviewer_token": await self._mint(REVIEWER_USER, f"op-{name}",
                                               ["write:repository", "write:issue"]),
        }
        for field, value in fields.items():
            secrets_store.write_connection_secret("repo", name, field, value)
        log.info("operator repo %s: %s/%s (user %s; card secrets stored)",
                 "adopted" if adopted else "created", OPERATOR_ORG, name,
                 svc_user)
        return {"repo_name": name,
                "clone_url": f"{self.url}/{OPERATOR_ORG}/{name}.git",
                "html_url": f"{self.public_url}/{OPERATOR_ORG}/{name}",
                "adopted": adopted}

    # ── skill store (docs/16 skill store v1) ─────────────────────────────────

    async def ensure_skill_store(self, seed_files: list[dict]) -> None:
        """Create-or-adopt {OPERATOR_ORG}/{SKILL_REPO} and seed the bundled
        skills — MISSING paths only, one commit, never clobbering operator
        edits (a deleted built-in file returns on next boot; DEFAULT_DEV_TYPES
        precedent). No auto_init: the seed commit itself initializes main, so
        the bundled README is not blocked by Gitea's auto-init stub."""
        await self._req("POST", "/orgs", tolerate=(409, 422),
                        json={"username": OPERATOR_ORG, "visibility": "private"})
        await self._req("POST", f"/orgs/{OPERATOR_ORG}/repos", tolerate=(409,),
                        json={"name": SKILL_REPO, "private": True,
                              "auto_init": False, "default_branch": "main"})
        existing = set(await self.skill_store_paths())
        missing = [f for f in seed_files if f["path"] not in existing]
        if not missing:
            return
        await self._req(
            "POST", f"/repos/{OPERATOR_ORG}/{SKILL_REPO}/contents",
            json={"message": "devcake: seed built-in skills",
                  "files": [{"operation": "create", "path": f["path"],
                             "content": f["content_b64"]} for f in missing]})
        log.info("skill store seeded: %d file(s) added", len(missing))

    async def skill_store_tree(self) -> list[dict]:
        """[{path, size}] for every blob on main → [] when there is nothing
        yet: 404 (repo or branch absent) AND 400 — live Gitea 1.24 answers
        400 "sha not found [main]" for the tree of a freshly created
        no-auto_init repo (live-verified 2026-07-17; the ref here is a
        constant, so a 400 can only mean that). Sizes let SkillService
        enforce its caps BEFORE downloading content. Gitea truncates trees
        at ~1000 entries — files past the boundary would read as missing,
        so an oversized store is surfaced loudly."""
        data = await self._req(
            "GET",
            f"/repos/{OPERATOR_ORG}/{SKILL_REPO}/git/trees/main?recursive=true",
            tolerate=(400, 404))
        if not data:
            return []
        if data.get("truncated"):
            log.warning("skill store tree is TRUNCATED (~1000-entry Gitea "
                        "cap) — skills past the boundary will read as "
                        "missing; prune the %s/%s repo",
                        OPERATOR_ORG, SKILL_REPO)
        return [{"path": t["path"], "size": int(t.get("size") or 0),
                 "sha": t.get("sha") or ""}
                for t in data.get("tree") or [] if t.get("type") == "blob"]

    async def skill_store_paths(self) -> list[str]:
        """Blob paths on main (seed-diff input) — the tree read above."""
        return [t["path"] for t in await self.skill_store_tree()]

    async def write_skill_files(self, files: list[dict], message: str) -> None:
        """Upsert store files in ONE commit (admin-panel authoring). The
        batch contents API needs the blob sha on updates and refuses one on
        creates, so the current tree decides per path."""
        shas = {t["path"]: t["sha"] for t in await self.skill_store_tree()}
        batch = []
        for f in files:
            entry = {"path": f["path"], "content": f["content_b64"]}
            if f["path"] in shas:
                entry.update(operation="update", sha=shas[f["path"]])
            else:
                entry["operation"] = "create"
            batch.append(entry)
        await self._req("POST", f"/repos/{OPERATOR_ORG}/{SKILL_REPO}/contents",
                        json={"message": message, "files": batch})

    async def delete_skill_paths(self, paths: list[str], message: str) -> None:
        """Delete store files in ONE commit (admin-panel skill removal)."""
        shas = {t["path"]: t["sha"] for t in await self.skill_store_tree()}
        batch = [{"operation": "delete", "path": p, "sha": shas[p]}
                 for p in paths if p in shas]
        if not batch:
            return
        await self._req("POST", f"/repos/{OPERATOR_ORG}/{SKILL_REPO}/contents",
                        json={"message": message, "files": batch})

    async def skill_store_file(self, path: str) -> bytes:
        """Raw bytes of one store file at main (GiteaForge.file_content's
        contents-API idiom, admin-credentialed)."""
        import base64
        from urllib.parse import quote
        data = await self._req(
            "GET", f"/repos/{OPERATOR_ORG}/{SKILL_REPO}/contents/"
                   f"{quote(path)}?ref=main")
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"])
        raise RuntimeError(f"skill store: unexpected contents payload for {path}")

    def skill_store_url(self) -> str:
        """Operator-clickable store URL (ROOT_URL-based, loopback :3300)."""
        return f"{self.public_url}/{OPERATOR_ORG}/{SKILL_REPO}"

    def mission_credentials(self, repo_name: str) -> MissionRepoCredentials | None:
        """The stored per-mission credential pair (runspec token source) —
        read from disk, never at rest between requests (docs/09 §5)."""
        stored = self._load(f"mission-{repo_name}.json")
        if not stored:
            return None
        creds = MissionRepoCredentials(**stored)
        self._register_mission(creds)
        return creds

    async def _token_works(self, token: str, repo: str) -> bool | None:
        # verify against the mission REPO — a *:repository-scoped token has no
        # read:user scope, so /user would 403 and force a needless re-mint
        # (which revokes the prior call's still-valid tokens).
        # Tri-state (audit A13): True = live; False = DEFINITIVELY rejected
        # (401/403/404 — bad token or repo gone); None = transient (network
        # error / 5xx) — the caller must not re-mint on None.
        try:
            async with httpx.AsyncClient(timeout=10,
                                         transport=self._transport) as client:
                r = await client.get(
                    f"{self.url}/api/v1/repos/{ORG}/{repo}",
                    headers={"Authorization": f"token {token}"})
        except Exception:
            return None
        if r.status_code == 200:
            return True
        if r.status_code in (401, 403, 404):
            return False
        return None

    # ── admin surface ────────────────────────────────────────────────────────

    async def list_repos(self) -> list[InternalRepo]:
        repos = await self._req("GET", f"/orgs/{ORG}/repos?limit=50") or []
        out = []
        for r in repos:
            html = r.get("html_url", "")
            # rewrite the in-container ROOT_URL host to the operator-facing one
            out.append(InternalRepo(
                name=r["name"],
                mission_key=r["name"].split("-", 1)[-1].upper(),
                html_url=html,
                clone_url=f"{self.url}/{ORG}/{r['name']}.git",
                size_kb=int(r.get("size") or 0),
                open_prs=int(r.get("open_pr_counter") or 0),
                updated_at=str(r.get("updated_at") or "")))
        return out

    async def delete_repo(self, repo_name: str) -> None:
        svc_user = _svc_user(repo_name)
        await self._req("DELETE", f"/repos/{ORG}/{repo_name}", tolerate=(404,))
        # purge=true drops ownerships that would otherwise block deletion
        await self._req("DELETE", f"/admin/users/{svc_user}?purge=true",
                        tolerate=(404,))
        path = _secrets_dir() / f"mission-{repo_name}.json"
        if path.exists():
            path.unlink()
        security.unregister_runtime_secret(f"internal:{repo_name}:w")
        security.unregister_runtime_secret(f"internal:{repo_name}:r")
        log.info("internal repo deleted: %s (+ %s)", repo_name, svc_user)

    async def health(self) -> dict:
        # ui_url rides along so the SPA can link the Gitea UI from anywhere
        # (Overview quick link, Repositories page) without an extra fetch
        try:
            org = await self._req("GET", f"/orgs/{ORG}", tolerate=(404,))
            if org is None:
                return {"ok": False, "ui_url": self.public_url,
                        "detail": "org not provisioned yet (boot pending?)"}
            return {"ok": True, "detail": "", "ui_url": self.public_url}
        except Exception as e:
            return {"ok": False, "detail": str(e)[:200],
                    "ui_url": self.public_url}

    # ── secret storage (0600, two-level path → auto-redaction scan) ─────────

    def _store(self, name: str, data: dict) -> None:
        d = _secrets_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def _load(self, name: str) -> dict | None:
        path = _secrets_dir() / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            log.error("unreadable internal-forge secret %s", path)
            return None

    def _register(self, svc: dict) -> None:
        security.register_runtime_secret("internal:app", svc.get("app_token", ""))
        security.register_runtime_secret("internal:reviewer",
                                         svc.get("reviewer_token", ""))
        security.register_runtime_secret("internal:activity-ro",
                                         svc.get("activity_ro_token", ""))

    def _register_mission(self, creds: MissionRepoCredentials) -> None:
        security.register_runtime_secret(f"internal:{creds.repo_name}:w",
                                         creds.token_write)
        security.register_runtime_secret(f"internal:{creds.repo_name}:r",
                                         creds.token_read)

    def service_tokens(self) -> dict | None:
        svc = self._load("service.json")
        if svc:
            self._register(svc)
        return svc
