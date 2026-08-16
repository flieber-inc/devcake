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
