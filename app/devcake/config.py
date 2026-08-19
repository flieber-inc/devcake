"""AppConfig: /data/config/config.yaml (docs/10 §3); first boot is EMPTY —
everything is configured through the admin GUI (schema v4, no env seeding).
The full operator surface: PMO/repo connections (0..N instances, secrets
GUI-stored under /data/secrets/ — ADR-0011), adoption, polling, assignments,
concurrency, merge policy, relations steward.
"""

import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger("devcake.config")

CONFIG_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "config" / "config.yaml"


# Operator-chosen instance identity (schema v3+, docs/16 M9). Lowercase
# alnum, ≤12 chars, NO hyphens: the name is embedded uppercased in branch
# names and run ids ({INSTANCE}-{key}), where a hyphen would make the
# compound ambiguous; ≤12 protects the 64-char Dagu run-id budget.
# Body (no anchors) is shared by skill refs, repo markers, etc. (CAKE-87).
INSTANCE_NAME_BODY = r"[a-z][a-z0-9]{0,11}"
_INSTANCE_NAME_RE = rf"^{INSTANCE_NAME_BODY}$"


def skill_ref_pattern() -> str:
    """DevType.skills entry shape: optional ``source/`` + skill dir name.

    Composed from INSTANCE_NAME_BODY + domain.skills.SKILL_NAME_RE — do not
    re-spell either half inline.
    """
    from .domain.skills import SKILL_NAME_RE
    return rf"(?:{INSTANCE_NAME_BODY}/)?{SKILL_NAME_RE}"

# Cron job ids (and any other non-instance slug): lowercase alnum +
# hyphen/underscore, no slash. Wider than instance names so reserved
# `memory-curator` fits; still a conservative path-safe token.
_CRON_ID_RE = r"^[a-z][a-z0-9_-]{0,31}$"

MEMORY_CURATOR_CRON_ID = "memory-curator"

MEMORY_CURATOR_TEMPLATE = (
    "Drain .claims/ in the work repository. Each *.json file is one\n"
    "unvalidated lead (finding / evidence / scope) with origin ids.\n"
    "Promote a lead to a note only when the notebook's own README\n"
    "filing rules and the evidence support it; otherwise discard.\n"
    "Every change is a pull request. Delete drained files from\n"
    ".claims/ in the same PR. Do not invent a layout. Do not write\n"
    "notes under .claims/. Do not edit .claims/README.md. Do not\n"
    "open a PR against any inherited extra clone."
)

# ADR-0030: the app-managed default-board PMO instance (auto-provisioned
# gitea_issues board on the bundled Gitea). Reserved in _pmos_valid; the name
# prefixes branches and run ids like any instance name (ADR-0009).
MANAGED_BOARD_NAME = "board"

# Shared shape for GUI-stored harness/secret-env var names —
# api.connections_service compiles _HARNESS_VAR_RE from this (one
# definition; docs/02 §6).
HARNESS_VAR_PATTERN = r"[A-Z][A-Z0-9_]{0,63}"

# Env names the Dev protocol/runtime owns. The runspec reply layers the
# secret half OVER spec_env (runs.py runspec.result) and the entrypoint
# os.environ.update()s the merge, so a secret_env entry with one of these
# names would corrupt the run instead of configuring a plugin:
# - REDIS_URL/REDIS_USER/REDIS_PASSWORD/TRACEPARENT: the entrypoint's
#   messaging transport — send() re-reads REDIS_PASSWORD per message, so a
#   shadowed value makes every artifact fail auth and the run die
#   timed_out with nothing streamed
# - GH_TOKEN/GITLAB_TOKEN/GITEA_SERVER_TOKEN: the entrypoint overwrites the
#   forge CLI token envs from DEVCAKE_FORGE_TOKEN — an operator value would
#   be silently ignored, so refuse it up front
# test_config_schema.py derives the must-cover set from the live forge
# registry and _protocol_spec_env, so drift fails CI without config
# importing adapters (config stays import-light).
RESERVED_SECRET_ENV = frozenset({
    "PATH", "HOME",
    "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD", "TRACEPARENT",
    "GH_TOKEN", "GITLAB_TOKEN", "GITEA_SERVER_TOKEN",
})
RESERVED_SECRET_ENV_PREFIXES = ("DEVCAKE_", "OTEL_", "GIT_")


def _dedupe_card_names(v: list[str], *, field: str) -> list[str]:
    """Card-granular repo-card names: instance-name shape, no slashes,
    deduped with order preserved (PLAN_MEMORY I5)."""
    out: list[str] = []
    for raw in v or []:
        name = (raw or "").strip()
        if (not name or name == ".." or "/" in name or "\\" in name
                or not re.fullmatch(_INSTANCE_NAME_RE, name)):
            raise ValueError(
                f"{field} {raw!r}: must be a repo-card name "
                f"(lowercase alnum, ≤12 chars, no slashes)")
        if name not in out:
            out.append(name)
    return out


class Assignment(BaseModel):
    dev_type: str = ""
    extra_cli_args: str = ""


class PMOInstance(BaseModel):
    """One configured PMO connection (instances-with-identities; secrets GUI-stored).
    An instance with an empty team_key is VALID BUT IDLE — the poll loop and
    label bootstrap skip it and /health shows it as unconfigured — so an
    empty first boot (and M12's GUI-only setup) is a defined state."""
    name: str = Field("linear", pattern=_INSTANCE_NAME_RE)
    system: str = "linear"          # validated against the adapter registry
    team_key: str = ""
    api_base: str | None = None     # None = the adapter's default API host
    # The instance's repo SET (item 2, founder decision 2026-07-15): the
    # ORDERED list of configured repo names this PMO's missions may target.
    # A `devcake-repo:` marker must name a listed repo; missions without a
    # marker route to the FIRST entry (the default). Empty = every mission
    # routes to its own internal-forge repo. (Replaces v4.0's singular
    # default_repo — refused with a hand-migration hint in _stale_shape_reason.)
    repos: list[str] = Field(default_factory=list)
    # REFERENCE repos (founder request 2026-07-15): configured repo cards
    # cloned read-only into EVERY stage's workspace as consultation material
    # (docs sites' source repos, style guides, …) — never work targets:
    # routing gates a marker that names one, and they are disjoint from the
    # routing set above by validation. Multiple supported, order = listing
    # order in the workspace note.
    reference_repos: list[str] = Field(default_factory=list)
    # Memory notebooks bound to this board (PLAN_MEMORY). Card names, same
    # shape as repos / reference_repos. Pairwise disjoint from those two
    # (I1). Never a work repo except on a Curator board (I2).
    memory_repos: list[str] = Field(default_factory=list)
    # Per-instance intake pause (under the global `AppConfig.intake_paused`
    # master switch). While true, this instance dispatches no NEW runs;
    # in-flight finalization and sweeps continue. Default open so a multi-PMO
    # deployment starts with every configured team active.
    intake_paused: bool = False
    # ADR-0033 D11 (draft field, founder ruling): gates the steward
    # discovery lane + cross-mission delivery for THIS instance's families
    # (families never span instances, so the toggle's unit matches the
    # routing unit). Harvest stays unconditional — the board stays complete
    # for a later toggle-on. Default ON: a default-off feature never
    # generates evaluation data; budgets + family scoping bound the radius.
    discovery_routing: bool = True
    # Per-instance Mission Type → Dev Type overrides (ADR-0019, dual-crew
    # staffing): a present key replaces the global AppConfig.assignments row
    # WHOLESALE — extra_cli_args included, because CLI flags are harness-
    # specific and must never leak from the global row's harness into the
    # override's. An absent key inherits the global row. Empty dict (the
    # default) = this instance staffs exactly like the deployment default.
    assignments: dict[str, Assignment] = Field(default_factory=dict)
    # ADR-0030: app-managed instance (the auto-provisioned default board).
    # Identity fields are canonicalized and the row is re-injected across
    # config PUTs / bundle applies by reconcile_managed_pmos while the
    # bundled provisioner is present; operator-tunable fields (repos,
    # reference_repos, assignments, intake_paused) stay operator-owned.
    managed: bool = False

    @field_validator("memory_repos")
    @classmethod
    def _memory_repos_valid(cls, v):
        return _dedupe_card_names(v, field="memory_repos")

    @field_validator("assignments")
    @classmethod
    def _assignment_overrides_valid(cls, v):
        # delegated to the ONE shared rule (defined after DEFAULT_ASSIGNMENTS,
        # resolved at call time): overrides may be partial, never empty-typed
        validate_assignment_map(v, require_complete=False,
                                context="assignments")
        return v

    @field_validator("system")
    @classmethod
    def _known_system(cls, v):
        # lazy import: config must stay import-light (adapters import security
        # and domain; a top-level import here would risk cycles)
        from .adapters.registry import PMO_SYSTEMS
        if v not in PMO_SYSTEMS:
            raise ValueError(f"unknown PMO system {v!r} — registered: "
                             f"{sorted(PMO_SYSTEMS)}")
        return v

    @property
    def configured(self) -> bool:
        return bool(self.team_key.strip())

    @property
    def api_key(self) -> str:
        # single-mode (schema v4, F5): the operator's key VALUE is stored
        # 0600 under /data/secrets via the GUI — no env-var indirection
        from . import secrets
        return secrets.read_connection_secret("pmo", self.name, "api_key")


