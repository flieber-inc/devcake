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


class InternalRepo(BaseModel):
    """Admin-surface row for one auto-created internal repo."""
    name: str                  # {instance}-{mission-key}, lowercased
    mission_key: str
    html_url: str              # ROOT_URL-based (operator-clickable, :3300)
    clone_url: str             # runtime-network origin (Dev-side)
    size_kb: int = 0
    open_prs: int = 0
    updated_at: str = ""


class MissionRepoCredentials(BaseModel):
    """Per-mission machine-user credentials (docs/14 §2a: Gitea tokens are
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

    def mission_credentials(self, repo_name: str) -> MissionRepoCredentials | None:
        """The stored per-mission token pair (runspec source; sync read)."""
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
