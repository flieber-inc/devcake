"""The Run record (docs/10): pure domain model, persisted by the files
adapter (adapters/files/run_store.py).

Advisory telemetry only (INV-1) — wiping /data/state never corrupts mission
state.
"""

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# Pre-schema-v3 run records carry no real instance ref: `pmo_ref` is "" or
# the old "main" default, and such records always count as LOCAL (hiding
# them would orphan pre-v3 blocker work). THE one definition (2026-08
# cleanup — copies had drifted into router/ports and could diverge);
# consumers: MissionManager._run_is_ours, the blocker locator's locality
# set, FinalizerRouter's single-manager fallback, ports.forge.run_branch's
# legacy branch naming. (dispatch's blocker-mount guard deliberately checks
# only "main" — there it is a synthetic REPO name, not a pmo_ref.)
LEGACY_PMO_REFS = frozenset({"", "main"})

RunState = Literal[
    "dispatched", "running", "finalizing", "finished", "failed", "timed_out", "orphaned"
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def aware(ts: datetime) -> datetime:
    """Timestamps arrive from three sources (audit log, run records, PMO
    comments); a stray naive one must not crash a scheduler comparison.
    Moved from dispatch._aware (ADR-0034 PR-3): a time utility, not
    dispatch logic — it lives beside utcnow now."""
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)