def intake_blocks_dispatch(config: "AppConfig", instance: PMOInstance) -> bool:
    """True when NEW dispatches must not start for this PMO instance.

    The global `intake_paused` master switch freezes every instance. Each
    instance's own `intake_paused` freezes only that instance. Direct field
    access: a rename must fail closed, never silently leave intake open.
    Lives here (not api/poll) so domain callers — the harvest kick — share
    the same predicate without importing the API layer.
    """
    if config.intake_paused:
        return True
    return bool(instance.intake_paused)


def _default_forge() -> str:
    # lazy: config stays import-light; the default forge id is registry
    # knowledge, not config knowledge (F1)
    from .adapters.registry import DEFAULT_FORGE
    return DEFAULT_FORGE


def _validate_known_forge(v: str) -> str:
    """Registry check shared by RepoInstance and SkillSource."""
    from .adapters.registry import forges  # lazy: config stays import-light
    if v not in forges():
        raise ValueError(f"unknown forge {v!r} — registered: "
                         f"{sorted(forges())}")
    return v


def _validate_forge_url_shape(v: str) -> str:
    """Forge-neutral host + owner/repo path rule (ISSUES #10). Empty is
    allowed for first-boot / unconfigured cards. Shared by RepoInstance and
    SkillSource so the two connection classes cannot drift."""
    if not v:
        return v
    from urllib.parse import urlsplit
    parts = urlsplit(v if "://" in v else f"https://{v}")
    path = (parts.path or "").strip("/").removesuffix(".git")
    segs = [s for s in path.split("/") if s]
    # every supported forge addresses a repo as at least
    # <owner-or-group>/<repo> (nested groups add more segments)
    if not parts.netloc or len(segs) < 2:
        raise ValueError(
            f"invalid repository URL {v!r}: need a host and an "
            f"owner/repo project path (e.g. https://<host>/owner/repo)")
    return v


class RepoInstance(BaseModel):
    """One configured forge repository (instances-with-identities; secrets GUI-stored).
    An instance with an empty url is valid but unconfigured (first boot)."""
    name: str = Field("main", pattern=_INSTANCE_NAME_RE)
    forge: str = Field(default_factory=_default_forge)  # registry-validated
    url: str = ""
    api_base: str | None = None     # None = the adapter's default API host / the repo's origin
    default_branch: str = "main"
    # Per-repo merge doctrine (docs/03 §4.1, ADR-0020). The last two are inert
    # while auto_merge is OFF: on a merge conflict, route back to EXECUTE to
    # sync + resolve (max 2 attempts) instead of parking on DEVCAKE-MERGE;
    # while a merge is merely not-possible-yet (CI running, mergeability
    # computing) the merge sweep keeps retrying for merge_retry_window_minutes
    # before the human hand-off (0 = immediately). Internal (zero-repo)
    # synthesized instances always set auto_merge=True at provision time.
    auto_merge: bool = False
    auto_resolve_merge_conflicts: bool = True
    merge_retry_window_minutes: int = Field(30, ge=0)
    # Post-approve settle (auto-merge only): wait this many minutes after
    # REVIEW approve before the first auto-merge attempt so sibling
    # discovery-in posts can land; at window end a freshness recheck can
    # re-open REVIEW. 0 = today's immediate merge path. Distinct from
    # merge_retry_window_minutes (forge readiness / CI), which runs after.
    merge_settle_minutes: int = Field(0, ge=0)
    # Token VALUES are GUI-stored 0600 under /data/secrets (schema v4, F5):
    # the `token`/`token_ro`/`reviewer_token` properties read them by instance
    # name. An optional read-only token for non-EXECUTE stages (ISSUES #15);
    # when absent, every stage receives the WRITE token — /health warns.

    @field_validator("forge")
    @classmethod
    def _known_forge(cls, v):
        return _validate_known_forge(v)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        return _validate_forge_url_shape(v)

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())

    @property
    def token(self) -> str:
        from . import secrets
        return secrets.read_connection_secret("repo", self.name, "token")

    @property
    def token_ro(self) -> str:
        from . import secrets
        return secrets.read_connection_secret("repo", self.name, "token_ro")

    @property
    def reviewer_token(self) -> str:
        from . import secrets
        return secrets.read_connection_secret("repo", self.name, "reviewer_token")

    @property
    def reference_only(self) -> bool:
        """Stores a read-only token but NO write token (founder decision
        2026-07-15): a first-class state — the repo serves as reference
        material for every stage and is never a work target. Health treats
        readable-but-not-writable as OK for these; no breaker, no nagging."""
        return bool(self.token_ro) and not self.token


class Concurrency(BaseModel):
    # concurrency caps remain the fleet-level throttle; PER-CONTAINER hard
    # limits are real since the 2026-08-13 dev-run migration to Dagu's
    # docker-executor form (ContainerLimits below — the old "Dagu cannot
    # apply HostConfig limits" era ended with 2.13.0 + the nested-resources
    # decode, docs/07 §7)
    global_max: int = Field(3, ge=1)


class ContainerLimits(BaseModel):
    """Per-Dev-container hard limits (kernel cgroups via Docker HostConfig —
    the 2026-08-13 founder ruling: admin knobs, not static DAG values).
    Applied to BOTH steps of every dev-run (provision + harness) as DAG
    params; **0 = unlimited** (the field is left unset on the engine —
    verified end-to-end). Defaults mirror the old best-effort DAG budget
    (cpu 2 / memory 4g) that these knobs replace."""
    memory_mb: int = Field(4096, ge=0)
    cpus: float = Field(2.0, ge=0)
    pids: int = Field(0, ge=0)


class SkillSource(BaseModel):
    """A dedicated skills connection (founder ruling 2026-08-14,
    superseding ADR-0016-addendum decision 1): a skills repository is
    its own first-class connection over the forge machinery — never a
    facet of a repo card. No PMO can select it, it has no PR surface,
    and it is read-only by construction. Content is served from the
    ADR-0024 mirror read-side exactly as before; Dev Types keep the
    `<source>/<skill>` naming."""
    name: str = Field(pattern=_INSTANCE_NAME_RE)
    forge: str = "github"
    url: str = ""
    default_branch: str = ""
    # optional path inside the repository holding the `<skill>/SKILL.md`
    # dirs ("" = repo root)
    subdir: str = ""

    @field_validator("forge")
    @classmethod
    def _known_forge(cls, v):
        return _validate_known_forge(v)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        return _validate_forge_url_shape(v)

    @field_validator("subdir")
    @classmethod
    def _subdir_relative(cls, v: str) -> str:
        v = v.strip().strip("/")
        if v and (".." in v.split("/") or v.startswith("/")):
            raise ValueError(
                f"subdir {v!r}: relative path inside the repo, no ..")
        return v

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())

    @property
    def token(self) -> str:
        from . import secrets
        return secrets.read_connection_secret("skill", self.name, "token")

    @property
    def token_ro(self) -> str:
        from . import secrets
        return secrets.read_connection_secret("skill", self.name, "token_ro")


