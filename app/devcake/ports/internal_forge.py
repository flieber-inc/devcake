"""InternalForgePort — provisioning seam for the bundled fallback forge
(docs/16 M11, F4). Distinct from ForgePort: these are ADMIN operations
(create repos/users, mint tokens, delete) that only the internal Gitea
supports; day-to-day PR mechanics go through the ordinary ForgePort adapter
the resolver synthesizes.

Constructed only via adapters.registry (the F1 import tripwire holds)."""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel

_SAFE = re.compile(r"[^a-z0-9-]+")


def internal_repo_name(instance: str, mission_key: str) -> str:
    """The deterministic name of a mission's internal repo — {instance}-{key}
    lowercased/sanitized. Domain knowledge (the resolver derives it to detect
    a prior internal routing across restarts), so it lives on the port, not
    in the adapter (F1 import boundary)."""
    return _SAFE.sub("-", f"{instance}-{mission_key}".lower()).strip("-")[:60]


# ADR-0014 D4: the sweeper discriminator — includes the trailing hyphen, so
# no operator card name (config._INSTANCE_NAME_RE, hyphen-free) can ever match
ACTIVITY_PREFIX = "activity-"


def activity_repo_name(instance: str, mission_key: str) -> str:
    """The deterministic name of a mission's ACTIVITY repo (ADR-0014 D4) —
    prefix applied AFTER the 60-char cap (max 69 < Gitea's 100-char limit;
    re-truncating after prefixing could collide two long mission keys). A
    cap landing mid-hyphen can yield a trailing '-': live-verified accepted
    by Gitea 1.24 (2026-07-18), and exact parity with work-repo names."""
    return ACTIVITY_PREFIX + internal_repo_name(instance, mission_key)


class InternalRepo(BaseModel):
    """Admin-surface row for one auto-created internal repo."""
    name: str                  # {instance}-{mission-key}, lowercased
    mission_key: str
    html_url: str              # ROOT_URL-based (operator-clickable, :3300)
    clone_url: str             # runtime-network origin (Dev-side)
    size_kb: int = 0
    open_prs: int = 0
    updated_at: str = ""


class ActivityRepoCredentials(BaseModel):
    """Shared read-only clone credentials for one activity repo (ADR-0014
    D4): no per-mission machine user — the app is the only writer, so every
    Dev clones with the single ACTIVITY_RO_USER token."""
    repo_name: str
    clone_url: str             # runtime-network origin (Dev-side)
    username: str
    token: str


class MissionRepoCredentials(BaseModel):
    """Per-mission machine-user credentials (docs/14 §2 Zone B + ADR-0010: Gitea tokens are
    user-scoped — isolation = one collaborator user per mission in a private
    org, holding a write+read scoped token pair; the read token is token_ro
    so per-stage scoping works exactly as on external repos)."""
    repo_name: str
    clone_url: str
    username: str
    token_write: str
    token_read: str


class InternalForgePort(Protocol):
    async def ensure_service_accounts(self) -> None:
        """Idempotent boot provisioning: the private org, the app-side
        merge account, and the reviewer account (+ their tokens)."""
        ...

    async def ensure_mission_repo(self, instance: str, mission_key: str
                                  ) -> MissionRepoCredentials:
        """Per-Mission idempotent: create (or reuse) the mission's repo,
        its machine user, and the scoped token pair — reused across
        attempts and rework (the M4 PR-reuse mechanics carry over)."""
        ...

    def mission_repo_binding(self, creds: MissionRepoCredentials):
        """(RepoInstance row, app-side ForgePort adapter) for a provisioned
        mission repo — ready for ForgeRuntime.register_internal. Lives on the
        port because ONLY the internal forge knows its own vendor: the old
        call site built a named gitea adapter inside domain code, threading
        the F1 tripwire's registry exemption (2026-08 evaluation F9)."""
        ...

    def mission_credentials(self, repo_name: str) -> MissionRepoCredentials | None:
        """The stored per-mission token pair (runspec source; sync read)."""
        ...

    async def ensure_skill_store(self, seed_files: list[dict]) -> None:
        """Idempotent boot/sync step: create-or-adopt the operator-editable
        skill-store repo and seed the bundled skills — missing paths only,
        never clobbering operator edits. seed_files: [{path, content_b64}]."""
        ...

    async def skill_store_tree(self) -> list[dict]:
        """[{path, size, sha}] blobs on the store's main branch ([] when
        absent/empty) — sizes let the SkillService enforce its payload
        caps before downloading any content; shas drive batch upserts."""
        ...

    async def write_skill_files(self, files: list[dict], message: str) -> None:
        """Upsert [{path, content_b64}] into the store in one commit
        (admin-panel authoring: create + import + overwrite)."""
        ...

    async def delete_skill_paths(self, paths: list[str], message: str) -> None:
        """Delete store paths in one commit (admin-panel skill removal)."""
        ...

    async def skill_store_file(self, path: str) -> bytes:
        """Raw bytes of one store file at main."""
        ...

    def skill_store_url(self) -> str:
        """Operator-clickable store URL (sync; ROOT_URL-based)."""
        ...

    # ── per-mission activity repos (ADR-0014 D4) ─────────────────────────────

    async def ensure_activity_repo(self, instance: str, mission_key: str
                                   ) -> str:
        """Create-or-adopt the mission's activity repo (unprotected, no
        machine user, shared-RO collaborator). Returns the repo name."""
        ...

    async def push_activity_snapshot(self, repo_name: str, files: list[dict],
                                     message: str) -> None:
        """ONE commit making main exactly match files [{path, content_b64}]:
        create/update by tree sha, stale paths deleted, unchanged blobs
        skipped (identical snapshot ⇒ no commit)."""
        ...

    def activity_credentials(self, repo_name: str
                             ) -> "ActivityRepoCredentials | None":
        """Shared RO clone credentials (runspec source; sync read)."""
        ...

    async def list_activity_repos(self) -> list[InternalRepo]:
        """Every activity-* repo — paginated, prefix-filtered (operator
        repos and the skill-store never appear)."""
        ...

    async def delete_activity_repo(self, repo_name: str) -> None:
        """Clear-sweep delete; refuses non-activity-prefixed names with
        ValueError before any HTTP."""
        ...

    async def list_repos(self) -> list[InternalRepo]: ...

    async def delete_repo(self, repo_name: str) -> None:
        """Founder decision (2026-07-14): repos are retained indefinitely;
        this is the manual admin Clear — deletes the repo, its machine
        user (revoking both tokens), and the stored credential file."""
        ...

    async def health(self) -> dict:
        """{ok, detail} for /health's internal-forge block."""
        ...
