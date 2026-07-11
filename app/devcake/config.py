"""AppConfig: /data/config/config.yaml (docs/10 §3), seeded from env on first boot.

M2 carries the PMO + adoption + poll fields; the full model (forge, assignments,
concurrency) fills in at M3–M6.
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger("devcake.config")

CONFIG_PATH = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "config" / "config.yaml"


class PMOConfig(BaseModel):
    system: Literal["linear"] = "linear"
    api_key_env: str = "LINEAR_API_KEY"
    team_key: str = ""


class RepoConfig(BaseModel):
    forge: Literal["github", "gitlab"] = "github"
    url: str = ""
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


class CredentialFile(BaseModel):
    """A secret file under /data/secrets/{dev_type}/ delivered via runspec and
    installed by the Dev entrypoint at path_hint (docs/08 §4, docs/09 §3)."""
    secret_file: str
    path_hint: str


class DevType(BaseModel):
    """docs/02 §6 — one YAML per Dev Type under /data/config/dev_types/."""
    name: str
    harness_template: Literal["claude-code", "grok-build", "codex"]
    identifying_prompt: str = ""
    mcp_setup_commands: list[str] = Field(default_factory=list)
    credential_env: list[str] = Field(default_factory=list)  # env vars passed through
    credential_files: list[CredentialFile] = Field(default_factory=list)
    max_concurrency: int = 1
    docker_image: str = ""


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

DEFAULT_DEV_TYPES = [
    DevType(name="senior-dev", harness_template="claude-code",
            identifying_prompt=SENIOR_PROMPT,
            credential_env=["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
            max_concurrency=2, docker_image="devcake/dev-claude-code:latest"),
    DevType(name="main-dev", harness_template="grok-build",
            identifying_prompt=MAIN_PROMPT,
            credential_env=["XAI_API_KEY"],  # optional API-key mode; OAuth file preferred
            credential_files=[CredentialFile(secret_file="grok-auth.json",
                                             path_hint="~/.grok/auth.json")],
            max_concurrency=2, docker_image="devcake/dev-grok-build:latest"),
]


class AppConfig(BaseModel):
    schema_version: int = 1
    pmo: PMOConfig = Field(default_factory=PMOConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)
    assignments: dict[str, Assignment] = Field(
        default_factory=lambda: dict(DEFAULT_ASSIGNMENTS))
    concurrency: Concurrency = Field(default_factory=Concurrency)
    adoption_mode: Literal["opt_in", "opt_out"] = "opt_in"
    poll_interval_seconds: int = 30
    dev_timeout_minutes: int = 120
    max_attempts: int = 3
    review_loop_warning_every: int = 3
    auto_merge: bool = False

    @property
    def api_key(self) -> str:
        return os.environ.get(self.pmo.api_key_env, "")


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
        cfg = AppConfig.model_validate(yaml.safe_load(CONFIG_PATH.read_text()) or {})
    else:
        cfg = AppConfig(pmo=PMOConfig(team_key=os.environ.get("DEVCAKE_TEAM_KEY", "")))
        log.info("config: first boot — seeding %s from env", CONFIG_PATH)
    # top-up missing/env-provided fields (e.g. repo added at M3), then persist
    if not cfg.repo.url and os.environ.get("DEVCAKE_REPO_URL"):
        cfg.repo.url = os.environ["DEVCAKE_REPO_URL"]
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
    if not any(dt_dir.glob("*.yaml")):
        for dt in DEFAULT_DEV_TYPES:
            _atomic_yaml(dt_dir / f"{dt.name}.yaml", dt.model_dump())
        log.info("config: seeded default dev types (senior-dev, main-dev)")
    out = {}
    for p in sorted(dt_dir.glob("*.yaml")):
        dt = DevType.model_validate(yaml.safe_load(p.read_text()) or {})
        out[dt.name] = dt
    return out
