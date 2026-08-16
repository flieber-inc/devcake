"""PMOPort — the ONE authoritative contract every PMO adapter implements
(docs/05 §1). Reads and writes are keyed by MissionRef: the adapter dispatches
on ref.kind internally, so vendor dualities (Linear's issue/project split)
never leak into the domain. Normalized DTOs live in domain.model."""

from typing import Optional, Protocol

from pydantic import BaseModel

from ..domain.model import Activity, Mission, MissionRef, NormalizedStatus


class PMOTransient(Exception):
    """Retryable PMO failure (429/5xx/network) — docs/15. Adapters raise this
    for anything worth retrying next poll cycle; everything else is permanent."""


class PMOHealth(BaseModel):
    """Neutral connection-probe result (docs/05): replaces the old private
    reach-ins into vendor JSON from /health and the admin test endpoint.
    Probes MUST NOT write (2026-08-12 audit F3): /health rides the SPA's
    10 s poll, so a probe that heals labels or PATCHes repo settings is a
    write path at poll cadence — healing belongs to the poll cycle's
    once-latch, never here."""
    ok: bool
    workspace: str = ""                 # resolved team/workspace reference
    managed_labels_present: int = 0     # of DevCake's managed set, found remotely
    managed_labels_expected: int = 0
    detail: str = ""


class PMOCapabilities(BaseModel):
    """Adapter self-description. No v0 reader — the single Linear adapter's
    quirks are handled where they occur — but future multi-PMO scheduling and
    the admin UI select behavior on these flags (kept by founder decision)."""
    projects_supported: bool
    project_labels_supported: bool
    attachment_max_bytes: int
    native_label_swap_atomic: bool
    relations_supported: bool = False
    # Official file-upload API. False = GitHub Issues (no public issue
    # attachment endpoint). Feed chokepoint posts inline and skips upload;
    # contract row 13 records SKIP. Default True so Linear / Gitea / GitLab
    # stay unchanged.
    attachments_supported: bool = True
    # pmo_ids are globally unique across the vendor environment (Linear
    # UUIDs) — only such systems may resolve blockers via PEER adapters or
    # accept peer run history on a locally-resolved foreign id. Colliding-id
    # systems (gitea_issues issue numbers) never cross instances. Declared
    # here so the blocker locator branches on a CAPABILITY, not a vendor
    # name (2026-08 evaluation F10 — adding a PMO no longer edits domain).
    global_ids: bool = False


class PMOPort(Protocol):
    """Every operation DevCake needs from a PMO system. Contract notes:

    - `get_activity` SHALLOW on a ref without an issue-style comment feed
      (Linear projects) returns the mission with `entries=[]` — never raises.
      The shallow project path has no production caller (marker scans are
      issue-only) and must stay cheap. That never-raises clause is scoped to
      `projects_supported` vendors: on a vendor WITHOUT project support, a
      project-kind ref is a caller bug and every method — reads and writes —
      MUST raise (permanent family), never fabricate a Mission or silently
      no-op the write (2026-08-12 audit F1: a swallowed project write
      reports success while swapping no labels, so the misroute re-derives
      forever with zero signal).
    - `get_activity(full=True)` (ADR-0014 D3, the activity-folder builder's
      mode) walks the ENTIRE feed history, carries reply structure
      (`entry_id`/`parent_id`) and mission-level attachments (description
      assets + the vendor's native attachment list), and sets
      `Activity.truncated` on its hard stop instead of raising. On a
      `projects_supported` vendor, full mode on a project ref mirrors the
      project-NATIVE feed (updates + their comments), long-form documents
      (`Activity.documents`), and external links/attachments; the enrichment
      is fail-open (a failure degrades to the brief alone — the mission must
      still dispatch). Default (shallow) mode keeps the cheap recent-window
      query — the marker-scan call paths must never pay full-history cost.
    - `post_feed` targets the kind-appropriate channel (issue comment /
      project update). Feed POLICY (redaction, sentinel, suppression) is the
      orchestrator's job; transport is the adapter's.
    - `create_mission` returns `(key, pmo_id)` — the id wires relation edges.
    - `create_relation` is duplicate-tolerant (decomposition resume, ADR-0007).
    - `ensure_labels` creates the managed label set in every namespace the
      vendor requires (Linear: team issue labels + workspace project labels).
    """

    # ── reads ────────────────────────────────────────────────────────────────
    # list_missions (DevCake-labeled only) has no v0 caller — the poll loop
    # reads list_all — but stays on the contract for adapters/versions where
    # the filtered read is materially cheaper (kept by founder decision, v0).
    async def list_missions(self, team_ref: str) -> list[Mission]: ...
    async def list_all(self, team_ref: str) -> list[Mission]: ...
    async def get(self, ref: MissionRef) -> Mission: ...
    async def get_activity(self, ref: MissionRef,
                           full: bool = False) -> Activity: ...
    async def children_of(self, ref: MissionRef) -> list[Mission]: ...

    # ── writes ───────────────────────────────────────────────────────────────
    async def post_feed(self, ref: MissionRef, markdown: str) -> None:
        """Post a feed entry. **Markdown fidelity is a port requirement:**
        DevCake stores state markers as backticked inline markdown (e.g.
        ``devcake:v1``, ``devcake:decomposition:v1 …``, merge-retry markers).
        Adapters must round-trip those bytes such that a later ``get_activity``
        can re-find them. ADF/rich-text PMOs (e.g. Jira) need an explicit
        fidelity strategy — multi-PMO is not “just another adapter” for this
        reason (ISSUES #35)."""
        ...
    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None: ...
    async def cancel_mission(self, ref: MissionRef) -> None:
        """Move the mission to the vendor's canceled/abandoned terminal state.
        Idempotent — canceling an already-canceled mission is success. A
        dedicated seam (not just set_status("canceled")): some PMOs express
        abandonment as archive/close rather than a plain status write."""
        ...
    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None: ...
    async def create_mission(self, team_ref: str, title: str, description: str,
                             priority: str, label_names: set[str],
                             parent_ref: Optional[str] = None) -> tuple[str, str]: ...
    async def create_relation(self, blocker_id: str, blocked_id: str) -> None: ...
    async def ensure_labels(self, team_ref: str, names: set[str]) -> None: ...
    async def append_description(self, ref: MissionRef, text: str) -> None:
        """Append ``text`` to the mission's description (markdown fidelity,
        same contract as ``post_feed``). Append-only INTENT — DevCake never
        composes rewrites — but the Linear implementation is an unguarded
        read-modify-write, so an edit a human saves inside the read-to-write
        window is lost (last writer wins). Accepted for the single v0
        caller: a short lineage footer on an issue that is canceled moments
        later (ADR-0012); callers with higher stakes need a CAS-capable
        vendor operation first. Issues only: no v0 caller passes a project
        ref, and callers must treat failures as non-fatal hygiene."""
        ...

    # ── assets ───────────────────────────────────────────────────────────────
    async def upload_attachment(self, pmo_id: str, filename: str,
                                data: bytes) -> str: ...
    async def download_asset(self, url: str) -> bytes: ...

    # ── meta ─────────────────────────────────────────────────────────────────
    async def health_probe(self, team_ref: str) -> PMOHealth: ...
    def capabilities(self) -> PMOCapabilities: ...
