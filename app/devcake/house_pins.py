"""House CLI pin versions — the app-side half of Q1.

Dockerfile ARG defaults stay the bake source. A ratchet in
test_harness_cli_pins keeps these literals equal to those ARGs.
Package identities (npm / x.ai) still live only in the Dockerfile.
"""

from __future__ import annotations

import os

SENTINEL_DIGEST = "DEVCAKE_APP_DIGEST_UNSET"

# Launch-supported first; experimental stay house-pin only (probe v1
# does not grade them, so fail-closed does not gate them).
HOUSE_PINS: dict[str, str] = {
    "claude-code": "2.1.229",
    "codex": "0.147.0",
    "grok-build": "0.2.112",
    "pi": "0.84.2",
    "opencode": "1.18.18",
    "qwen-code": "0.21.12",
}

LAUNCH_SUPPORTED = frozenset({"claude-code", "codex", "grok-build"})

PACKAGE_IDS: dict[str, str] = {
    "claude-code": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
    "pi": "@earendil-works/pi-coding-agent",
    "opencode": "opencode-ai",
    "qwen-code": "@qwen-code/qwen-code",
    "grok-build": "x.ai/cli",
}

DOCKERFILE_ARG: dict[str, str] = {
    "claude-code": "CLAUDE_CODE_VERSION",
    "codex": "CODEX_VERSION",
    "grok-build": "GROK_VERSION",
    "pi": "PI_VERSION",
    "opencode": "OPENCODE_VERSION",
    "qwen-code": "QWEN_CODE_VERSION",
}


def app_digest() -> str:
    return os.environ.get("DEVCAKE_APP_DIGEST", SENTINEL_DIGEST)


def effective_cli_version(dev_type) -> str:
    """Stored pin, or the house Dockerfile ARG for that template."""
    pin = (getattr(dev_type, "cli_version", "") or "").strip()
    if pin:
        return pin
    return HOUSE_PINS.get(dev_type.harness_template, "")


def image_ref(template: str, cli_version: str, *, tag: str | None = None) -> str:
    """House pin (empty or the ARG default) → :TAG. Anything else → :TAG-ver.

    Keep-set records effective versions, so the baker cannot tell a typed
    house pin from an empty field. Both must name the same image.
    """
    tag = tag if tag is not None else os.environ.get("DEVCAKE_TAG", "latest")
    pin = (cli_version or "").strip()
    if not pin or pin == HOUSE_PINS.get(template, ""):
        return f"devcake/dev-{template}:{tag}"
    return f"devcake/dev-{template}:{tag}-{pin}"
