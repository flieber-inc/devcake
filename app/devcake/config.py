"""AppConfig: /data/config/config.yaml (docs/10 §3), seeded from env on first
boot. The full operator surface: PMO/repo connections (plural, schema v2),
adoption, polling, assignments, concurrency, merge policy, relations mapper.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger("devcake.config")

CONFIG_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "config" / "config.yaml"


class PMOInstance(BaseModel):
    """One configured PMO connection. v0 runs exactly one (see AppConfig
    validator); the list shape is the forward-compatible schema so multi-PMO
    needs no config migration."""
    id: str = "main"
    system: str = "linear"          # validated against the adapter registry
    api_key_env: str = "LINEAR_API_KEY"
    team_key: str = ""
    api_base: str | None = None     # None = the adapter's default API host

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
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


def _default_forge() -> str:
    # lazy: config stays import-light; the default forge id is registry
    # knowledge, not config knowledge (F1)
    from .adapters.registry import DEFAULT_FORGE
    return DEFAULT_FORGE


class RepoInstance(BaseModel):
    """One configured forge repository. v0 runs exactly one (see AppConfig
    validator)."""
    id: str = "main"
    forge: str = Field(default_factory=_default_forge)  # registry-validated
    url: str = ""
    api_base: str | None = None     # None = the adapter's default API host / the repo's origin
    default_branch: str = "main"
    # empty = derived from the forge's descriptor (token_env_default)
    token_env: str = ""
    # Optional read-only PAT for non-EXECUTE stages (ISSUES #15). When set,
    # PLAN/REVIEW/MAPPER/ONBOARD clone with it instead of the write token.
    # When empty (the DEFAULT), every stage receives the WRITE token — the
    # entrypoint always clones, so omitting it entirely breaks private repos.
    # Accepted for v0; /health surfaces a dismissable warning (docs/14 §2).
    token_ro_env: str | None = None
    reviewer_token_env: str | None = None

    @field_validator("forge")
    @classmethod
    def _known_forge(cls, v):
        from .adapters.registry import forges  # lazy: config stays import-light
        if v not in forges():
            raise ValueError(f"unknown forge {v!r} — registered: "
                             f"{sorted(forges())}")
        return v

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        """Reject malformed forge URLs that would crash adapter constructors
        after a config PUT has already persisted (ISSUES #10). Empty is allowed
        for first-boot until the operator configures a repo."""
        if not v:
            return v
        from urllib.parse import urlsplit
        parts = urlsplit(v if "://" in v else f"https://{v}")
        path = (parts.path or "").strip("/").removesuffix(".git")
        segs = [s for s in path.split("/") if s]
        # forge-neutral rule: every supported forge addresses a repo as at
        # least <owner-or-group>/<repo> (nested groups add more segments)
        if not parts.netloc or len(segs) < 2:
            raise ValueError(
                f"invalid repository URL {v!r}: need a host and an "
                f"owner/repo project path (e.g. https://<host>/owner/repo)")
        return v

    @model_validator(mode="after")
    def _derive_token_env(self):
        """An empty token_env derives from the forge's descriptor — the
        default env-var name is adapter knowledge, not config knowledge (F1).
        Runs after field validation, so self.forge is registry-checked."""
        if not self.token_env:
            from .adapters.registry import forges  # lazy: import-light
            self.token_env = forges()[self.forge].token_env_default
        return self

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")

    @property
    def token_ro(self) -> str:
        # explicit token_ro_env wins; otherwise the conventional name
        # ({token_env}_RO) works out of the box — the /health warning and
        # .env.example both point operators at it
        if self.token_ro_env:
            return os.environ.get(self.token_ro_env, "")
        return os.environ.get(f"{self.token_env}_RO", "")


class Assignment(BaseModel):
    dev_type: str = ""
    extra_cli_args: str = ""


class Concurrency(BaseModel):
    # concurrency caps are the real host-protection throttle: Dagu 2.10.5
    # cannot apply Docker HostConfig limits to Dev containers (docs/07 §7);
    # per-container hard limits return with Dagu host-config support (v0.1)
    global_max: int = Field(3, ge=1)


class RelationsMapper(BaseModel):
    """Dev run that maps missing blocked-by relations (ADR-0007). Manual-only
    by default (enabled=False → the admin "Run now" button); the periodic
    service is opt-in. dev_type must name an existing Dev Type whenever
    enabled — the seeded junior-dev (cheap model) is the default vehicle."""
    enabled: bool = False
    interval_minutes: int = Field(60, ge=1)
    dev_type: str | None = "junior-dev"


class DevType(BaseModel):
    """docs/02 §6 — one YAML per Dev Type under /data/config/dev_types/.

    Deliberately slim: the Docker image, credential requirements, and OAuth
    flow all DERIVE from harness_template via harness.HARNESSES — the admin
    panel's harness combobox is authoritative. Unknown YAML keys are ignored
    on load and dropped on the next save (pydantic's default)."""
    name: str
    harness_template: Literal["claude-code", "grok-build", "codex"]
    identifying_prompt: str = ""
    mcp_setup_commands: list[str] = Field(default_factory=list)
    max_concurrency: int = Field(1, ge=1)
    model: str = ""  # harness model override (e.g. claude-fable-5); "" = harness default


DEFAULT_ASSIGNMENTS = {
    "ONBOARD": Assignment(dev_type="senior-dev", extra_cli_args="--max-turns 15"),
    "PLAN": Assignment(dev_type="senior-dev"),
    "EXECUTE": Assignment(dev_type="main-dev"),
    "REVIEW": Assignment(dev_type="senior-dev"),
}

# docs/03 §7 — canonical identifying prompts (seed data; admin-editable)
SENIOR_PROMPT = (
    "You are **Senior Dev**, DevCake's judgment-heavy engineer. You assess, plan, and "
    "review software work with the skepticism of a staff engineer who has been burned "
    "before. You are precise about scope: you do exactly what your current mission type "
    "asks — no more. You never invent requirements, you flag what you cannot verify, and "
    "you write conclusions that a teammate can act on without asking follow-up questions."
)
MAIN_PROMPT = (
    "You are **Main Dev**, DevCake's implementation engineer. You turn plans into working, "
    "tested code. You follow the plan you are given; where reality contradicts the plan, "
    "you implement the smallest sound deviation and document it prominently in your "
    "summary. You match the conventions of the codebase you are in, you run the tests, "
    "and you never commit until the work is complete."
)
JUNIOR_PROMPT = (
    "You are **Junior Dev**, DevCake's fast, literal assistant. You do exactly the narrow "
    "task you are given — no more. You never improvise scope, you follow output formats "
    "to the letter, and when you are unsure you say so instead of guessing."
)

DEFAULT_DEV_TYPES = [
    DevType(name="senior-dev", harness_template="claude-code",
            identifying_prompt=SENIOR_PROMPT, max_concurrency=2,
            model="claude-fable-5"),  # founder decision 2026-07-12: Senior Dev judgment runs on Fable
    DevType(name="main-dev", harness_template="grok-build",
            identifying_prompt=MAIN_PROMPT, max_concurrency=2),
    # cheap, literal worker for narrow structured tasks — the Relations Mapper's
    # default vehicle (ADR-0007 addendum); same harness/credentials as senior-dev
    DevType(name="junior-dev", harness_template="claude-code",
            identifying_prompt=JUNIOR_PROMPT, max_concurrency=1,
            model="claude-haiku-4-5"),
]


class AppConfig(BaseModel):
    schema_version: int = 2
    pmos: list[PMOInstance] = Field(default_factory=lambda: [PMOInstance()])
    repos: list[RepoInstance] = Field(default_factory=lambda: [RepoInstance()])
    assignments: dict[str, Assignment] = Field(
        default_factory=lambda: dict(DEFAULT_ASSIGNMENTS))
    concurrency: Concurrency = Field(default_factory=Concurrency)
    adoption_mode: Literal["opt_in", "opt_out"] = "opt_in"
    poll_interval_seconds: int = Field(30, ge=1, le=3600)
    dev_timeout_minutes: int = Field(120, ge=1, le=24 * 60)
    max_attempts: int = Field(3, ge=1, le=50)
    # ge=1: used as a modulo cadence; 0 would ZeroDivisionError (ISSUES #8/#9)
    review_loop_warning_every: int = Field(3, ge=1)
    auto_merge: bool = False
    # both inert while auto_merge is OFF (docs/03 §4.1): on a merge conflict,
    # route back to EXECUTE to sync + resolve (max 2 attempts) instead of
    # parking on DEVCAKE-MERGE; while a merge is merely not-possible-yet
    # (CI running, mergeability computing) the merge sweep keeps retrying for
    # merge_retry_window_minutes before the human hand-off (0 = immediately)
    auto_resolve_merge_conflicts: bool = True
    merge_retry_window_minutes: int = Field(30, ge=0)
    # operator switch: no NEW runs dispatch while paused; in-flight runs finish
    # and sweeps keep running (docs/11)
    intake_paused: bool = False
    relations_mapper: RelationsMapper = Field(default_factory=RelationsMapper)
    # admin-UI state: dismissed advisory alerts as "id:signature" strings.
    # A list (not a dict) on purpose — deep_merge can't delete dict keys, so
    # the UI un-dismisses by PUTting the whole replacement list.
    dismissed_alerts: list[str] = Field(default_factory=list)

    @field_validator("pmos", "repos")
    @classmethod
    def _exactly_one(cls, v, info):
        if len(v) != 1:
            raise ValueError(f"{info.field_name}: exactly one entry is supported "
                             "in v0 (multi-instance is a declared future seam)")
        ids = [e.id for e in v]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{info.field_name}: duplicate instance ids")
        return v

    # v0 single-instance accessors: the runtime is written against ONE pmo and
    # ONE repo; these keep call sites (config.pmo.team_key, config.repo.url)
    # stable while the persisted schema is already plural.
    @property
    def pmo(self) -> PMOInstance:
        return self.pmos[0]

    @property
    def repo(self) -> RepoInstance:
        return self.repos[0]

    @property
    def api_key(self) -> str:
        return self.pmos[0].api_key


def reject_v1_patch(body: dict) -> None:
    """Refuse v1-shaped PUT bodies ({"pmo": {…}} / {"repo": {…}}) loudly.
    Load-bearing, not defensive: pydantic ignores unknown keys, so without
    this a stale client's PUT would silently DROP the operator's edit
    instead of failing (the v1→v2 auto-migration was removed at v0)."""
    stale = [k for k in ("pmo", "repo") if isinstance(body.get(k), dict)]
    if stale:
        raise ValueError(
            f"singular {'/'.join(stale)!s} config keys are schema v1; "
            "send the plural v2 shape (pmos:/repos: lists)")


def deep_merge(base: dict, patch: dict) -> dict:
    """Recursive dict merge for partial config PUTs (docs/11 §1): a nested
    patch like {"repo": {"url": …}} must not silently reset sibling fields
    (forge, token_env, …) to their defaults."""
    merged = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _atomic_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        # detect v1 by its singular keys, never by an absent schema_version —
        # an empty or hand-written v2 file without the version field is fine.
        # Refuse loudly: silently validating v1 data would reset the
        # operator's connections to defaults (pydantic ignores unknown keys).
        if isinstance(data, dict) and ("pmo" in data or "repo" in data):
            raise RuntimeError(
                f"{CONFIG_PATH} uses the singular v1 shape (pmo:/repo: "
                "blocks); the v1→v2 auto-migration was removed at v0 — "
                "migrate by hand (pmo:→pmos: list, repo:→repos: list, "
                "schema_version: 2) or delete the file and reconfigure via "
                "the admin panel")
        cfg = AppConfig.model_validate(data)
    else:
        cfg = AppConfig(pmos=[PMOInstance(
            team_key=os.environ.get("DEVCAKE_TEAM_KEY", ""))])
        log.info("config: first boot — seeding %s from env", CONFIG_PATH)
    # top-up missing/env-provided fields (a config predating a field picks up
    # its env default here), then persist
    if not cfg.repo.url and os.environ.get("DEVCAKE_REPO_URL"):
        cfg.repos[0].url = os.environ["DEVCAKE_REPO_URL"]
    _atomic_yaml(CONFIG_PATH, cfg.model_dump())
    log.info("config: team=%s adoption=%s repo=%s",
             cfg.pmo.team_key, cfg.adoption_mode, cfg.repo.url or "(unset)")
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
