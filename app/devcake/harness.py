"""The harness registry — single source of truth for what each harness_template
means at runtime: Docker image, credential requirements, OAuth device-code
flow, and the skills directory the harness CLI reads (docs/08 §2, §4, §7a).

The admin panel's harness combobox is authoritative BECAUSE dispatch derives
everything from this table: changing a Dev Type's harness changes its image and
credential requirements in one move. Secret *material* stays per Dev Type under
/data/secrets/{name}/ (two Dev Types on the same harness may use different
accounts); only the *requirements* live here.
"""

import os
import re
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
    # HOME-relative dir the harness CLI loads personal skills from; None =
    # no skills support (dispatch skips with a warning, the SPA disables the
    # selector). Snapshotted onto the Run at dispatch (run.spec_skills_dir)
    # so dir, skill content, and image all come from the same registry read.
    skills_dir: str | None = None
    # True = ships in-tree but has not passed a live operator battery.
    # The admin picker surfaces this so experimental is not prose-only.
    experimental: bool = False


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
        # CLI 2.1.210 reads ONLY ~/.claude/skills (cli.js-verified)
        skills_dir=".claude/skills",
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
        # CLI 0.2.103 reads ~/.agents/skills, ~/.grok/skills AND
        # ~/.claude/skills (claude-compat; `grok inspect`-verified) —
        # .agents is canonical; writing two dirs would double-list skills
        skills_dir=".agents/skills",
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
        # CLI 0.144.4 reads ~/.agents/skills (user) + repo .agents/skills +
        # /etc/codex/skills (developers.openai.com/codex/skills + binary
        # strings); repo-level is unused here — never write into the clone
        skills_dir=".agents/skills",
    ),
    "pi": Harness(
        image=f"devcake/dev-pi:{_TAG}",
        # Multi-provider CLI (docs/08): any one stored key is enough.
        credential_env=["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"],
        credential_files=[CredentialFile(secret_file="pi-auth.json",
                                         path_hint="~/.pi/agent/auth.json")],
        # Reads ~/.pi/agent/skills AND ~/.agents/skills; .agents is the
        # Agent Skills standard the other templates already use.
        skills_dir=".agents/skills",
        experimental=True,
    ),
    "opencode": Harness(
        image=f"devcake/dev-opencode:{_TAG}",
        # Multi-provider (models.dev): any one stored key is enough.
        credential_env=["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY"],
        credential_files=[CredentialFile(secret_file="opencode-auth.json",
                                         path_hint="~/.local/share/opencode/auth.json")],
        # Also reads ~/.claude/skills and ~/.config/opencode/skills;
        # .agents is the shared Agent Skills dir.
        skills_dir=".agents/skills",
        experimental=True,
    ),
    "qwen-code": Harness(
        image=f"devcake/dev-qwen-code:{_TAG}",
        # Proven headless path: OpenAI-compat or Anthropic. DashScope /
        # Coding Plan need OPENAI_BASE_URL or settings.json selectedType —
        # a lone vendor key is not enough (put those in secret_env).
        credential_env=["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        credential_files=[CredentialFile(secret_file="qwen-settings.json",
                                         path_hint="~/.qwen/settings.json")],
        # Personal skills: ~/.qwen/skills (project .qwen/skills is unused —
        # never write into the clone).
        skills_dir=".qwen/skills",
        experimental=True,
    ),
}


def resolve_image(dev_type) -> str:
    """Harness image for a run. Empty pin = house image at DEVCAKE_TAG.

    The three launch sites (dispatch, steward, OAuth) call this. Hello
    stays HELLO_IMAGE. Receipts do not change the string in this slice
    (fail-open). No cli_version field yet.
    """
    return HARNESSES[dev_type.harness_template].image


def missing_referenced_secret_env(dt) -> list[str]:
    """Declared secret_env names with NO stored value that ARE referenced
    ($VAR or ${VAR}) by an mcp_setup_command. Such a command would run with
    an empty expansion and hard-fail as exit 14 inside the container,
    burning an attempt with the root cause buried — the caller must refuse
    deterministically instead (founder decision 2026-07-16). Declared-but-
    unreferenced missing names stay warn-and-proceed (_credential_spec).
    v1 reference rule: a literal $NAME/${NAME} in the command text,
    word-bounded (mirrors shell longest-identifier expansion); indirection
    like `printenv NAME` is not detected."""
    from . import secrets as secrets_store
    cmds = "\n".join(dt.mcp_setup_commands)
    return [var for var in dt.secret_env
            if not secrets_store.read_harness_secret(var)
            and re.search(rf"\$(?:{var}\b|\{{{var}\}})", cmds)]


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
            "skills_dir": h.skills_dir,
            "experimental": h.experimental,
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
