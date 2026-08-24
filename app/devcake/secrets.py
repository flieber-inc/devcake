"""GUI-stored operator secrets (docs/16 M12, F5). The single-mode replacement
for env-var indirection: the operator supplies secret VALUES through the
Config page; they live 0600 under /data/secrets/, redaction-registered, and
are NEVER echoed back.

Layout — deliberately two path levels so security._known_values()'s existing
glob("*/*") scan auto-redacts every value:
    /data/secrets/connections/{scope}-{instance}.json   scope ∈ pmo | repo | skill
    /data/secrets/harness/{VAR}.json                    harness/model keys

A connection file holds a flat {field: value} dict (e.g. {"api_key": …} for a
pmo, {"token", "token_ro", "reviewer_token"} for a repo); a harness file holds
{"value": …}. Reads are through-the-file (low call frequency); nothing is
cached in memory beyond the redaction registry.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from . import security
from .config import DEV_TYPE_NAME_RE, HARNESS_VAR_PATTERN, _INSTANCE_NAME_RE
from .pathsafety import confined

log = logging.getLogger("devcake.secrets")

# ONE definition of the per-scope connection-secret field allowlist —
# api.connections_service validates endpoint input against it and
# settings_bundle validates bundle shapes. scope/instance/field become path
# components everywhere, so every entry point re-validates against this
# (audit A5/A9).
CONNECTION_FIELDS: dict[str, set[str]] = {
    "pmo": {"api_key"},
    "repo": {"token", "token_ro", "reviewer_token"},
    # dedicated skills connections (2026-08-14): read tokens only — a
    # skills source has no PR surface and is read-only by construction
    "skill": {"token", "token_ro"},
}

_HARNESS_VAR_RE = re.compile(f"^{HARNESS_VAR_PATTERN}$")


def connection_instances(config) -> tuple[tuple[str, str], ...]:
    """`(scope, name)` for every connection card on config — pmo, repo, skill.

    ONE derivation of the live instance set: settings_bundle serialize /
    validate / apply and config PUT cleanup must not hard-code a subset of
    scopes. Accepts an AppConfig or a `model_dump()` dict.
    """
    if isinstance(config, dict):
        pmos = config.get("pmos") or []
        repos = config.get("repos") or []
        skills = config.get("skill_sources") or []

        def _name(x) -> str:
            return x["name"] if isinstance(x, dict) else x.name
    else:
        pmos = config.pmos
        repos = config.repos
        skills = getattr(config, "skill_sources", None) or []

        def _name(x) -> str:
            return x.name

    return tuple(
        [("pmo", _name(p)) for p in pmos]
        + [("repo", _name(r)) for r in repos]
        + [("skill", _name(s)) for s in skills]
    )


def connection_instance_keys(config) -> set[str]:
    """`{scope}-{name}` keys for every connection card on config."""
    return {f"{scope}-{name}" for scope, name in connection_instances(config)}


def _root() -> Path:
    return Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"


def _conn_path(scope: str, instance: str) -> Path:
    """Build connections/{scope}-{instance}.json under the secrets root.

    Validates scope + instance *before* interpolating so a skipped caller
    gate cannot turn ``instance`` into a path separator or ``..`` escape
    (CAKE-136 / CodeQL py/path-injection).
    """
    if scope not in CONNECTION_FIELDS:
        raise ValueError(f"unknown connection scope {scope!r}")
    if not instance or re.fullmatch(_INSTANCE_NAME_RE, instance) is None:
        raise ValueError(f"invalid connection instance {instance!r}")
    return confined(_root() / "connections", f"{scope}-{instance}.json")


def _harness_path(var: str) -> Path:
    """Build harness/{VAR}.json under the secrets root (store-level gate)."""
    if not var or _HARNESS_VAR_RE.fullmatch(var) is None:
        raise ValueError(f"invalid harness var {var!r}")
    return confined(_root() / "harness", f"{var}.json")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp + fsync + chmod 0600 + replace + dir fsync (POSIX atomic durable)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # 0600 by default
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir(path.parent)          # make the rename itself durable
        # 2026-08 evaluation F17: every store write funnels through here, so
        # this ONE call keeps security's _known_values result cache exact in
        # the direction that matters — a just-stored value is rescanned
        # before the next redact(). Deletes deliberately do not invalidate
        # (stale-extra masking is the safe direction).
        from .security import invalidate_secret_scan
        invalidate_secret_scan()
    except Exception:  # noqa: BLE001 — temp cleanup then re-raise (atomic write contract)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _atomic_write(path: Path, data: dict) -> None:
    _atomic_write_bytes(path, json.dumps(data).encode())


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> dict:
    """Lenient read for status/read-through paths: a corrupt file reads as
    absent (the redaction scanner alarms on it separately). Wrong-TYPE JSON
    (a list/string that parses) is corrupt too — without the isinstance
    check it escaped the except and the caller's .get AttributeError'd
    inside the poll cycle (2026-08-12 audit SEC-10)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except Exception:  # noqa: BLE001 — lenient-read contract: corrupt file reads as absent (logged); redaction scanner alarms separately
        log.error("unreadable secret file %s", path)
        return {}


