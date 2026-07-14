"""Transcript/report redaction (docs/14 §5).

Boundary (honest): app→PMO and app→forge *writes* are scrubbed at choke points
(`MissionManager._feed`, forge PR comments, `create_mission` titles/bodies).
This is not a guarantee that every string leaving the process is redacted —
Dev containers have unrestricted egress and can exfiltrate env/secrets over
their own sockets (docs/14 §2 accepted risk). A secret that leaks into a
Linear comment is unrecoverable; this filter is the last line of defense for
app-mediated posts, not the only control."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("devcake.security")

# Platform credentials (harness keys, infra passwords) are static; PMO/forge
# secrets are contributed by EVERY registered adapter via the registry —
# configured or not, so switching adapters never opens a redaction gap.
# test_security.py pins the union as a superset of the v0 lists.
PLATFORM_SECRET_ENV_VARS = [
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "XAI_API_KEY",
    "OPENAI_API_KEY", "CODEX_API_KEY", "REDIS_PASSWORD", "DAGU_PASSWORD",
    "ADMIN_PASSWORD", "OO_ROOT_PASSWORD", "OO_INGEST_PASSWORD",
]

# harness/model key shapes (docs/14 §5) — not adapter-owned
PLATFORM_TOKEN_PATTERNS = [
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
    r"\bxai-[A-Za-z0-9_-]{20,}\b",
]

_registry_cache: tuple[list[str], list[str]] | None = None


def _registry_contributions() -> tuple[list[str], list[str]]:
    """(env_vars, pattern_sources) from every registered PMO system and forge.
    Lazy + memoized: security is imported by adapters, so the registry import
    must happen at first redact() call, never at module import."""
    global _registry_cache
    if _registry_cache is None:
        from .adapters.registry import PMO_SYSTEMS, forges
        envs: list[str] = []
        pats: list[str] = []
        for s in PMO_SYSTEMS.values():
            envs += s.secret_env_vars
            pats += s.token_patterns
        for d in forges().values():
            envs += d.secret_env_vars
            pats += d.token_patterns
        _registry_cache = (envs, pats)
    return _registry_cache


def secret_env_vars() -> list[str]:
    return PLATFORM_SECRET_ENV_VARS + _registry_contributions()[0]


def token_patterns() -> list[re.Pattern]:
    return [re.compile(p)
            for p in PLATFORM_TOKEN_PATTERNS + _registry_contributions()[1]]

MASK = "«REDACTED»"
_SECRETS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"

# Ephemeral per-run credentials (the Redis relay password) exist only as
# process-local values — never in env or on disk — so redact() can only learn
# them through this registry. Registered at ACL-user creation, dropped at
# teardown; stale entries are harmless (over-redacting a dead random token).
_runtime_secrets: dict[str, str] = {}


def register_runtime_secret(key: str, value: str) -> None:
    if value:
        _runtime_secrets[key] = value


def unregister_runtime_secret(key: str) -> None:
    _runtime_secrets.pop(key, None)


# Paths already reported as unreadable — _known_values runs on every redact()
# call, so a broken file must alarm once, not flood the logs.
_reported_unreadable: set[str] = set()
# Strong references to in-flight alarm tasks: the event loop only holds weak
# refs, so a fire-and-forget create_task could be GC'd before it runs.
_alarm_tasks: set = set()


def _report_unreadable(path: Path, exc: Exception) -> None:
    """A secrets file this filter can't parse is a live redaction gap: its
    values will NOT be masked in PMO-bound content. Alarm loudly (log + OO
    run_failures stream, best effort) but never break the redacting caller."""
    if str(path) in _reported_unreadable:
        return
    _reported_unreadable.add(str(path))
    log.error("unreadable secrets file %s — its values are NOT being redacted: %s",
              path, exc)
    try:
        import asyncio
        from .telemetry import push_oo_log
        task = asyncio.get_running_loop().create_task(push_oo_log("run_failures", {
            "level": "error",
            "outcome": "redaction_gap",
            "reason": f"unreadable secrets file: {path.name}",
            "detail": f"{path}: {exc}",
        }))
        _alarm_tasks.add(task)
        task.add_done_callback(_alarm_tasks.discard)
    except RuntimeError:
        pass  # no running event loop (sync caller) — the ERROR log stands


def _known_values() -> list[str]:
    values = [v for var in secret_env_vars() if (v := os.environ.get(var, "").strip())]
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
        except Exception as exc:
            _report_unreadable(p, exc)
            continue
    return sorted(set(values), key=len, reverse=True)  # longest first


def redact(text: str, extra_values: list[str] | None = None) -> str:
    """Scrub known secret values + token patterns from PMO-bound content."""
    if not text:
        return text
    runtime = sorted(_runtime_secrets.values(), key=len, reverse=True)
    for value in _known_values() + runtime + [v for v in (extra_values or []) if v]:
        if len(value) >= 8:
            text = text.replace(value, MASK)
    for pattern in token_patterns():
        text = pattern.sub(MASK, text)
    return text


def redact_value(value: Any, extra_values: list[str] | None = None) -> Any:
    """Recursively scrub strings before Dev-controlled structures are persisted."""
    if isinstance(value, str):
        return redact(value, extra_values)
    if isinstance(value, list):
        return [redact_value(item, extra_values) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, extra_values) for key, item in value.items()}
    return value


def security_warnings(config) -> list[dict]:
    """Loud-but-dismissable credential-posture warnings (ISSUES #13/#15).
    Surfaced in /health; the SPA renders them as dismissable alerts (dismissals
    persist in config.dismissed_alerts, and resurface if the content changes).
    Lives here (not api/) so the copy is testable without the api singletons;
    all wording derives from config/descriptors — never from forge names (F1)."""
    # (the former `oo-root-creds` warning is structurally gone as of M8:
    # Devs export through the otel-collector and receive NO OO credentials —
    # there is no per-run credential whose posture could degrade.)
    warns = []
    if config.repos[0].token and not config.repos[0].token_ro:
        warns.append({
            "id": "forge-write-token", "severity": "warning",
            "title": "All mission stages hold the forge WRITE token",
            "body": "No read-only PAT is configured (token_ro_env / "
                    f"{config.repos[0].resolved_token_env}_RO), so PLAN/REVIEW/MAPPER/ONBOARD "
                    "Devs receive the same write-capable forge token as EXECUTE. "
                    "A prompt-injected non-EXECUTE Dev could push to the repo. "
                    f"Create a read-only PAT and set {config.repos[0].resolved_token_env}_RO "
                    "in .env (ISSUES #15).",
        })
    return warns
