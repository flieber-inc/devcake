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


class AppConfig(BaseModel):
    schema_version: int = 1
    pmo: PMOConfig = Field(default_factory=PMOConfig)
    adoption_mode: Literal["opt_in", "opt_out"] = "opt_in"
    poll_interval_seconds: int = 30
    dev_timeout_minutes: int = 120
    max_attempts: int = 3
    review_loop_warning_every: int = 3
    auto_merge: bool = False

    @property
    def api_key(self) -> str:
        return os.environ.get(self.pmo.api_key_env, "")


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        cfg = AppConfig.model_validate(yaml.safe_load(CONFIG_PATH.read_text()) or {})
        log.info("config: loaded %s (team=%s, adoption=%s)",
                 CONFIG_PATH, cfg.pmo.team_key, cfg.adoption_mode)
        return cfg
    cfg = AppConfig(pmo=PMOConfig(team_key=os.environ.get("DEVCAKE_TEAM_KEY", "")))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(cfg.model_dump(), f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_PATH)
    log.info("config: first boot — seeded %s from env (team=%s)", CONFIG_PATH, cfg.pmo.team_key)
    return cfg
