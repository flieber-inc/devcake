"""AppConfig: /data/config/config.yaml (docs/10 §3), seeded from env on first boot.

M2 carries the PMO + adoption + poll fields; the full model (forge, assignments,
concurrency) fills in at M3–M6.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

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


class RepoInstance(BaseModel):
    """One configured forge repository. v0 runs exactly one (see AppConfig
    validator)."""
    id: str = "main"
    forge: str = "github"           # validated against the adapter registry
    url: str = ""
    api_base: str | None = None     # None = api.github.com / the repo's origin
    default_branch: str = "main"
    token_env: str = "GITHUB_TOKEN"
    reviewer_token_env: str | None = None

    @property
    def token(self) -> str:
        return os.environ.get(self.token_env, "")


class Assignment(BaseModel):
    dev_type: str = ""
    extra_cli_args: str = ""


class Concurrency(BaseModel):
    global_max: int = 3


class RelationsMapper(BaseModel):
    """Dev run that maps missing blocked-by relations (ADR-0007). Manual-only
    by default (enabled=False → the admin "Run now" button); the periodic
    service is opt-in. dev_type must name an existing Dev Type whenever
    enabled — the seeded junior-dev (cheap model) is the default vehicle."""
    enabled: bool = False
    interval_minutes: int = 60
    dev_type: str | None = "junior-dev"


class DevType(BaseModel):
    """docs/02 §6 — one YAML per Dev Type under /data/config/dev_types/.

    Deliberately slim: the Docker image, credential requirements, and OAuth
    flow all DERIVE from harness_template via harness.HARNESSES — the admin
    panel's harness combobox is authoritative. Legacy YAML keys (docker_image,
    credential_env, credential_files) are ignored on load and dropped on the
    next save (pydantic extra="ignore")."""
    name: str
    harness_template: Literal["claude-code", "grok-build", "codex"]
    identifying_prompt: str = ""
    mcp_setup_commands: list[str] = Field(default_factory=list)
    max_concurrency: int = 1
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
    poll_interval_seconds: int = 30
    dev_timeout_minutes: int = 120
    max_attempts: int = 3
    review_loop_warning_every: int = 3
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


def migrate_config(data: dict) -> dict:
    """v1 (singular pmo:/repo: blocks) → v2 (plural pmos:/repos:, id="main").
    Idempotent; applied before validation on every load (ADR-0002: migrate on
    load, persist atomically). Forward-only — load_config keeps a .v1.bak."""
    if int(data.get("schema_version") or 1) >= 2:
        return data
    out = dict(data)
    out["pmos"] = [{"id": "main", **(out.pop("pmo", None) or {})}]
    out["repos"] = [{"id": "main", **(out.pop("repo", None) or {})}]
    out["schema_version"] = 2
    return out


def migrate_config_patch(body: dict, current: "AppConfig") -> dict:
    """Translate v1-shaped PUT bodies ({"pmo": {…}} / {"repo": {…}}) to the
    plural v2 shape, merging over the current single entry. Load-bearing, not
    defensive: pydantic ignores unknown keys, so without this a stale client's
    PUT would silently DROP the operator's edit instead of failing."""
    out = dict(body)
    if isinstance(out.get("pmo"), dict):
        out["pmos"] = [{**current.pmos[0].model_dump(), **out.pop("pmo")}]
    if isinstance(out.get("repo"), dict):
        out["repos"] = [{**current.repos[0].model_dump(), **out.pop("repo")}]
    return out


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
        if int(data.get("schema_version") or 1) < 2:
            # forward-only migration: the .bak is the rollback story
            shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(".yaml.v1.bak"))
            log.info("config: migrating %s to schema v2 (backup: config.yaml.v1.bak)",
                     CONFIG_PATH)
        cfg = AppConfig.model_validate(migrate_config(data))
    else:
        cfg = AppConfig(pmos=[PMOInstance(
            team_key=os.environ.get("DEVCAKE_TEAM_KEY", ""))])
        log.info("config: first boot — seeding %s from env", CONFIG_PATH)
    # top-up missing/env-provided fields (e.g. repo added at M3), then persist
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