# The operator-editable half of the relations steward's instructions
# (founder ask 2026-08-14, reversing the 2026-07-14 "STEWARD stays
# un-templated" ruling — the SAME parallelism as the Memory Curator's
# ticket text). `{mission_table}` is substituted with the live mission
# list; the required result.json contract stays CODE-OWNED and is
# appended by prompts.steward_prompt so an edit cannot break the
# machine half of the run.
STEWARD_RELATIONS_TEMPLATE = (
    "## Your current mission type: RELATIONS STEWARD\n"
    "\n"
    "Below is every open mission DevCake manages in this team. Your ONLY "
    "job is to\n"
    "identify ordering dependencies that are not yet mapped: pairs where "
    "one mission\n"
    "clearly consumes another's output and therefore must not start "
    "before it\n"
    "finishes (implementation after design/documentation, migration "
    "after schema\n"
    "change, consumer after API). Do not modify any code — the "
    "repository clone is\n"
    "context only.\n"
    "\n"
    "Be conservative: when unsure, propose nothing. Each mission lists "
    "its existing\n"
    "blockers — only propose edges that are missing. Never invent "
    "mission keys.\n"
    "\n"
    "### The missions (key · status · existing blockers · title + "
    "description head)\n"
    "{mission_table}"
)


class Steward(BaseModel):
    """The out-of-the-loop Dev class (STEWARD — renamed from MAPPER,
    founder decision 2026-08-06): board-tending runs outside any mission's
    pipeline. Duties: mapping missing blocked-by relations (ADR-0007)
    and discovery routing (ADR-0033). Manual-only by default
    (enabled=False → the admin "Run now" button); the periodic service is
    opt-in. dev_type must name an existing Dev Type whenever enabled — the
    seeded steward (EXECUTE-grade / Opus-class) is the default vehicle."""
    enabled: bool = False
    interval_minutes: int = Field(60, ge=1)
    dev_type: str | None = "steward"
    # operator-owned instruction text; `{mission_table}` marks where the
    # live mission list lands (appended automatically when omitted)
    playbook_template: str = STEWARD_RELATIONS_TEMPLATE


class CronJob(BaseModel):
    """One create-a-labeled-ticket job (PLAN_MEMORY §6). The reserved
    `memory-curator` row is seeded on every AppConfig and re-injected by
    reconcile_reserved_crons; it never picks a product PMO."""
    id: str = Field(pattern=_CRON_ID_RE)
    name: str
    enabled: bool = False
    interval_minutes: int = Field(60, ge=1)
    pmo: str | None = None
    entry_stage: Literal["ONBOARD", "PLAN", "EXECUTE", "REVIEW"]
    description_template: str
    reserved: bool = False

    @model_validator(mode="after")
    def _canonicalize_reserved(self):
        if self.id == MEMORY_CURATOR_CRON_ID:
            self.reserved = True
            self.pmo = None
            self.entry_stage = "EXECUTE"
        else:
            self.reserved = False
            if not (self.pmo or "").strip():
                raise ValueError(
                    f"crons[{self.id}]: non-reserved job requires pmo")
        return self


def memory_curator_seed() -> CronJob:
    """Fresh reserved Memory Curator row (operator may later edit
    enabled / interval / template)."""
    return CronJob(
        id=MEMORY_CURATOR_CRON_ID,
        name="Memory Curator",
        enabled=False,
        interval_minutes=60,
        pmo=None,
        entry_stage="EXECUTE",
        description_template=MEMORY_CURATOR_TEMPLATE,
        reserved=True,
    )


class Budgets(BaseModel):
    """Operator-owned counting budgets (ADR-0033 Decision 7 AS AMENDED,
    founder rulings 2026-08-13): discoveries are a proxy for memory-building
    on an otherwise memoryless system — strictly the memory useful to the
    tasks at hand — so the bounds are knobs the operator sizes to their
    board, not evaluation constants. Devs carry self-regulation guidance in
    the playbooks; these are the backstops. 0 = unlimited, everywhere.
    ROUTING deliberately has no numeric budget (addendum 14): the
    (source, step) delivery dedup and family size already bound fan-out
    structurally, and the steward is the designed judgment layer — a
    numeric reject would only manufacture the spent-budget retry
    pathology."""
    # per-mission-lifetime cap on freshness re-review directives (ADR-0031;
    # shared by human steering posts and routed discoveries alike)
    freshness_rereviews: int = Field(5, ge=0)
    # max discovery entries harvested from one run's result.json
    discoveries_per_run: int = Field(3, ge=0)
    # max .claims/*.json files per notebook (PLAN_MEMORY §5.3). 0 = unlimited.
    # At cap the conveyor refuses the new id (does not evict).
    claims_queue_max: int = Field(50, ge=0)


def migrate_steward_names(data: dict) -> dict:
    """MAPPER→STEWARD one-time raw-config migration (2026-08-06 rename, no
    aliases in code): `relations_mapper` → `steward` top-level key, and the
    "mapper" Dev-Type NAME wherever config references one. Idempotent;
    load_config's normalized write-back persists the result, so the old
    shape disappears from disk on first boot. Runs on EVERY AppConfig
    validation (before-validator), which also covers settings-bundle
    imports and API config patches carrying the old shape."""
    if not isinstance(data, dict):
        return data
    if "relations_mapper" in data and "steward" not in data:
        data["steward"] = data.pop("relations_mapper")
    st = data.get("steward")
    if isinstance(st, dict) and st.get("dev_type") == "mapper":
        st["dev_type"] = "steward"

    def _fix_assignments(assignments) -> None:
        if not isinstance(assignments, dict):
            return
        for row in assignments.values():
            if isinstance(row, dict) and row.get("dev_type") == "mapper":
                row["dev_type"] = "steward"
    _fix_assignments(data.get("assignments"))
    for inst in data.get("pmos") or []:
        if isinstance(inst, dict):
            _fix_assignments(inst.get("assignments"))
    return data


class RepoMirror(BaseModel):
    """Mandatory repo source mirror (ADR-0024). NO on/off switch by design
    (founder decision 2026-08-03): configured repos always clone from the
    app-maintained mirror volume; a fresh sync is a fail-closed dispatch
    precondition. These are the only knobs:
    - sync_max_age_seconds: 0 (default) = sync before EVERY dispatch; N > 0
      accepts a mirror synced within the last N seconds (fewer forge
      requests between rapid-fire mission steps, bounded staleness).
    - lfs: also fetch Git LFS content (default-branch scope) into the
      mirror so Devs get real files instead of pointer files. Default off:
      LFS content can be large, and pointers-without-content is exactly
      today's behavior (dev images gained git-lfs with ADR-0024)."""
    sync_max_age_seconds: int = Field(0, ge=0)
    lfs: bool = False


class ModelRate(BaseModel):
    """One operator rate-card row: USD per 1M tokens, keyed by model prefix
    (longest prefix wins at estimation time — domain/costing.rate_for).
    cache_write defaults to 0: grok reports no cache-write counter, and a
    missing rate must price as free rather than block the whole estimate."""
    model_prefix: str = Field(min_length=1, max_length=64)
    input_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float = Field(ge=0)
    cache_write_per_mtok: float = Field(0.0, ge=0)
    output_per_mtok: float = Field(ge=0)


# xAI public list prices for grok-4.5 (standard, <200k prompt tokens) —
# the one harness with token splits but no native cost_usd. Estimation is
# app-side only; the harness layer stays estimate-free (docs/08 §5).
DEFAULT_MODEL_RATES: list[ModelRate] = [
    ModelRate(model_prefix="grok-4.5", input_per_mtok=2.00,
              cache_read_per_mtok=0.30, output_per_mtok=6.00),
    # Anthropic list prices for Claude Opus 5 (the seeded steward vehicle,
    # ADR-0033 D10) — cache write at the 5m-TTL 1.25× premium; claude
    # harnesses DO report cache-write counters, unlike grok
    ModelRate(model_prefix="claude-opus", input_per_mtok=5.00,
              cache_read_per_mtok=0.50, cache_write_per_mtok=6.25,
              output_per_mtok=25.00),
]

# Bump the -vN suffix whenever DEFAULT_MODEL_RATES change, so a stamped
# feed line ("cost (estimated, builtin-v1)") names an unambiguous vintage.
BUILTIN_RATE_CARD_ID = "builtin-v2"


