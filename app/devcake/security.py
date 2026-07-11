"""Transcript/report redaction (docs/14 §5): everything DevCake posts to the
PMO System (an external SaaS) is scrubbed of known secret values and common
token patterns first. A secret that leaks into a Linear comment is unrecoverable
— this filter is the last line of defense, not the only one."""

import json
import os
import re
from pathlib import Path

# env vars whose VALUES are secrets (config-referenced + platform credentials)
SECRET_ENV_VARS = [
    "LINEAR_API_KEY", "GITHUB_TOKEN", "GITHUB_REVIEWER_TOKEN", "GITLAB_TOKEN",
    "GITLAB_REVIEWER_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "XAI_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "REDIS_PASSWORD",
    "DAGU_PASSWORD", "ADMIN_PASSWORD", "OO_ROOT_PASSWORD",
]

# common token shapes (docs/14 §5)
TOKEN_PATTERNS = [
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{15,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\blin_api_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\blin_oauth_[A-Za-z0-9]{20,}\b"),
]

MASK = "«REDACTED»"
_SECRETS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"


def _known_values() -> list[str]:
    values = [v for var in SECRET_ENV_VARS if (v := os.environ.get(var, "").strip())]
    # credential file key material: every long-ish string value in stored JSONs
    for p in _SECRETS_DIR.glob("*/*"):
        try:
            def walk(o):
                if isinstance(o, dict):
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
                elif isinstance(o, str) and len(o) >= 16:
                    values.append(o)
            walk(json.loads(p.read_text()))
        except Exception:
            continue
    return sorted(set(values), key=len, reverse=True)  # longest first


def redact(text: str, extra_values: list[str] | None = None) -> str:
    """Scrub known secret values + token patterns from PMO-bound content."""
    if not text:
        return text
    for value in _known_values() + [v for v in (extra_values or []) if v]:
        if len(value) >= 8:
            text = text.replace(value, MASK)
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub(MASK, text)
    return text
