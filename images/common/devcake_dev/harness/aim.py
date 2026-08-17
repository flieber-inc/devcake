"""Backend adaptor — docs/08 §8.

The adaptor’s job is: given this CLI and this base URL, produce the env,
argv, and files that make the process talk to that URL.

Not the dialect (argv/fault/classify). Not the stub (wire tapes only).
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class AimedFile:
    path_hint: str
    content: str


@dataclass(frozen=True)
class Aimed:
    env: dict[str, str]
    extra_argv: tuple[str, ...] = ()
    files: tuple[AimedFile, ...] = ()


_KNOWN = frozenset({
    "claude-code", "codex", "grok-build",
    "pi", "opencode", "qwen-code",
})


def aim(template: str, base_url: str, *, api_key: str,
        cli_version: str = "", model: str = "") -> Aimed:
    """Map this CLI + URL onto env, extra argv, and HOME-relative files."""
    if template not in _KNOWN:
        raise ValueError(
            f"unknown harness {template!r} — refusing aim "
            f"(known: {sorted(_KNOWN)})")
    url = (base_url or "").strip()
    if not url:
        return Aimed(env={})
    if not (api_key or "").strip():
        raise ValueError(
            "backend URL is set but no API key is stored for this harness")
    if template == "claude-code":
        return Aimed(env={
            "ANTHROPIC_BASE_URL": url,
            "ANTHROPIC_AUTH_TOKEN": api_key,
        })
    if template == "grok-build":
        return _aim_grok(url, api_key=api_key, cli_version=cli_version,
                         model=model)
    if template == "pi":
        return _aim_pi(url, api_key=api_key, model=model)
    if template == "opencode":
        return _aim_opencode(url, api_key=api_key, model=model)
    if template == "qwen-code":
        return _aim_qwen(url, api_key=api_key, model=model)
    if template == "codex":
        return Aimed(
            env={"CODEX_API_KEY": api_key},
            extra_argv=(
                "-c", "model_provider=stub",
                "-c", "model_providers.stub.name=Stub",
                "-c", "model_providers.stub.env_key=CODEX_API_KEY",
                "-c", "model_providers.stub.wire_api=responses",
                "-c", f"model_providers.stub.base_url={url}",
                "-c", "model_providers.stub.request_max_retries=0",
                "-c", "model_providers.stub.stream_max_retries=0",
            ),
        )
    raise ValueError(
        f"no aim recipe for {template!r} yet — measure the binary first")


def _aim_grok(url: str, *, api_key: str, cli_version: str,
              model: str) -> Aimed:
    # House 0.2.112 honors GROK_MODELS_BASE_URL. 1.0.4 (and later 1.x)
    # ignores it for a non-catalog --model unless config.toml declares
    # [model.<id>] with base_url + env_key (measured 2026-08-16).
    if not _grok_needs_model_block(cli_version):
        return Aimed(env={
            "GROK_MODELS_BASE_URL": url,
            "XAI_API_KEY": api_key,
        })
    pin = (model or "").strip()
    if not pin:
        raise ValueError(
            "backend URL is set for grok-build 1.x — set the Model field "
            "to the backend model id")
    body = (
        f"[model.{_toml_key(pin)}]\n"
        f"model = {_toml_str(pin)}\n"
        f"base_url = {_toml_str(url)}\n"
        'env_key = "XAI_API_KEY"\n'
    )
    return Aimed(
        env={"XAI_API_KEY": api_key},
        files=(AimedFile(path_hint="~/.grok/config.toml", content=body),),
    )


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_key(value: str) -> str:
    """Bare key if safe; otherwise quoted (TOML)."""
    import re
    if re.fullmatch(r"[A-Za-z0-9_-]+", value or ""):
        return value
    return _toml_str(value)


def merge_grok_config_toml(existing: str, model_block: str) -> str:
    """Keep unrelated tables; replace or append the aimed [model.<id>] block."""
    block = (model_block or "").strip() + "\n"
    if not (existing or "").strip():
        return block
    header = block.split("\n", 1)[0].strip()  # [model.x]
    if not header.startswith("["):
        return existing.rstrip() + "\n\n" + block
    lines = existing.splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skip = stripped == header
            if skip:
                continue
        if skip:
            continue
        out.append(line)
    body = "".join(out).rstrip()
    if body:
        return body + "\n\n" + block
    return block


def _grok_needs_model_block(cli_version: str) -> bool:
    ver = (cli_version or "").strip()
    if not ver or ver == "0.2.112":
        return False
    return not ver.startswith("0.")


def _aim_pi(url: str, *, api_key: str, model: str) -> Aimed:
    pin = (model or "").strip()
    if not pin:
        raise ValueError("pi aim() needs the model id for models.json")
    body = json.dumps({
        "providers": {
            "devcake": {
                "baseUrl": url,
                "api": "openai-completions",
                "apiKey": "$OPENAI_API_KEY",
                "models": [{"id": pin}],
            }
        }
    }, indent=2) + "\n"
    return Aimed(
        env={"OPENAI_API_KEY": api_key},
        extra_argv=("--provider", "devcake"),
        files=(AimedFile(path_hint="~/.pi/agent/models.json", content=body),),
    )


def _aim_opencode(url: str, *, api_key: str, model: str) -> Aimed:
    # House 1.18.18 ignores OPENAI_BASE_URL. A custom XDG provider with
    # npm:@ai-sdk/openai-compatible + options.baseURL is what the CLI
    # honors (measured 2026-08-16). Pin --model as devcake/<id>.
    pin = (model or "").strip().rsplit("/", 1)[-1]
    if not pin:
        raise ValueError("opencode aim() needs the model id for opencode.json")
    body = json.dumps({
        "provider": {
            "devcake": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "DevCake",
                "options": {"baseURL": url, "apiKey": "$OPENAI_API_KEY"},
                "models": {pin: {"name": pin}},
            }
        }
    }, indent=2) + "\n"
    return Aimed(
        env={"OPENAI_API_KEY": api_key},
        files=(AimedFile(
            path_hint="~/.config/opencode/opencode.json", content=body),),
    )


def _aim_qwen(url: str, *, api_key: str, model: str) -> Aimed:
    # House 0.21.12 leftover OPENAI_BASE_URL is FAULT. selectedType openai
    # + modelProviders.openai[].baseUrl is what the CLI honors
    # (measured 2026-08-16). envKey names OPENAI_API_KEY; omitting
    # selectedType is FAULT even with a complete providers block.
    pin = (model or "").strip().rsplit("/", 1)[-1]
    if not pin:
        raise ValueError("qwen-code aim() needs the model id for settings.json")
    body = json.dumps({
        "security": {"auth": {"selectedType": "openai"}},
        "modelProviders": {
            "openai": [{
                "id": pin,
                "envKey": "OPENAI_API_KEY",
                "baseUrl": url,
            }]
        },
    }, indent=2) + "\n"
    return Aimed(
        env={"OPENAI_API_KEY": api_key},
        files=(AimedFile(path_hint="~/.qwen/settings.json", content=body),),
    )


_KEY_ENV = {
    "claude-code": ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"),
    "grok-build": ("XAI_API_KEY",),
    "codex": ("CODEX_API_KEY",),
    "pi": ("OPENAI_API_KEY",),
    "opencode": ("OPENAI_API_KEY",),
    "qwen-code": ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
}


def api_key_from_env(template: str, env: dict[str, str]) -> str:
    """First stored key this CLI's aim() recipe will honor."""
    for name in _KEY_ENV.get(template, ()):
        val = (env.get(name) or "").strip()
        if val:
            return val
    return ""


def aimed_model_id(template: str, model: str) -> str:
    """OpenCode --model is provider/id; aim registers provider devcake."""
    pin = (model or "").strip()
    if template == "opencode" and pin and "/" not in pin:
        return f"devcake/{pin}"
    return pin


def stub_lane(template: str, stub_origin: str, scenario: str) -> str:
    """Stub URL in the shape this CLI’s aim() recipe expects."""
    lane = f"{stub_origin.rstrip('/')}/s/{scenario}"
    if template == "claude-code":
        return lane
    return f"{lane}/v1"
