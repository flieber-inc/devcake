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
    "GITEA_ADMIN_PASSWORD",
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
# Values displaced by register overwrite (credential rotation). Kept until
# process restart so a just-rotated secret still scrubs late PMO/forge writes
# — same safe direction as delete_connection_* keeping redaction on delete.
# Bounded so pathological rotate loops cannot grow forever.
_RUNTIME_SECRET_PRIOR_CAP = 64
_runtime_secret_priors: list[str] = []


def register_runtime_secret(key: str, value: str) -> None:
    if not value:
        return
    prev = _runtime_secrets.get(key)
    if prev and prev != value and len(prev) >= 8:
        if prev not in _runtime_secret_priors:
            _runtime_secret_priors.append(prev)
            while len(_runtime_secret_priors) > _RUNTIME_SECRET_PRIOR_CAP:
                _runtime_secret_priors.pop(0)
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


# path → (mtime_ns, size, extracted values): redact() runs on every PMO- and
# forge-bound write, and re-parsing every /data/secrets JSON per call grows
# with mission count (M11 adds one file per internal mission — audit A28).
# Unchanged files serve from here; changed files re-parse; removed drop out.
_scan_cache: dict[str, tuple[int, int, list[str]]] = {}


# Structured secret dirs expect JSON; raw OAuth/upload blobs live under
# Dev-type dirs and are covered by register_all / write_credential_file.
_STRUCTURED_SECRET_DIRS = frozenset({
    "connections", "harness", "profiles", "internal_forge",
})


def _file_values(p) -> list[str]:
    """Long-ish string values of one stored-secret JSON, mtime/size-cached.

    Returns None when *p* is a non-JSON credential file under a Dev-type
    directory (not a redaction gap — runtime registration owns those).
    """
    st = p.stat()
    key = str(p)
    hit = _scan_cache.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and len(o) >= 16:
            found.append(o)

    try:
        walk(json.loads(p.read_text()))
    except json.JSONDecodeError:
        # Raw credential files under /data/secrets/{dev_type}/ are intentional
        # non-JSON; boot register_all covers them. Do not alarm redaction_gap.
        if p.parent.name not in _STRUCTURED_SECRET_DIRS:
            _scan_cache[key] = (st.st_mtime_ns, st.st_size, [])
            return []
        raise
    _scan_cache[key] = (st.st_mtime_ns, st.st_size, found)
    return found


def _known_values() -> list[str]:
    values = [v for var in secret_env_vars() if (v := os.environ.get(var, "").strip())]
    # credential file key material: every long-ish string value in stored JSONs
    live_paths = set()
    for p in _SECRETS_DIR.glob("*/*"):
        try:
            live_paths.add(str(p))
            values.extend(_file_values(p))
        except Exception as exc:  # noqa: BLE001 — redaction-source scan must cover the rest; unreadable file reported once
            _report_unreadable(p, exc)
            continue
    for stale in set(_scan_cache) - live_paths:      # deleted files drop out
        del _scan_cache[stale]
    return sorted(set(values), key=len, reverse=True)  # longest first


def redact(text: str, extra_values: list[str] | None = None) -> str:
    """Scrub known secret values + token patterns from PMO-bound content."""
    if not text:
        return text
    runtime = sorted(
        list(_runtime_secrets.values()) + list(_runtime_secret_priors),
        key=len, reverse=True)
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
    warns = [{
        "id": "gui-secrets-basic-auth", "severity": "info",
        "title": "GUI-stored secrets behind basic auth",
        "body": "Operator secrets (PMO/forge tokens, model keys) are stored "
                "0600 on the app volume and the admin panel is protected only "
                "by HTTP basic auth — which, since settings export exists "
                "(ADR-0013), includes the ability to export stored secret "
                "values. This is fine on localhost/a dedicated host; revisit "
                "(OIDC/SSO, secret-manager) before exposing DevCake beyond "
                "localhost (docs/14 §7).",
    }]
    for repo in config.repos:
        if repo.configured and repo.token and not repo.token_ro:
            # per-repo id (M10): dismissing repo A's warning must not
            # dismiss repo B's (v0.1 plan finding L8)
            warns.append({
                "id": f"forge-write-token:{repo.name}", "severity": "warning",
                "title": f"All mission stages hold repo '{repo.name}'s "
                         "WRITE token",
                "body": f"No read-only PAT is set for repo '{repo.name}', so "
                        "PLAN/REVIEW/MAPPER/ONBOARD Devs receive the same "
                        "write-capable forge token as EXECUTE. A prompt-injected "
                        "non-EXECUTE Dev could push to the repo. Add a read-only "
                        "token for this repo on the Config page (ISSUES #15).",
            })
    # read-only-only repositories (founder request 2026-07-15): valid — but
    # only as REFERENCE material. Warn when such a repo sits in a PMO's WORK
    # set, where EXECUTE would fail at push for lack of a write token.
    from . import secrets as _secrets
    for repo in config.repos:
        has_ro = bool(_secrets.read_connection_secret("repo", repo.name, "token_ro"))
        has_w = bool(_secrets.read_connection_secret("repo", repo.name, "token"))
        if has_ro and not has_w and any(
                repo.name in (p.repos or []) for p in config.pmos):
            warns.append({
                "id": f"repo-read-only:{repo.name}", "severity": "warning",
                "title": f"Repo '{repo.name}' has only a read-only token",
                "body": (f"'{repo.name}' is in a PMO instance's WORK repo set "
                         f"but stores no write token — missions routed to it "
                         f"will fail at push. Read-only repos are meant as "
                         f"REFERENCE material: move it to the instance's "
                         f"Reference repos, or add a write token."),
            })
    return warns