def _read_strict(path: Path) -> dict:
    """Strict read for read-modify-WRITE paths: silently treating a corrupt
    file as {} would drop the sibling fields it once held (audit A18)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except Exception as e:
        raise ValueError(
            f"corrupt secret file {path.name!r} — refusing read-modify-write; "
            f"delete or repair the file under /data/secrets/") from e


# ── redaction registration keys (write + boot-register must match) ──────────

def conn_redact_key(scope: str, instance: str, field: str) -> str:
    return f"conn:{scope}:{instance}:{field}"


def harness_redact_key(var: str) -> str:
    return f"harness:{var}"


def cred_redact_key(dev_type: str, filename: str) -> str:
    return f"cred:{dev_type}:{filename}"


# ── connection secrets (pmo/repo credentials) ───────────────────────────────

def write_connection_secret(scope: str, instance: str, field: str,
                            value: str) -> None:
    path = _conn_path(scope, instance)
    data = _read_strict(path)
    data[field] = value
    _atomic_write(path, data)
    if value:
        security.register_runtime_secret(
            conn_redact_key(scope, instance, field), value)


def read_connection_secret(scope: str, instance: str, field: str) -> str:
    return _read(_conn_path(scope, instance)).get(field, "")


def delete_connection_field(scope: str, instance: str, field: str) -> None:
    """Remove one field (a REAL delete, not an empty-string write — audit
    A9/A18); unlink the file once its last field is gone. A missing file is a
    no-op — deleting must never CREATE a file. The redaction registration is
    deliberately kept until restart: unregistering a just-revoked value is
    the risky direction."""
    path = _conn_path(scope, instance)
    if not path.exists():
        return
    data = _read_strict(path)
    data.pop(field, None)
    if data:
        _atomic_write(path, data)
    else:
        path.unlink(missing_ok=True)


def delete_connection_instance(scope: str, instance: str) -> None:
    """Unlink the connection secrets file. Redaction registrations are
    deliberately kept until process restart — same safe direction as
    delete_connection_field / delete_harness_secret (a just-revoked value
    must still scrub late PMO/forge writes)."""
    path = _conn_path(scope, instance)
    path.unlink(missing_ok=True)


def rename_connection_instance(scope: str, old: str, new: str) -> None:
    """Move `connections/{scope}-{old}.json` → `{scope}-{new}.json`.

    Used when a config PUT renames a card in place (same list index, new
    name) so tokens follow the card instead of orphaning under the old
    name. Missing source is a no-op; if the destination already exists it
    is replaced (the renamed card owns that identity). Old redaction
    registrations are kept until restart (safe direction); values are
    re-registered under the new instance name.
    """
    if old == new:
        return
    src = _conn_path(scope, old)
    dst = _conn_path(scope, new)
    if not src.exists():
        return
    data = _read_strict(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)
    _fsync_dir(dst.parent)
    from .security import invalidate_secret_scan
    invalidate_secret_scan()
    for field, value in data.items():
        if isinstance(value, str) and value:
            security.register_runtime_secret(
                conn_redact_key(scope, new, field), value)


# ── harness/model secrets ───────────────────────────────────────────────────

def write_harness_secret(var: str, value: str) -> None:
    _atomic_write(_harness_path(var), {"value": value})
    if value:
        security.register_runtime_secret(harness_redact_key(var), value)


def read_harness_secret(var: str) -> str:
    return _read(_harness_path(var)).get("value", "")


def delete_harness_secret(var: str) -> None:
    """Revoke a stored harness/model key (audit A10). Redaction registration
    is kept until restart — the safe direction for a just-revoked value."""
    _harness_path(var).unlink(missing_ok=True)


# ── per-Dev-Type credential files (OAuth / uploaded secrets) ────────────────
# /data/secrets/{dev_type}/{filename} — NOT under harness/ or connections/.
# Reserved top-level names are the structured secret stores; everything else
# is treated as a Dev Type credential directory (see inventory()).

_RESERVED_SECRET_DIRS = frozenset({
    "connections", "harness", "profiles", "internal_forge",
})


def write_system_secret_json(subdir: str, filename: str, data: dict) -> Path:
    """Atomic 0600 JSON under /data/secrets/{subdir}/{filename}.

    For system-managed reserved dirs (internal_forge, …) that share the
    redaction glob. Same write choke point as connection/harness secrets —
    invalidates the redaction scan cache. Path components are validated so
    callers cannot escape the secrets root.
    """
    if subdir not in _RESERVED_SECRET_DIRS:
        raise ValueError(f"not a system secret dir: {subdir!r}")
    base = os.path.basename(filename or "")
    if (not base or base != filename or base in (".", "..")
            or "/" in base or "\\" in base):
        raise ValueError(f"invalid system secret filename {filename!r}")
    path = _root() / subdir / base
    _atomic_write(path, data)
    return path


def require_credential_ref(dev_type: str, filename: str) -> None:
    """Path components only — refuse reserved dirs and traversal."""
    if (not dev_type or dev_type in _RESERVED_SECRET_DIRS
            or "/" in dev_type or "\\" in dev_type or ".." in dev_type
            or not DEV_TYPE_NAME_RE.fullmatch(dev_type)):
        raise ValueError(f"invalid credential dev_type {dev_type!r}")
    base = os.path.basename(filename or "")
    if (not base or base != filename or base in (".", "..")
            or "/" in base or "\\" in base):
        raise ValueError(f"invalid credential filename {filename!r}")


def credential_path(dev_type: str, filename: str) -> Path:
    """Confined path under /data/secrets/{dev_type}/{filename}.

    Allowlist via ``require_credential_ref`` then ``confined`` — the single
    builder for credential write / read / delete and the grok-auth lock
    sidecar. Callers that only need bytes should prefer
    ``read_credential_file`` / ``write_credential_file``.
    """
    require_credential_ref(dev_type, filename)
    return confined(_root(), dev_type, filename)


# Operator-uploaded / OAuth credential files (raw text, not JSON dicts).
MAX_CREDENTIAL_FILE_BYTES = 1 * 1024 * 1024


def write_credential_file(dev_type: str, filename: str, content: str) -> Path:
    """Atomically write a raw credential file under /data/secrets/{dev_type}/.
    0600; registers content for redaction when long enough (≥8)."""
    raw = content if isinstance(content, str) else str(content or "")
    data = raw.encode()
    if len(data) > MAX_CREDENTIAL_FILE_BYTES:
        raise ValueError(
            f"credential file too large ({len(data)} > {MAX_CREDENTIAL_FILE_BYTES})")
    path = credential_path(dev_type, filename)
    _atomic_write_bytes(path, data)
    if len(raw) >= 8:
        security.register_runtime_secret(
            cred_redact_key(dev_type, filename), raw)
    return path


def read_credential_file(dev_type: str, filename: str) -> str | None:
    """Read a raw credential file. ``None`` when absent (caller logs)."""
    path = credential_path(dev_type, filename)
    if not path.exists():
        return None
    return path.read_text()


def delete_credential_file(dev_type: str, filename: str) -> None:
    """Unlink one OAuth/uploaded credential file. Missing = no-op."""
    path = credential_path(dev_type, filename)
    path.unlink(missing_ok=True)
    # drop empty dir so inventory doesn't keep a ghost. suppress: a concurrent
    # OAuth write can race the empty check (TOCTOU) — unlink already succeeded.
    parent = path.parent
    if parent.is_dir() and not any(parent.iterdir()):
        with contextlib.suppress(OSError):
            parent.rmdir()

# ── status (never echoes values) ────────────────────────────────────────────

def _status(path: Path, field: str | None = None) -> dict:
    """{present, updated_at} — NO value, NO value-derived fingerprint (an
    unsalted hash would let anyone who sees the UI confirm a guessed secret
    offline; review finding L3 of the v0.1 plan)."""
    if not path.exists():
        return {"present": False, "updated_at": None}
    data = _read(path)
    present = bool(data.get(field)) if field else bool(data.get("value"))
    return {"present": present,
            "updated_at": _iso(path.stat().st_mtime) if present else None}


def connection_status(scope: str, instance: str, field: str) -> dict:
    """Presence for one connection field.

    ``updated_at`` is the shared file mtime only when this field is the sole
    present value — multi-field connection files share one clock, so reporting
    that mtime for every field falsely bumps siblings after a single-field
    write. Prefer null over lying when more than one field is set
    (harness_status keeps mtime — one value per file).
    """
    path = _conn_path(scope, instance)
    if not path.exists():
        return {"present": False, "updated_at": None}
    data = _read(path)
    present = bool(data.get(field))
    if not present:
        return {"present": False, "updated_at": None}
    present_fields = [k for k, v in data.items() if isinstance(v, str) and v]
    if len(present_fields) == 1 and present_fields[0] == field:
        return {"present": True, "updated_at": _iso(path.stat().st_mtime)}
    return {"present": True, "updated_at": None}


def harness_status(var: str) -> dict:
    return _status(_harness_path(var))


def _iso(mtime: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def list_connection_secrets() -> dict[str, dict[str, str]]:
    """{"{scope}-{instance}": {field: value}} for every stored connection
    secret. VALUE-BEARING — the settings-bundle serializer and boot
    registration are the only consumers (ADR-0013); the API never echoes
    these."""
    out: dict[str, dict[str, str]] = {}
    conn_dir = _root() / "connections"
    if conn_dir.exists():
        for p in sorted(conn_dir.glob("*.json")):
            fields = {k: v for k, v in _read(p).items()
                      if isinstance(v, str) and v}
            if fields:
                out[p.stem] = fields
    return out


def list_harness_secrets() -> dict[str, str]:
    """{VAR: value} for every stored harness/model key. Value-bearing — same
    consumers and same caveat as list_connection_secrets."""
    out: dict[str, str] = {}
    harness_dir = _root() / "harness"
    if harness_dir.exists():
        for p in sorted(harness_dir.glob("*.json")):
            value = _read(p).get("value")
            if isinstance(value, str) and value:
                out[p.stem] = value
    return out


def inventory() -> dict[str, list[dict]]:
    """Presence-only catalog of every operator-clearable secret. NEVER values.

    Groups:
      harness           — model / secret_env keys under harness/
      connections       — pmo/repo/skill fields under connections/
      credential_files  — OAuth/upload files under /data/secrets/{dev_type}/

    Excludes profile snapshots and internal_forge mission tokens (system-
    managed; not operator "Clear secrets" targets).
    """
    harness: list[dict] = []
    for var in list_harness_secrets():
        st = harness_status(var)
        if st["present"]:
            harness.append({"var": var, "updated_at": st["updated_at"]})

    connections: list[dict] = []
    for key, fields in list_connection_secrets().items():
        scope, _, instance = key.partition("-")
        if not scope or not instance:
            continue
        for field in fields:
            st = connection_status(scope, instance, field)
            if st["present"]:
                connections.append({
                    "scope": scope, "instance": instance, "field": field,
                    "updated_at": st["updated_at"],
                })

    credential_files: list[dict] = []
    root = _root()
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name in _RESERVED_SECRET_DIRS:
                continue
            if not DEV_TYPE_NAME_RE.fullmatch(d.name):
                continue
            for p in sorted(d.iterdir()):
                if p.is_file():
                    credential_files.append({
                        "dev_type": d.name,
                        "filename": p.name,
                        "updated_at": _iso(p.stat().st_mtime),
                    })

    return {
        "harness": harness,
        "connections": connections,
        "credential_files": credential_files,
    }


def register_all() -> None:
    """Register every stored secret with the redaction layer at boot (the
    glob scan already covers them, but explicit registration means immediate
    coverage for exact-match replacement even below the 16-char scan floor).
    Keys MUST match write_/delete_'s scheme or unregister leaves the
    boot-registered copy behind.

    Credential files (OAuth/upload) are raw text — the JSON-only disk scan
    misses non-JSON blobs and values under the 16-char floor, so they are
    registered here under the same ``cred:{dev}:{file}`` keys as
    write_credential_file.
    """
    for key, fields in list_connection_secrets().items():
        # {scope}-{instance}; instance names can't contain hyphens
        scope, _, instance = key.partition("-")
        for field, value in fields.items():
            security.register_runtime_secret(
                conn_redact_key(scope, instance, field), value)
    for var, value in list_harness_secrets().items():
        security.register_runtime_secret(harness_redact_key(var), value)
    for cf in inventory().get("credential_files") or []:
        dev, fname = cf.get("dev_type"), cf.get("filename")
        if not dev or not fname:
            continue
        try:
            require_credential_ref(dev, fname)
            raw = (_root() / dev / fname).read_text()
        except (ValueError, OSError) as e:
            log.error("boot redaction: unreadable credential %s/%s: %s",
                      dev, fname, e)
            continue
        if len(raw) >= 8:
            security.register_runtime_secret(cred_redact_key(dev, fname), raw)


# ── profile secret snapshots (ADR-0013) ─────────────────────────────────────
# /data/secrets/profiles/{name}.json — ONE json per profile, 0600. The file
# sits at glob level two, so security._known_values()'s glob("*/*") covers a
# dormant snapshot with zero redaction changes (values <16 chars stay below
# the scan floor until applied — the documented residual). Shape mirrors the
# bundle secrets section: {"connections": {...}, "harness": {...}}.

def _profile_path(name: str) -> Path:
    """Build profiles/{name}.json under the secrets root (store-level gate).

    Charset matches ``profiles.PROFILE_NAME_RE`` (lazy import avoids the
    profiles → secrets cycle). ``confined`` then closes traversal.
    """
    from .profiles import PROFILE_NAME_RE
    if not PROFILE_NAME_RE.fullmatch(name or ""):
        raise ValueError(f"invalid profile name {name!r}")
    return confined(_root() / "profiles", f"{name}.json")


def write_profile_secrets(name: str, data: dict) -> None:
    _atomic_write(_profile_path(name), data)


def read_profile_secrets(name: str) -> dict | None:
    """None when the profile stores no secrets. Corrupt files REFUSE (strict
    read): silently applying half a snapshot would delete every live secret
    the missing half once named."""
    p = _profile_path(name)
    if not p.exists():
        return None
    return _read_strict(p)


def delete_profile_secrets(name: str) -> None:
    _profile_path(name).unlink(missing_ok=True)


def rename_profile_secrets(name: str, new_name: str) -> None:
    p = _profile_path(name)
    if p.exists():
        os.replace(p, _profile_path(new_name))