class CostInputs(BaseModel):
    """Operator cost inputs (ADR-0021): the per-model rate card behind every
    app-side cost estimate, plus the display-override switch. Native
    harness-reported cost_usd stays authoritative and untouched; when
    override_native is on, DISPLAY surfaces (Runs tab, feed) prefer the
    rate-card computation where one is possible."""
    rates: list[ModelRate] = Field(
        default_factory=lambda: [r.model_copy() for r in DEFAULT_MODEL_RATES])
    override_native: bool = False

    @field_validator("rates")
    @classmethod
    def _unique_prefixes(cls, v: list[ModelRate]) -> list[ModelRate]:
        seen: set[str] = set()
        for r in v:
            if r.model_prefix in seen:
                raise ValueError(
                    f"duplicate rate-card model prefix: {r.model_prefix!r}")
            seen.add(r.model_prefix)
        return v

    @property
    def rate_card_id(self) -> str:
        rows = [r.model_dump() for r in self.rates]
        if rows == [r.model_dump() for r in DEFAULT_MODEL_RATES]:
            return BUILTIN_RATE_CARD_ID
        import hashlib
        import json
        canon = json.dumps(rows, sort_keys=True)
        return "operator:" + hashlib.sha256(canon.encode()).hexdigest()[:8]


class DevType(BaseModel):
    """docs/02 §6 — one YAML per Dev Type under /data/config/dev_types/.

    Deliberately slim: the Docker image, credential requirements, and OAuth
    flow all DERIVE from harness_template via harness.HARNESSES — the admin
    panel's harness combobox is authoritative. Unknown YAML keys are ignored
    on load and dropped on the next save (pydantic's default). Allowed
    template ids are HARNESSES keys (docs/16 H2), not a parallel Literal."""
    # no ":" — dev-type breakers share /health's circuit_breakers map with
    # per-repo `repo:<name>` entries (M10); a colon would let a dev type
    # collide with (and mask) a repo breaker
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    harness_template: str
    identifying_prompt: str = ""
    # Operator shell. Additive after aiming unless override_harness_adapter.
    # Legacy YAML `mcp_setup_commands` (list of lines) joins into this on read.
    dev_entrypoint: str = ""
    # When True, dialect argv is not launched — the script is the process.
    override_harness_adapter: bool = False
    # Skill-store skills installed to the harness's registry-declared skills
    # dir before the harness starts (harness.py skills_dir; a harness
    # without one skips them with a warning). Selected = Available
    # (consult-optional); skills_required is a subset that also gets a
    # soft-force "must consult" prompt append (docs/02 §6).
    skills: list[str] = Field(default_factory=list)
    skills_required: list[str] = Field(default_factory=list)
    # Named secret env vars delivered to this Dev Type's runs: NAMES only —
    # values are GUI-stored under /data/secrets/harness/ (ADR-0011) and read
    # at runspec time, so mcp_setup_commands can reference e.g. $DD_API_KEY
    # without a secret value ever touching config.yaml.
    secret_env: list[str] = Field(default_factory=list)
    # Memory notebooks bound to this Dev Type (domain-bound, PLAN_MEMORY).
    # Card names; deduped, order preserved. Empty default.
    memory_repos: list[str] = Field(default_factory=list)
    max_concurrency: int = Field(1, ge=1)
    model: str = ""  # harness model override (e.g. claude-fable-5); "" = harness default
    # OpenAI-compatible / Anthropic-compatible backend. Empty = vendor
    # default (no aim). Non-empty → entrypoint aim() writes env/argv/files.
    backend_base_url: str = ""
    # Coding-harness binary pin (not DEVCAKE_TAG, not model). Empty = house
    # Dockerfile ARG. `latest` is a resolve-once gesture, never stored.
    cli_version: str = ""

    @staticmethod
    def _dedupe_skill_names(v: list[str], *, field: str) -> list[str]:
        # `<card>/<skill>` selects a skill from an EXTERNAL skill source
        # (ADR-0016 addendum): one slash max, prefix instance-shaped.
        # SKILL_NAME_RE (store/builtin authoring) stays slash-free, so the
        # namespaces are structurally disjoint — no precedence rules exist.
        pattern = skill_ref_pattern()
        out: list[str] = []
        for name in v:
            if not re.fullmatch(pattern, name):
                raise ValueError(
                    f"{field} name {name!r}: lowercase alnum with - or _ "
                    "(≤64 chars), starting alphanumeric; external skills as "
                    "<repo-card>/<skill>")
            if name not in out:
                out.append(name)
        return out

    @model_validator(mode="before")
    @classmethod
    def _legacy_mcp_setup_commands(cls, data):
        if not isinstance(data, dict):
            return data
        script = data.get("dev_entrypoint")
        if isinstance(script, str) and script.strip():
            return data
        legacy = data.get("mcp_setup_commands")
        if isinstance(legacy, list) and legacy:
            data = dict(data)
            data["dev_entrypoint"] = "\n".join(
                str(x) for x in legacy if str(x).strip())
        elif isinstance(legacy, str) and legacy.strip():
            data = dict(data)
            data["dev_entrypoint"] = legacy
        return data

    @property
    def mcp_setup_commands(self) -> list[str]:
        """Line view of the entrypoint script — secret-ref scans + old callers."""
        return [ln for ln in self.dev_entrypoint.splitlines() if ln.strip()]

    @field_validator("harness_template")
    @classmethod
    def _known_harness_template(cls, v: str) -> str:
        from .harness import HARNESSES
        if v not in HARNESSES:
            raise ValueError(
                f"unknown harness_template {v!r}; known: {sorted(HARNESSES)}")
        return v

    @field_validator("memory_repos")
    @classmethod
    def _memory_repos_valid(cls, v):
        return _dedupe_card_names(v, field="memory_repos")

    @field_validator("skills")
    @classmethod
    def _skill_names_valid(cls, v):
        return cls._dedupe_skill_names(v, field="skill")

    @field_validator("skills_required")
    @classmethod
    def _skill_required_names_valid(cls, v):
        return cls._dedupe_skill_names(v, field="skills_required")

    @field_validator("backend_base_url")
    @classmethod
    def _strip_backend_url(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("cli_version")
    @classmethod
    def _cli_version_is_empty_or_semver(cls, v: str) -> str:
        pin = (v or "").strip()
        if pin.lower() == "latest":
            raise ValueError(
                "cli_version cannot be 'latest' — resolve the remote "
                "number first, then store that semver")
        if pin:
            from .versions import CLI_VERSION_SEMVER_RE
            if not CLI_VERSION_SEMVER_RE.fullmatch(pin):
                raise ValueError(
                    "cli_version must be empty (house pin) or a semver "
                    "like 2.1.250")
        return pin

    @model_validator(mode="after")
    def _skills_required_subset(self):
        have = set(self.skills)
        missing = [n for n in self.skills_required if n not in have]
        if missing:
            raise ValueError(
                f"skills_required must be a subset of skills; not installed: "
                f"{', '.join(missing)}")
        return self

    @model_validator(mode="after")
    def _skill_basenames_unique(self):
        """Materialization dir = the BASENAME (external payload paths are
        flattened so harness discovery stays untouched) — so `tdd` and
        `myrepo/tdd` selected together would fight over one directory."""
        seen: dict[str, str] = {}
        for name in self.skills:
            base = name.rsplit("/", 1)[-1]
            if base in seen and seen[base] != name:
                raise ValueError(
                    f"skills {seen[base]!r} and {name!r} share the install "
                    f"dir {base!r} — pick one per Dev Type")
            seen.setdefault(base, name)
        return self

    @field_validator("secret_env")
    @classmethod
    def _secret_env_names(cls, v: list[str]) -> list[str]:
        """The runspec reply merges the secret half OVER spec_env (runs.py
        runspec.result), so an unguarded name could shadow the Dev protocol
        contract. Shape mirrors api.connections_service._HARNESS_VAR_RE —
        the store these names read from."""
        seen: set[str] = set()
        for name in v:
            if not re.fullmatch(HARNESS_VAR_PATTERN, name):
                raise ValueError(
                    f"secret env var {name!r} must be UPPER_SNAKE_CASE "
                    "([A-Z][A-Z0-9_]*, max 64 chars)")
            if name in seen:
                raise ValueError(f"duplicate secret env var {name!r}")
            seen.add(name)
            if (name in RESERVED_SECRET_ENV
                    or name.startswith(RESERVED_SECRET_ENV_PREFIXES)):
                raise ValueError(
                    f"secret env var {name!r} would shadow the Dev "
                    "protocol/tooling env — pick a different name")
        return v


DEFAULT_ASSIGNMENTS = {
    "ONBOARD": Assignment(dev_type="judgment", extra_cli_args="--max-turns 15"),
    "PLAN": Assignment(dev_type="judgment"),
    "EXECUTE": Assignment(dev_type="implementer"),
    "REVIEW": Assignment(dev_type="judgment"),
}


def validate_assignment_map(rows: dict[str, Assignment], *,
                            require_complete: bool, context: str) -> None:
    """The ONE assignment-map shape rule for every entry point (2026-08-12
    audit SEC-3): the AppConfig field validator (boot, PUT /config, bundle
    validate, and reconcile all funnel through model_validate), the
    per-instance override validator, and PUT /assignments. The old split —
    endpoint-only completeness, no model rule — let a hand-edited
    config.yaml boot green and KeyError inside the poll cycle at
    assignment_for()."""
    unknown = set(rows) - set(DEFAULT_ASSIGNMENTS)
    if unknown:
        raise ValueError(
            f"{context}: unknown mission type(s) {sorted(unknown)} — "
            f"valid keys: {sorted(DEFAULT_ASSIGNMENTS)}")
    empty = sorted(mt for mt, a in rows.items() if not a.dev_type)
    if empty:
        if require_complete:
            raise ValueError(
                f"{context}[{', '.join(empty)}]: an assignment must name a "
                f"Dev Type")
        raise ValueError(
            f"{context}[{', '.join(empty)}]: an override must name a "
            f"Dev Type — remove the key to inherit the global assignment")
    if require_complete:
        missing = sorted(set(DEFAULT_ASSIGNMENTS) - set(rows))
        if missing:
            raise ValueError(
                f"{context}: unassigned mission types {missing} — add rows "
                f"for them, or delete the `assignments:` key entirely to "
                f"restore the defaults")


def assignment_for(config: "AppConfig", instance: PMOInstance,
                   mission_type: str) -> Assignment:
    """The Assignment staffing `mission_type` on `instance` (ADR-0019): the
    instance's override row wholesale when present, else the global row.
    Never mixes fields across the two — extra_cli_args are harness-specific
    and belong to whichever row named the Dev Type."""
    override = instance.assignments.get(mission_type)
    return override if override is not None else config.assignments[mission_type]

# docs/03 §7 — canonical identifying prompts (seed data; admin-editable)
JUDGMENT_PROMPT = (
    "You are **Judgment**, DevCake's judgment-heavy engineer. You assess, plan, and "
    "review software work with the skepticism of a staff engineer who has been burned "
    "before. You are precise about scope: you do exactly what your current mission "
    "playbook asks — no more. You never invent requirements, you flag what you cannot "
    "verify, and you write conclusions that a teammate can act on without asking "
    "follow-up questions."
)
IMPLEMENTER_PROMPT = (
    "You are **Implementer**, DevCake's implementation engineer. You turn plans into "
    "working, tested code. You follow the plan you are given; where reality contradicts "
    "the plan, you implement the smallest sound deviation and document it prominently "
    "in your summary. You match the conventions of the codebase you are in, you run "
    "the tests, and you never commit until the work is complete. Do exactly what your "
    "current mission playbook asks."
)
STEWARD_PROMPT = (
    "You are **Steward**, DevCake's board-tending engineer. You reason "
    "about a whole team's missions at once — ordering dependencies and "
    "cross-mission relevance — with the judgment of a staff engineer and "
    "the restraint of a librarian: you follow output formats exactly, you "
    "propose only what the evidence supports, and when you are unsure you "
    "propose nothing. Do exactly what your current mission playbook asks."
)

DEFAULT_DEV_TYPES = [
    DevType(name="judgment", harness_template="claude-code",
            identifying_prompt=JUDGMENT_PROMPT, max_concurrency=2,
            model="claude-fable-5"),  # founder decision 2026-07-12: judgment runs on Fable
    DevType(name="implementer", harness_template="grok-build",
            identifying_prompt=IMPLEMENTER_PROMPT, max_concurrency=2),
    # EXECUTE-grade for BOTH steward duties (ADR-0033 D10, founder ruling):
    # a wrong blocked_by edge silently reorders a family's execution, and
    # discovery routing is family-wide relevance judgment — the steward
    # class carries at least the bar EXECUTE demands. Same harness/
    # credentials as judgment; fresh boots only (existing deployments keep
    # their configured staffing and upgrade via the admin UI).
    DevType(name="steward", harness_template="claude-code",
            identifying_prompt=STEWARD_PROMPT, max_concurrency=1,
            model="claude-opus-5"),
]


class AppConfig(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _migrate_steward_names(cls, data):
        return migrate_steward_names(data)

    # v4 (M12): secret VALUES are GUI-stored, not env-referenced; a truly
    # empty first boot (pmos/repos both empty) is a defined idle state
    schema_version: int = 4
    pmos: list[PMOInstance] = Field(default_factory=list)
    repos: list[RepoInstance] = Field(default_factory=list)
    # deep copy: rename_dev_type edits rows IN PLACE, so shared Assignment
    # objects would write through to DEFAULT_ASSIGNMENTS for the process
    # lifetime and leak into every later AppConfig()
    assignments: dict[str, Assignment] = Field(
        default_factory=lambda: {k: v.model_copy()
                                 for k, v in DEFAULT_ASSIGNMENTS.items()})

    @field_validator("assignments")
    @classmethod
    def _assignments_valid(cls, v):
        # the global map must staff EVERY mission type with a named Dev Type
        # (SEC-3: assignment_for indexes it directly on the dispatch path)
        validate_assignment_map(v, require_complete=True,
                                context="assignments")
        return v
    concurrency: Concurrency = Field(default_factory=Concurrency)
    adoption_mode: Literal["opt_in", "opt_out"] = "opt_in"
    poll_interval_seconds: int = Field(30, ge=1, le=3600)
    dev_timeout_minutes: int = Field(120, ge=1, le=24 * 60)
    max_attempts: int = Field(3, ge=1, le=50)
    # ADR-0026 — what grants a step FRESH attempts (and whether give-up exists
    # at all). The pre-0026 rule — ANY non-DevCake comment resets the count —
    # let any chatty integration (a sync bot, a CI notifier) keep the counter
    # at 1 forever, defeating max_attempts and unbounding token spend.
    #   label-ops (default): only removing DEVCAKE-FAILED or a later step
    #     finishing resets the count — plus a comment containing the literal
    #     DEVCAKE-RETRY, the deliberate human gesture integrations never emit
    #     (pre-give-up there is no label to remove, so strict mode needs one).
    #   any-comment: the pre-0026 behavior, for boards with no bot traffic.
    #   unlimited: the app NEVER applies DEVCAKE-FAILED (breakers still act;
    #     DEVCAKE-SKIP still stops everything). A loop-style warning with
    #     cumulative cost posts every review_loop_warning_every failures so
    #     the mode is loud. For operators whose token cost is measured in
    #     watts, by explicit choice.
    attempt_reset: Literal["label-ops", "any-comment",
                           "unlimited"] = "label-ops"
    # ADR-0026 — widen the backend brake (ADR-0018) to exit-11 DEV_BAD_OUTPUT
    # evidence. Default OFF (founder decision 2026-08-04: current design
    # stands unless the operator opts in). ON makes a shared-backend garbage
    # cascade (the 2026-07-24 shape: every container talks but none writes
    # result.json) correlate across missions — excusing attempts and
    # throttling to one probe — instead of burning the board to
    # DEVCAKE-FAILED. The continuation loop (ADR-0022) already absorbs most
    # solitary narrate-and-stop exit-11s; this covers what survives it.
    brake_on_bad_output: bool = False
    # ADR-0018 — Devs are told to write /workspace/out/result.json. With this on,
    # a result file the Dev wrote elsewhere in its workspace is still accepted,
    # but only when it was created during that run and passes the same
    # validation. The misplacement is recorded either way, so turning this off
    # costs diagnosis nothing — it only stops DevCake acting on the stray.
    recover_misplaced_result: bool = True
    # ADR-0022 — when a harness exits 0 with no fault but never wrote
    # result.json (the narrate-and-stop shape), the Dev entrypoint relaunches
    # the harness in the same container with a contract-reminder nudge instead
    # of failing the attempt. `auto` resumes the session when the harness has
    # a capture-verified resume (RESUME_SPECS) and escalates permanently to a
    # fresh session after a zero-progress continuation; the integer budget is
    # the ONLY terminator (founder decision 2026-08-02 — stalls escalate,
    # never stop, so large experimental budgets run to completion). Plan mode
    # never continues. NOTE: each relaunch resets the CLI's own --max-turns,
    # so the effective turn budget is (max_continuations + 1) × max-turns.
    continuation_policy: Literal["auto", "resume-only", "fresh-only",
                                 "off"] = "auto"
    # deliberately no upper bound (unlike max_attempts): large budgets (10,
    # 50) are a legitimate experiment, bounded by dev_timeout_minutes
    max_continuations: int = Field(2, ge=0)
    # ge=1: used as a modulo cadence; 0 would ZeroDivisionError (ISSUES #8/#9)
    review_loop_warning_every: int = Field(3, ge=1)
    # After a REVIEW-approved merge, also zip the PR change set onto the PMO
    # feed for CONFIGURED (external) work repos. Internal/zero-repo missions
    # always zip (ADR-0010) regardless of this flag. Default OFF: the forge
    # PR is the canonical artifact for eng repos; zips are merge-time
    # snapshots (can omit large files, dual-truth vs main, secrets risk).
    # (Merge doctrine — auto_merge / auto-resolve / retry window — lives on
    # each RepoInstance; see ADR-0020.)
    attach_merged_changeset_to_pmo: bool = False
    # operator switch: no NEW runs dispatch while paused; in-flight runs finish
    # and sweeps keep running (docs/11)
    intake_paused: bool = False
    # how many generations of ONBOARD decomposition are allowed below a root
    # mission (ADR-0012). 0 = unlimited — the ONBOARD Dev decides; removes
    # the fission backstop by explicit operator choice (docs/03 §1.3)
    max_decomposition_depth: int = Field(2, ge=0)
    steward: Steward = Field(default_factory=Steward)
    # PLAN_MEMORY: skill-source + memory-mount fail-closed (default ON).
    context_sourcing_strict: bool = True
    # PLAN_MEMORY: OFF enforces a person at the merge chokepoint for any
    # memory-bound card. ON is two-model consent, not a person.
    memory_auto_merge: bool = False
    # Dedicated skills connections (2026-08-14 ruling) — never repo cards.
    skill_sources: list[SkillSource] = Field(default_factory=list)
    # Cron module. The reserved memory-curator row is always present —
    # a PUT/bundle that omits it is healed by reconcile_reserved_crons
    # (and the field validator injects the seed if the list is empty).
    crons: list[CronJob] = Field(default_factory=lambda: [memory_curator_seed()])
    # counting budgets (ADR-0033 D7 as amended) — see the Budgets docstring
    budgets: Budgets = Field(default_factory=Budgets)
    # per-Dev-container cgroup limits (2026-08-13) — see ContainerLimits
    container_limits: ContainerLimits = Field(default_factory=ContainerLimits)
    # ADR-0024 — mandatory source mirror; see the RepoMirror docstring
    repo_mirror: RepoMirror = Field(default_factory=RepoMirror)
    # operator rate card + display-override switch for app-side cost
    # estimates (ADR-0021); edited via the Runs page "Cost inputs" modal
    cost_inputs: CostInputs = Field(default_factory=CostInputs)
    # per-Mission-Type ACTIVE prompt template (v0.1.1): missing key ⇒ the
    # built-in "default". A dict map (deep_merge-safe: a patch touching one
    # type preserves siblings; reset = PUT the value "default"). Name
    # existence is validated in the PUT endpoint, not here (no disk I/O in
    # validators).
    active_prompt_templates: dict[str, str] = Field(default_factory=dict)
    # per-Dev-Type ACTIVE identifying-prompt template (2026-07-15): missing
    # key ⇒ "Development" (the dev type's original prompt, seeded once)
    active_devtype_prompts: dict[str, str] = Field(default_factory=dict)
    # admin-UI state: dismissed advisory alerts as "id:signature" strings.
    # A list (not a dict) on purpose — deep_merge can't delete dict keys, so
    # the UI un-dismisses by PUTting the whole replacement list.
    dismissed_alerts: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _current_version(cls, v):
        # ONE generic refusal for every stale version (v1, v2, future) —
        # no bespoke per-version detectors (docs/10 §3 has the migration
        # recipe; there are no deployments to auto-migrate for)
        if v != 4:
            raise ValueError(
                f"config schema_version {v} is not the current version (4) — "
                "hand-migrate per docs/10 §3 or delete the file and "
                "reconfigure via the admin panel")
        return v

    @field_validator("pmos")
    @classmethod
    def _pmos_valid(cls, v):
        # 0..N PMO instances (M12): empty = idle first boot (GUI-only setup)
        names = [e.name for e in v]
        if len(set(names)) != len(names):
            raise ValueError("pmos: duplicate instance names")
        # PMO-side only (audit A15): 'main' marks legacy pre-v3 run records
        # (Run.pmo_ref default) — a live instance named main would adopt
        # every legacy record directly and count them as its own in the
        # in-flight guard; 'sys' is the HELLO/OAUTH pseudo-instance in run
        # ids. Repo names stay free ('main' is the default repo card name).
        reserved = {"main", "sys"} & set(names)
        if reserved:
            raise ValueError(
                f"pmos: reserved instance name(s) {sorted(reserved)} — "
                f"'main' marks legacy run records, 'sys' the HELLO/OAUTH "
                f"pseudo-instance; pick another name")
        # ADR-0030: 'board' is the app-managed default-board instance —
        # operators cannot claim the name with an ordinary row (the managed
        # row itself, stamped by the app or carried by a bundle, validates)
        for e in v:
            if e.name == MANAGED_BOARD_NAME and not (
                    e.managed and e.system == "gitea_issues"):
                raise ValueError(
                    f"pmos: {MANAGED_BOARD_NAME!r} is reserved for the "
                    f"auto-provisioned default board (ADR-0030) — pick "
                    f"another name")
        # two instances polling the same underlying team would double-
        # dispatch every mission under two identity prefixes — refuse
        targets = [(e.system, e.api_base, e.team_key) for e in v if e.configured]
        if len(set(targets)) != len(targets):
            raise ValueError("pmos: two instances target the same "
                             "(system, api_base, team_key) — this would "
                             "double-dispatch every mission")
        return v

    @field_validator("repos")
    @classmethod
    def _repos_valid(cls, v):
        # 0..N repos (M10): zero = missions resolve via the internal
        # fallback forge when available (M11 shipped); otherwise gated
        names = [e.name for e in v]
        if len(set(names)) != len(names):
            raise ValueError("repos: duplicate instance names")
        urls = [(e.forge, e.url.rstrip("/").removesuffix(".git").lower())
                for e in v if e.configured]
        if len(set(urls)) != len(urls):
            raise ValueError("repos: two entries target the same repository "
                             "(trailing '/' and '.git' are ignored)")
        return v

    @field_validator("crons")
    @classmethod
    def _crons_valid(cls, v):
        rows = list(v or [])
        ids = [c.id for c in rows]
        if len(set(ids)) != len(ids):
            raise ValueError("crons: duplicate job ids")
        if not any(c.id == MEMORY_CURATOR_CRON_ID for c in rows):
            rows.append(memory_curator_seed())
        return rows

    @model_validator(mode="after")
    def _pmo_repo_sets_valid(self):
        repo_names = {r.name for r in self.repos}
        src_names = [x.name for x in self.skill_sources]
        if len(set(src_names)) != len(src_names):
            raise ValueError("skill_sources: duplicate names")
        overlap = set(src_names) & repo_names
        if overlap:
            # the mirror cache keys both kinds by name — one namespace
            raise ValueError(
                f"skill_sources {sorted(overlap)} collide with repository "
                f"card names — pick distinct names")
        for p in self.pmos:
            for field in ("repos", "reference_repos", "memory_repos"):
                names = getattr(p, field)
                if len(set(names)) != len(names):
                    raise ValueError(f"pmos[{p.name}].{field}: duplicate entries")
                unknown = [n for n in names if n not in repo_names]
                if unknown:
                    raise ValueError(
                        f"pmos[{p.name}].{field} {unknown} name no configured "
                        f"repo (have: {sorted(repo_names)})")
            # work∩reference disjointness lives in validate_memory_bindings
            # (called next) — one chokepoint, not a parallel copy here.
        validate_memory_bindings(self)
        pmo_names = {p.name for p in self.pmos}
        for job in self.crons:
            if not job.reserved and job.pmo not in pmo_names:
                raise ValueError(
                    f"crons[{job.id}].pmo {job.pmo!r} names no configured "
                    f"instance (have: {sorted(pmo_names)})")
        return self


def _stale_shape_reason(data: dict) -> str | None:
    """ONE detector for every stale config shape — file loads and PUT bodies
    share it (a divergent copy was a review finding). Detects v1 (singular
    pmo:/repo: dicts), v2 (id-keyed instance entries), and explicit old
    schema_versions. Returns the human reason, or None when current."""
    stale = [k for k in ("pmo", "repo") if isinstance(data.get(k), dict)]
    if stale:
        return (f"singular {'/'.join(stale)!s} config keys are schema v1; "
                "the current v4 shape is pmos:/repos: name-keyed lists")
    for key in ("pmos", "repos"):
        entries = data.get(key)
        if isinstance(entries, list):
            if any(isinstance(e, dict) and "id" in e for e in entries):
                return (f"{key} entries carry the v2 'id' field; schema v4 "
                        "uses operator-chosen 'name' identities")
            # v3→v4: env-name references replaced by GUI-stored secret values
            if any(isinstance(e, dict) and (
                    "api_key_env" in e or "token_env" in e or
                    "token_ro_env" in e or "reviewer_token_env" in e)
                   for e in entries):
                return (f"{key} entries carry v3 *_env fields; schema v4 stores "
                        "secret VALUES via the Config page — remove the *_env "
                        "keys and re-enter secrets in the admin panel")
    # v4.0→v4.1 (item 2): singular default_repo became the ordered repo SET
    if any(isinstance(e, dict) and "default_repo" in e
           for e in (data.get("pmos") or []) if isinstance(e, dict)):
        return ("pmos entries carry the pre-repo-set 'default_repo' field — "
                "replace it with `repos: [<name>, …]` (ordered; the first "
                "entry is the default for unmarked missions)")
    if data.get("schema_version") not in (None, 4):
        return (f"schema_version {data['schema_version']} is stale — the "
                "current version is 4")
    return None


def reject_stale_patch(body: dict) -> None:
    """Refuse stale-schema PUT bodies loudly. Load-bearing, not defensive:
    pydantic ignores unknown keys, so without this a stale client's PUT
    would silently DROP the operator's edit instead of failing."""
    reason = _stale_shape_reason(body)
    if reason:
        raise ValueError(f"{reason} (hand-migration recipe: docs/10 §3)")


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursive dict merge for partial config PUTs (docs/11 §1): a nested
    patch like {"concurrency": {"global_max": …}} must not silently reset
    sibling fields to their defaults. NOTE: lists (pmos/repos/dev-type
    names) replace WHOLESALE — a partial body omitting an instance deletes
    it (and, at v4, its stored secrets)."""
    merged = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _iter_dev_types(dev_types) -> list:
    if not dev_types:
        return []
    if isinstance(dev_types, dict):
        return list(dev_types.values())
    return list(dev_types)


def memory_bound_names(cfg: "AppConfig", dev_types=None) -> set[str]:
    """Set M: every card named in any instance or Dev Type memory_repos."""
    names: set[str] = set()
    for p in cfg.pmos:
        names.update(p.memory_repos or [])
    for dt in _iter_dev_types(dev_types):
        names.update(getattr(dt, "memory_repos", None) or [])
    return names


def is_memory_bound(cfg: "AppConfig", name: str, dev_types=None) -> bool:
    """True when `name` is a memory notebook anywhere (PLAN_MEMORY §4.1).

    A card listed in any instance or Dev Type `memory_repos` is bound.
    A lone product work-repo is not — `repos == [webapp]` without a
    memory listing is just a one-repo board.
    """
    return name in memory_bound_names(cfg, dev_types)


def auto_merge_permitted(cfg: "AppConfig", inst, repo_name: str,
                         dev_types=None) -> bool:
    """Merge chokepoint (PLAN_MEMORY §4.1): card `auto_merge` AND, for a
    memory-bound target, `memory_auto_merge`. OFF leaves the mission in
    the human-await / DEVCAKE-MERGE state. App commits under `.claims/`
    do not go through this gate."""
    if not getattr(inst, "auto_merge", False):
        return False
    if is_memory_bound(cfg, repo_name, dev_types) and not cfg.memory_auto_merge:
        return False
    return True


def validate_memory_bindings(cfg: "AppConfig", dev_types=None) -> None:
    """I1 + I2 (PLAN_MEMORY §2.5). Called from AppConfig validation
    (instance-side) and from validate_config_semantics / Dev Type PUT
    with the live Dev Type map so domain-bound names join set M."""
    for p in cfg.pmos:
        repos, refs, mems = (set(p.repos), set(p.reference_repos),
                             set(p.memory_repos))
        overlap_wr = repos & refs
        if overlap_wr:
            raise ValueError(
                f"pmos[{p.name}]: {sorted(overlap_wr)} cannot be both a "
                f"work repo and a reference repo — reference repos are "
                f"read-only context, never routing targets")
        overlap_wm = repos & mems
        if overlap_wm:
            raise ValueError(
                f"pmos[{p.name}]: {sorted(overlap_wm)} cannot be both a "
                f"work repo and a memory notebook")
        overlap_rm = refs & mems
        if overlap_rm:
            raise ValueError(
                f"pmos[{p.name}]: {sorted(overlap_rm)} cannot be both a "
                f"reference repo and a memory notebook")
        # memory_repos duplicates are refused by _dedupe_card_names on the
        # field (and again in AppConfig._pmo_repo_sets_valid) — no second
        # refuse story here.
    M = memory_bound_names(cfg, dev_types)
    for p in cfg.pmos:
        for m in sorted(M):
            if m in p.repos and list(p.repos) != [m]:
                raise ValueError(
                    f"pmos[{p.name}]: {m!r} is memory-bound and cannot be "
                    f"a work repo among others — a Curator board lists "
                    f"only that notebook (repos == [{m!r}])")


def reconcile_reserved_crons(current: list[dict],
                             incoming: list[dict]) -> list[dict]:
    """Keep the reserved memory-curator row across wholesale list replaces
    (config PUT, bundle apply) — same shape as reconcile_managed_pmos.

    - omitted reserved row is re-injected from live (or the seed);
    - identity fields (id/reserved/pmo/entry_stage) are canonical;
    - operator tunables (name/enabled/interval/template) come from the
      incoming row when present, else the live row;
    - a stray `reserved: true` on any other row is stripped.
    """
    live_by_id = {c["id"]: c for c in (current or [])
                  if isinstance(c, dict) and c.get("id")}
    live = live_by_id.get(MEMORY_CURATOR_CRON_ID)
    if live is None:
        live = memory_curator_seed().model_dump()
    out: list[dict] = []
    seen = False
    for raw in (incoming or []):
        if not isinstance(raw, dict):
            out.append(raw)
            continue
        row = dict(raw)
        if row.get("id") == MEMORY_CURATOR_CRON_ID:
            row["id"] = MEMORY_CURATOR_CRON_ID
            row["reserved"] = True
            row["pmo"] = None
            row["entry_stage"] = "EXECUTE"
            for field in ("name", "enabled", "interval_minutes",
                          "description_template"):
                if field not in row:
                    row[field] = live.get(field)
            seen = True
        elif row.get("reserved"):
            log.warning("crons[%r]: stripping stray reserved flag — only "
                        "%r may be reserved", row.get("id"),
                        MEMORY_CURATOR_CRON_ID)
            row["reserved"] = False
        out.append(row)
    if not seen:
        log.info("re-injecting reserved cron %r omitted by the incoming "
                 "list", MEMORY_CURATOR_CRON_ID)
        out.append(dict(live))
    return out


def reconcile_managed_pmos(current: list[dict], incoming: list[dict], *,
                           internal_forge_present: bool) -> list[dict]:
    """ADR-0030: keep app-managed PMO rows coherent across the two world-swap
    write paths (config PUT, bundle/profile apply — `pmos` is replaced
    WHOLESALE by both, and profiles saved before the feature simply don't
    contain the row; without this, applying one silently deletes the board
    instance AND its stored PAT via the removed-instance cleanup).

    Pure list[dict] → list[dict]:
    - a live managed row OMITTED by the incoming list is re-injected — while
      the bundled provisioner is present; with it absent, deletion is allowed
      (an undeletable red card on a torn-out Gitea would be worse doctrine);
    - a live managed row PRESENT in the incoming list keeps its identity
      fields canonical (name/system/team_key/api_base/managed come from the
      live row) while operator-tunable fields (repos, reference_repos,
      assignments, intake_paused) stay the incoming row's;
    - a stray `managed: true` on any OTHER incoming row is stripped (logged)
      — fake managed rows would otherwise gain delete-protection; stripping
      instead of refusing keeps cross-stack bundle imports applyable.
    """
    managed_live = {p["name"]: p for p in (current or [])
                    if isinstance(p, dict) and p.get("managed")}
    out: list[dict] = []
    seen: set[str] = set()
    for p in (incoming or []):
        if not isinstance(p, dict):
            out.append(p)
            continue
        row = dict(p)
        name = row.get("name")
        live = managed_live.get(name)
        if live is not None:
            for field in ("name", "system", "team_key", "api_base", "managed"):
                row[field] = live.get(field)
            seen.add(name)
        elif row.get("managed") and name != MANAGED_BOARD_NAME:
            log.warning("pmos[%r]: stripping stray managed flag — managed "
                        "rows are stamped by the app (ADR-0030)", name)
            row["managed"] = False
        out.append(row)
    for name, live in managed_live.items():
        if name not in seen and internal_forge_present:
            log.info("re-injecting managed PMO instance %r omitted by the "
                     "incoming list (ADR-0030)", name)
            out.append(dict(live))
    return out


def _atomic_yaml(path: Path, data: dict) -> None:
    """tmp + fsync + replace; unlink temp on any failure after mkstemp
    (same atomic-write cleanup contract as secrets._atomic_write_bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — temp cleanup then re-raise (atomic write contract)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _refuse_stale_file(data: dict) -> None:
    """Refuse a stale-schema config.yaml loudly at boot (docs/10 §3): the
    auto-migrations were removed (v0 crystallization; no deployments exist),
    and silently validating stale data would reset the operator's connections
    to defaults (pydantic ignores unknown keys). Detection is by SHAPE first
    (a hand-written current file without schema_version stays fine)."""
    reason = _stale_shape_reason(data)
    if reason:
        raise RuntimeError(
            f"{CONFIG_PATH}: {reason} — hand-migrate per docs/10 §3 or "
            "delete the file and reconfigure via the admin panel")


# Former top-level merge doctrine keys (pre-ADR-0020). Dropped silently by
# pydantic unless we shout — operators with auto_merge:true lose auto-merge
# until they re-enable per repo card.
_LEGACY_MERGE_DOCTRINE_KEYS = frozenset({
    "auto_merge", "auto_resolve_merge_conflicts", "merge_retry_window_minutes",
})


def auto_merge_flipped_on(
    previous_repos: list, new_repos: list,
) -> set[str]:
    """Repo names whose auto_merge went OFF→ON (ADR-0020 re-arm set).

    Accepts model_dump dicts or RepoInstance rows for either side so PUT and
    bundle apply can share one comparison.
    """
    def _name_on(r) -> tuple[str | None, bool]:
        if isinstance(r, dict):
            return r.get("name"), bool(r.get("auto_merge"))
        return getattr(r, "name", None), bool(getattr(r, "auto_merge", False))

    prev_on: set[str] = set()
    for r in previous_repos:
        name, on = _name_on(r)
        if name and on:
            prev_on.add(name)
    flipped: set[str] = set()
    for r in new_repos:
        name, on = _name_on(r)
        if name and on and name not in prev_on:
            flipped.add(name)
    return flipped


def apply_auto_merge_rearm(previous_repos: list, new_repos: list,
                           managers) -> set[str]:
    """Union OFF→ON repo names into every manager's rearm set (ADR-0020).

    THE re-arm implementation for both world-swap paths — config PUT and
    profile/bundle apply. Touches only plain manager attributes, so it can
    live here without pulling orchestrator imports into config.
    """
    if not managers:
        return set()
    flipped = auto_merge_flipped_on(previous_repos, new_repos)
    if not flipped:
        return set()
    for mgr in managers.values():
        mgr.rearm_merge_repos |= flipped
    log.info("auto_merge flipped ON for repo(s) %s — parked "
             "DEVCAKE-MERGE missions on those repos re-armed for the "
             "deferred-merge sweep", sorted(flipped))
    return flipped


def warn_unknown_top_level_keys(data: dict) -> None:
    """Pre-v1: no migration — pydantic drops unknown fields. Surface them so
    a silent default is not a quiet no-op (docs/10 §3, ADR-0020)."""
    known = set(AppConfig.model_fields)
    dropped = sorted(k for k in data if k not in known)
    if not dropped:
        return
    log.warning(
        "config: ignoring unknown top-level key(s) %s "
        "(pre-v1 policy — no migration; check docs/10 §3)",
        dropped)
    doctrine = [k for k in dropped if k in _LEGACY_MERGE_DOCTRINE_KEYS]
    if doctrine:
        log.warning(
            "config: former top-level merge doctrine key(s) %s were DROPPED — "
            "merge policy is per-repo (ADR-0020). Each repos[] entry defaults "
            "to auto_merge=false; re-enable on the Repos page cards if this "
            "deployment previously auto-merged (docs/10 §3, docs/11 §2b)",
            doctrine)


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        if isinstance(data, dict):
            _refuse_stale_file(data)
            warn_unknown_top_level_keys(data)
        cfg = AppConfig.model_validate(data)
    else:
        # first boot is EMPTY (schema v4, F5): everything — PMO instances,
        # repos, credentials — is configured through the GUI; no env seeding
        cfg = AppConfig()
        log.info("config: first boot — empty config (%s); configure via the "
                 "admin panel", CONFIG_PATH)
    _atomic_yaml(CONFIG_PATH, cfg.model_dump())
    log.info("config: %d pmo instance(s), %d repo(s), adoption=%s",
             len(cfg.pmos), len(cfg.repos), cfg.adoption_mode)
    return cfg


def save_config(cfg: AppConfig) -> None:
    _atomic_yaml(CONFIG_PATH, cfg.model_dump())


def save_dev_type(dt: DevType) -> None:
    _atomic_yaml(CONFIG_PATH.parent / "dev_types" / f"{dt.name}.yaml", dt.model_dump())


def delete_dev_type(name: str) -> None:
    (CONFIG_PATH.parent / "dev_types" / f"{name}.yaml").unlink(missing_ok=True)


def load_dev_types() -> dict[str, DevType]:
    dt_dir = CONFIG_PATH.parent / "dev_types"
    dt_dir.mkdir(parents=True, exist_ok=True)
    # MAPPER→STEWARD (2026-08-06): migrate a persisted mapper.yaml — rename
    # the file AND its name field, preserving operator customizations
    # (harness, model, secret refs) — unless a steward.yaml already exists
    # (then the old file is retired untouched-in-content but removed, so the
    # seeder below cannot resurrect a parallel "mapper" type). One-time,
    # idempotent, ahead of the seeding pass.
    legacy = dt_dir / "mapper.yaml"
    if legacy.exists():
        target = dt_dir / "steward.yaml"
        if not target.exists():
            data = yaml.safe_load(legacy.read_text()) or {}
            data["name"] = "steward"
            _atomic_yaml(target, DevType.model_validate(data).model_dump())
            log.info("config: migrated dev type mapper → steward")
        legacy.unlink()
    # name-based top-up: a default Dev Type is (re-)seeded whenever its file is
    # missing, so existing deployments gain new defaults on boot. Customize
    # defaults by EDITING them — a deleted default returns next boot (docs/02 §6).
    for dt in DEFAULT_DEV_TYPES:
        p = dt_dir / f"{dt.name}.yaml"
        if not p.exists():
            _atomic_yaml(p, dt.model_dump())
            log.info("config: seeded default dev type %s", dt.name)
    out = {}
    for p in sorted(dt_dir.glob("*.yaml")):
        dt = DevType.model_validate(yaml.safe_load(p.read_text()) or {})
        out[dt.name] = dt
    return out