def auth_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Run(BaseModel):
    schema_version: int = 2
    # lost-update fence (2026-08-12 audit F8): bumped by RunStore.save() on
    # every write. Two writers holding DIFFERENT objects for the same run
    # (a fresh get() vs a shared all()-cache object) used to last-writer-
    # wins SILENTLY; the fence makes the collision loud. Additive — legacy
    # records parse as rev 0.
    rev: int = 0
    run_id: str
    mission_key: str
    mission_pmo_id: str = ""
    # Operator-clickable PMO URL snapshotted at dispatch (mission.url).
    # Empty on legacy / steward / hello records; the runs list may fill
    # those from the live poll cache when the mission is still on the board.
    mission_url: str = ""
    pmo_kind: str = "issue"
    # which configured instance served this run (AppConfig.pmos/repos entry id)
    pmo_ref: str = "main"
    repo_ref: str = "main"
    mission_type: str
    dev_type: str
    # CLI version the provision container reported on run.started
    # (e.g. "0.2.112"). Empty on legacy records / hello / report failure.
    harness_version: str = ""
    seq: int
    attempt_of_step: int = 1
    # Done direct blockers' work repos (non-secret snapshot at dispatch):
    # [{repo_ref, mission_key}] — tokens are attached at runspec time.
    # Empty on legacy / relations-steward / no-blocker runs. The ADR-0033
    # discovery steward reuses this shape for the FAMILY's work repos.
    blocker_work: list[dict[str, str]] = Field(default_factory=list)
    # ADR-0033: which steward duty this run serves — "" (legacy/relations)
    # or "discovery". The flavor lives on the run record, never in the
    # outcome (one duty-agnostic `stewarded` outcome, founder ruling).
    steward_duty: str = ""
    # ADR-0033: the discovery run's dispatch snapshot of the batches its
    # package carried — [{pmo_id, key, step}]. Finalize disposition-receipts
    # exactly this set (a batch harvested AFTER dispatch was not in the
    # package and must stay pending); a routed-nowhere batch gets a `to=-`
    # receipt so the counterflow terminates instead of re-dispatching.
    steward_batches: list[dict] = Field(default_factory=list)
    # The mirror gate's needed_for set, snapshotted at dispatch (2026-08
    # evaluation F12): which repos this run's extras serve via mirror_path is
    # decided ONCE, when the gate proved them fresh — exactly like the
    # primary's DEVCAKE_MIRROR_PATH in spec_env. Empty on legacy records
    # (pre-field) → runspec falls back to the live derivation.
    mirror_repos: list[str] = Field(default_factory=list)
    # ADR-0016 addendum — supply-chain provenance: {card: sha} of every
    # skill-source repo card at the moment this run's `<card>/<skill>`
    # skills were read into spec_skills. Records WHICH third-party commit
    # produced the skills a run consumed (docs/14 trust-class shift); not
    # exposed on the runs API.
    skill_repo_heads: dict[str, str] = Field(default_factory=dict)
    # PLAN_MEMORY §3.6 — consumer memory mounts snapshotted at dispatch.
    # [{card, binding, commit, stale_cache, path}]. Empty on legacy /
    # Curator runs (no consumer mounts). A notebook added mid-flight
    # must not appear.
    memory_mounts: list[dict] = Field(default_factory=list)
    # ADR-0031 — the run's reading receipt: {entry_id, ts(iso)} of the newest
    # feed entry in the ACTIVITY.md mirror this run received. Captured only
    # from a SUCCESSFUL snapshot push (a failed push serves the previous,
    # older snapshot); refreshed when the Redis activity fallback rebuilds
    # the payload at container start. Empty on legacy records / internal
    # forge absent / empty feed → the Freshness Gate falls back to
    # entry-ts > created_at.
    feed_watermark: dict[str, str] = Field(default_factory=dict)
    stage_label_at_dispatch: Optional[str] = None
    # the PR branch minted at dispatch (schema v3): stored so review/merge
    # lookups can never drift from what the Dev actually pushed; "" on
    # legacy/steward/hello records (ports.forge.run_branch derives those)
    branch: str = ""
    spec_prompt: str = ""
    # skill-store files for the Dev (non-secret, fetched at dispatch so a
    # mid-run Gitea outage can't change what a runspec re-request serves):
    # [{name, files: [{path, content_b64}]}]
    spec_skills: list[dict[str, Any]] = Field(default_factory=list)
    # HOME-relative dir the entrypoint writes spec_skills under — snapshotted
    # at dispatch from the harness registry so dir, skill content, and launch
    # image come from the same read; "" on legacy records → entrypoint default
    spec_skills_dir: str = ""
    state: RunState = "dispatched"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    timeout_seconds: int = 120 * 60
    traceparent: Optional[str] = None
    # One-way verifier for the per-run Redis envelope credential. The raw ACL
    # password is passed directly to Dagu and is never persisted in run state.
    auth_digest: Optional[str] = None
    spec_env: dict[str, str] = Field(default_factory=dict)
    finalized_steps: list[str] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    token_report: Optional[dict[str, Any]] = None
    artifact_bytes: Optional[int] = None
    error: Optional[str] = None
    # ADR-0018 — the STRUCTURED half of `error`. Every terminal path stamps one
    # (the taxonomy of docs/15 §1); "" means a pre-upgrade record, which
    # attempt counting still honours via a prefix match on `error`. Matching on
    # this field instead of on `error` closes an injection: the tail of `error`
    # can carry Dev-authored text (decomposition.py raises with the Dev's
    # blocked_by list verbatim), so a Dev could otherwise emit
    # blocked_by:["DEV_AUTH"] and make its own failures stop counting.
    error_class: str = ""
    # ADR-0018 — frozen at failure time, never recomputed. A correlated
    # backend fault does not burn the mission's attempt; that verdict must not
    # flip later when the backend heals and the evidence window clears, or the
    # excused missions would give up all at once.
    attempt_counted: bool = True
    # App-level judgment when it diverges from the executor's: a run can end
    # state="finished" (Dagu succeeded, artifacts were legal) yet carry a
    # verdict like "rejected: …" because _transition refused to act on the
    # outcome. None means an ordinary success.
    verdict: Optional[str] = None
    # ADR-0022 — how many in-container continuations (nudge relaunches) the
    # entrypoint used before this run ended, success or failure. 0 = the loop
    # never fired, and every pre-ADR-0022 record reads as 0.
    continuations_used: int = 0
    # Process-local wipe generation stamped at launch (docs/10): RunStore.clear
    # bumps wipe_generation then unlinks files; save() drops any run whose
    # store_gen is older so in-flight finalize/heartbeat cannot resurrect a
    # record after "start fresh". Default 0 = born before any wipe in-process
    # (legacy records load as 0). Optional so older JSON still validates.
    store_gen: int = 0
