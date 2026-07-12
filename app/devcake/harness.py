"""The harness registry — single source of truth for what each harness_template
means at runtime: Docker image, credential requirements, and OAuth device-code
flow (docs/08 §2, §4).

The admin panel's harness combobox is authoritative BECAUSE dispatch derives
everything from this table: changing a Dev Type's harness changes its image and
credential requirements in one move. Secret *material* stays per Dev Type under
/data/secrets/{name}/ (two Dev Types on the same harness may use different
accounts); only the *requirements* live here.
"""

import os
from pathlib import Path

from pydantic import BaseModel, Field


class CredentialFile(BaseModel):
    """A secret file under /data/secrets/{dev_type}/ delivered via runspec and
    installed by the Dev entrypoint at path_hint (docs/08 §4, docs/09 §3)."""
    secret_file: str
    path_hint: str


class OAuthFlow(BaseModel):
    """Device-code login run inside the harness image (docs/16 M6)."""
    login_cmd: str      # headless login command
    auth_path: str      # where the CLI writes its auth state in-container
    secret_file: str    # stored name under /data/secrets/{dev_type}/


class Harness(BaseModel):
    image: str
    credential_env: list[str] = Field(default_factory=list)   # any-of passthrough
    credential_files: list[CredentialFile] = Field(default_factory=list)
    oauth: OAuthFlow | None = None


HARNESSES: dict[str, Harness] = {
    "claude-code": Harness(
        image="devcake/dev-claude-code:latest",
        # paste-token mode (claude setup-token → CLAUDE_CODE_OAUTH_TOKEN) or
        # plain API key; no device-code flow (docs/08 §4)
        credential_env=["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
    ),
    "grok-build": Harness(
        image="devcake/dev-grok-build:latest",
        credential_env=["XAI_API_KEY"],  # API-key mode; OAuth file preferred
        credential_files=[CredentialFile(secret_file="grok-auth.json",
                                         path_hint="~/.grok/auth.json")],
        oauth=OAuthFlow(login_cmd="grok login --device-auth",
                        auth_path="~/.grok/auth.json",
                        secret_file="grok-auth.json"),
    ),
    "codex": Harness(
        image="devcake/dev-codex:latest",
        # OPENAI_API_KEY needs `codex login --with-api-key` piping — not a
        # plain passthrough (docs/08 §4), so only CODEX_API_KEY is listed
        credential_env=["CODEX_API_KEY"],
        credential_files=[CredentialFile(secret_file="codex-auth.json",
                                         path_hint="~/.codex/auth.json")],
        oauth=OAuthFlow(login_cmd="codex login --device-auth",
                        auth_path="~/.codex/auth.json",
                        secret_file="codex-auth.json"),
    ),
}


def dev_type_status(dt) -> dict:
    """Enriched admin payload for one DevType (GET /api/v1/dev-types): the slim
    stored fields + derived harness info + which secret files actually exist
    under this Dev Type's /data/secrets dir. Readiness itself is computed by
    the SPA (env-var status comes from GET /env-check)."""
    h = HARNESSES[dt.harness_template]
    secrets = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets" / dt.name
    return {
        **dt.model_dump(),
        "harness": {
            "docker_image": h.image,
            "credential_env": h.credential_env,
            "credential_files": [cf.model_dump() for cf in h.credential_files],
            "oauth_available": h.oauth is not None,
        },
        "secrets_present": sorted(p.name for p in secrets.glob("*"))
                           if secrets.is_dir() else [],
    }
