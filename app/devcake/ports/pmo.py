"""PMOPort — the ONE authoritative contract every PMO adapter implements
(docs/05 §1). Reads and writes are keyed by MissionRef: the adapter dispatches
on ref.kind internally, so vendor dualities (Linear's issue/project split)
never leak into the domain. Normalized DTOs live in domain.model."""

import functools
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Literal, Optional, Protocol

from pydantic import BaseModel

from ..domain.model import Activity, Mission, MissionRef, NormalizedStatus


class PMOTransient(Exception):
    """Retryable PMO failure (429/5xx/network) — docs/15. Adapters raise this
    for anything worth retrying next poll cycle; everything else is permanent.

    ``retry_after`` (seconds) and ``reset_at`` (epoch seconds) are advisory:
    set when the vendor or the request budget knows when the next attempt
    can succeed, ``None`` otherwise."""

    def __init__(self, msg: str = "", *, retry_after: float | None = None,
                 reset_at: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after
        self.reset_at = reset_at


class PMOBudgetExceeded(PMOTransient):
    """The request budget refused the call BEFORE any vendor request was made
    (docs/15 §2, ADR-0040): the remaining quota is reserved for critical
    work, or pacing would require a wait the caller's class never takes.
    A subclass, so every `except PMOTransient` keeps its segment-skip
    semantics while logs and health can tell self-throttle from a vendor
    rejection."""


# ── request budget call classes (ADR-0040) ───────────────────────────────────
# The port declares HOW URGENT a call is; the adapters' shared governor
# decides what that means against the vendor's quota. Two classes only:
# `critical` = anything that writes back a run's results or launches work
# (finalize, dispatch, operator actions) — may spend the reserve, waits for
# the refill; `routine` = everything else (poll reads, sweeps, probes) —
# paced, never sleeps, refused when the reserve is reached. Unset context
# means routine, so a caller that never declares gets today's behaviour.
CallClass = Literal["critical", "routine"]


@dataclass(frozen=True)
class PMOCallContext:
    call_class: CallClass
    started: float                      # time.monotonic() at context entry
    wait_budget_s: float | None = None  # None → the governor's class default


pmo_call_ctx: ContextVar[PMOCallContext | None] = ContextVar(
    "pmo_call_ctx", default=None)
_mono = time.monotonic     # seam: tests drive the governor and this clock together


@contextmanager
def pmo_call(call_class: CallClass, *,
             wait_budget_s: float | None = None) -> Iterator[PMOCallContext]:
    """Declare the class of every PMO call made inside the block. Nested
    blocks: the inner declaration wins. The wait budget is cumulative for
    the whole block (a finalize's many calls share one deadline)."""
    ctx = PMOCallContext(call_class, _mono(), wait_budget_s)
    token = pmo_call_ctx.set(ctx)
    try:
        yield ctx
    finally:
        pmo_call_ctx.reset(token)


def with_pmo_call(call_class: CallClass, *, wait_budget_s: float | None = None):
    """Decorator form of `pmo_call` for the async boundary verbs (manager
    finalize/dispatch, admin actions): the whole call runs under one
    context, so its many PMO calls share one cumulative wait budget."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with pmo_call(call_class, wait_budget_s=wait_budget_s):
                return await fn(*args, **kwargs)
        return wrapper
    return deco


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
    """Adapter self-description consumed by the feed chokepoint, blocker
    locator, health probe, and admin mission-action paths (attachments,
    comment caps, global_ids, relations)."""
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
    # Vendor issue-comment character cap. None = no extra cap (attachments
    # handle long bodies). GitHub Issues is 65536; the feed chokepoint
    # paginates the FULL body as `Part i of n` comments under that cap.
    comment_max_chars: int | None = None
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
        reason."""
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
