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
    """Device-code login run inside the harness image (docs/08 §4)."""
    login_cmd: str      # headless login command
    auth_path: str      # where the CLI writes its auth state in-container
    secret_file: str    # stored name under /data/secrets/{dev_type}/


class Harness(BaseModel):
    image: str
    # model the harness runs when the Dev Type's model field is empty
    # ("" = let the CLI pick its own default)
    default_model: str = ""
    credential_env: list[str] = Field(default_factory=list)   # any-of passthrough
    credential_files: list[CredentialFile] = Field(default_factory=list)
    oauth: OAuthFlow | None = None


# Dispatch must use the same tag the operator baked (AGENTS.md pin workflow:
# export DEVCAKE_TAG=<sha> + bake + compose up). Hardcoding :latest silently
# ran stale harnesses under a pinned control plane — or, with no local
# :latest, pulled from the public devcake/* Docker Hub namespace (audit A7).
# Compose passes DEVCAKE_TAG through to the app container.
_TAG = os.environ.get("DEVCAKE_TAG", "latest")

HARNESSES: dict[str, Harness] = {
    "claude-code": Harness(
        image=f"devcake/dev-claude-code:{_TAG}",
        # paste-token mode (claude setup-token → CLAUDE_CODE_OAUTH_TOKEN) or
        # plain API key; no device-code flow (docs/08 §4)
        credential_env=["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
    ),
    "grok-build": Harness(
        image=f"devcake/dev-grok-build:{_TAG}",
        default_model="grok-4.5",
        credential_env=["XAI_API_KEY"],  # API-key mode; OAuth file preferred
        credential_files=[CredentialFile(secret_file="grok-auth.json",
                                         path_hint="~/.grok/auth.json")],
        oauth=OAuthFlow(login_cmd="grok login --device-auth",
                        auth_path="~/.grok/auth.json",
                        secret_file="grok-auth.json"),
    ),
    "codex": Harness(
        image=f"devcake/dev-codex:{_TAG}",
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
    under this Dev Type's /data/secrets dir. credentials_ready is the
    server-side readiness verdict (any ONE env key stored, or any credential
    file present, is enough) — the Overview Devs card reads it directly."""
    from . import secrets as secrets_store
    h = HARNESSES[dt.harness_template]
    secrets = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets" / dt.name
    present = (sorted(p.name for p in secrets.glob("*"))
               if secrets.is_dir() else [])
    ready = (any(secrets_store.read_harness_secret(var) for var in h.credential_env)
             or any(cf.secret_file in present for cf in h.credential_files))
    return {
        **dt.model_dump(),
        "harness": {
            "docker_image": h.image,
            "default_model": h.default_model,
            "credential_env": h.credential_env,
            "credential_files": [cf.model_dump() for cf in h.credential_files],
            "oauth_available": h.oauth is not None,
        },
        "secrets_present": present,
        # ✓/✗ per declared secret env var (DevType.secret_env) — presence
        # only, never the value; deliberately NOT folded into
        # credentials_ready (mission tooling, not harness credentials)
        "secret_env_present": {
            var: secrets_store.harness_status(var)["present"]
            for var in dt.secret_env},
        "credentials_ready": ready,
    }
