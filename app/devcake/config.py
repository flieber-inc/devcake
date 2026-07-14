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


# Operator-chosen instance identity (schema v3, docs/16 M9). Lowercase
# alnum, ≤12 chars, NO hyphens: the name is embedded uppercased in branch
# names and run ids ({INSTANCE}-{key}), where a hyphen would make the
# compound ambiguous; ≤12 protects the 64-char Dagu run-id budget.
_INSTANCE_NAME_RE = r"^[a-z][a-z0-9]{0,11}$"


class PMOInstance(BaseModel):
    """One configured PMO connection (schema v3: instances-with-identities).
    An instance with an empty team_key is VALID BUT IDLE — the poll loop and
    label bootstrap skip it and /health shows it as unconfigured — so an
    empty first boot (and M12's GUI-only setup) is a defined state."""
    name: str = Field("linear", pattern=_INSTANCE_NAME_RE)
    system: str = "linear"          # validated against the adapter registry
    api_key_env: str = "LINEAR_API_KEY"
    team_key: str = ""
    api_base: str | None = None     # None = the adapter's default API host
    # M10 routing: the repo (by name) missions of this instance resolve to
    # when they carry no `devcake-repo:` marker; None = zero-repo gate
    default_repo: str | None = None

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
        return os.environ.get(self.api_key_env, "")


def _default_forge() -> str:
    # lazy: config stays import-light; the default forge id is registry
    # knowledge, not config knowledge (F1)
    from .adapters.registry import DEFAULT_FORGE
    return DEFAULT_FORGE


class RepoInstance(BaseModel):
    """One configured forge repository (schema v3: instances-with-identities).
    An instance with an empty url is valid but unconfigured (first boot)."""
    name: str = Field("main", pattern=_INSTANCE_NAME_RE)
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

    @property
    def configured(self) -> bool:
        return bool(self.url.strip())

    @property
    def resolved_token_env(self) -> str:
        """token_env, or the forge descriptor's default when unset. Resolved
        at READ time, never materialized into the model: a persisted "" must
        re-derive after a forge switch (the admin GitHub↔GitLab hot-switch),
        so save_config never bakes one forge's env name into another's config."""
        if self.token_env:
            return self.token_env
        from .adapters.registry import forges  # lazy: config stays import-light
        return forges()[self.forge].token_env_default

    @property
    def token(self) -> str:
        return os.environ.get(self.resolved_token_env, "")

    @property
    def token_ro(self) -> str:
        # explicit token_ro_env wins; otherwise the conventional name
        # ({token_env}_RO) works out of the box — the /health warning and
        # .env.example both point operators at it
        if self.token_ro_env:
            return os.environ.get(self.token_ro_env, "")
        return os.environ.get(f"{self.resolved_token_env}_RO", "")


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
    schema_version: int = 3
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

    @field_validator("schema_version")
    @classmethod
    def _current_version(cls, v):
        # ONE generic refusal for every stale version (v1, v2, future) —
        # no bespoke per-version detectors (docs/10 §3 has the migration
        # recipe; there are no deployments to auto-migrate for)
        if v != 3:
            raise ValueError(
                f"config schema_version {v} is not the current version (3) — "
                "hand-migrate per docs/10 §3 or delete the file and "
                "reconfigure via the admin panel")
        return v

    @field_validator("pmos")
    @classmethod
    def _pmos_valid(cls, v):
        if not v:
            raise ValueError("pmos: at least one PMO instance is required "
                             "(it may be unconfigured — empty team_key)")
        names = [e.name for e in v]
        if len(set(names)) != len(names):
            raise ValueError("pmos: duplicate instance names")
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
        if len(v) != 1:
            raise ValueError("repos: exactly one entry is supported until "
                             "M10 (repo multiplicity — docs/16)")
        names = [e.name for e in v]
        if len(set(names)) != len(names):
            raise ValueError("repos: duplicate instance names")
        return v

    @model_validator(mode="after")
    def _default_repo_exists(self):
        repo_names = {r.name for r in self.repos}
        for p in self.pmos:
            if p.default_repo is not None and p.default_repo not in repo_names:
                raise ValueError(
                    f"pmos[{p.name}].default_repo {p.default_repo!r} names no "
                    f"configured repo (have: {sorted(repo_names)})")
        return self


def _stale_shape_reason(data: dict) -> str | None:
    """ONE detector for every stale config shape — file loads and PUT bodies
    share it (a divergent copy was a review finding). Detects v1 (singular
    pmo:/repo: dicts), v2 (id-keyed instance entries), and explicit old
    schema_versions. Returns the human reason, or None when current."""
    stale = [k for k in ("pmo", "repo") if isinstance(data.get(k), dict)]
    if stale:
        return (f"singular {'/'.join(stale)!s} config keys are schema v1; "
                "the current v3 shape is pmos:/repos: name-keyed lists")
    for key in ("pmos", "repos"):
        entries = data.get(key)
        if isinstance(entries, list) and any(
                isinstance(e, dict) and "id" in e for e in entries):
            return (f"{key} entries carry the v2 'id' field; schema v3 uses "
                    "operator-chosen 'name' identities")
    if data.get("schema_version") not in (None, 3):
        return (f"schema_version {data['schema_version']} is stale — the "
                "current version is 3")
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


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        if isinstance(data, dict):
            _refuse_stale_file(data)
        cfg = AppConfig.model_validate(data)
    else:
        cfg = AppConfig(pmos=[PMOInstance(
            team_key=os.environ.get("DEVCAKE_TEAM_KEY", ""))])
        log.info("config: first boot — seeding %s from env", CONFIG_PATH)
    # top-up missing/env-provided fields (a config predating a field picks up
    # its env default here), then persist
    if not cfg.repos[0].url and os.environ.get("DEVCAKE_REPO_URL"):
        cfg.repos[0].url = os.environ["DEVCAKE_REPO_URL"]
    _atomic_yaml(CONFIG_PATH, cfg.model_dump())
    log.info("config: team=%s adoption=%s repo=%s",
             cfg.pmos[0].team_key, cfg.adoption_mode,
             cfg.repos[0].url or "(unset)")
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
