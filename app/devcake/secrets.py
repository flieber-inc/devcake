"""GUI-stored operator secrets (docs/16 M12, F5). The single-mode replacement
for env-var indirection: the operator supplies secret VALUES through the
Config page; they live 0600 under /data/secrets/, redaction-registered, and
are NEVER echoed back.

Layout — deliberately two path levels so security._known_values()'s existing
glob("*/*") scan auto-redacts every value:
    /data/secrets/connections/{scope}-{instance}.json   scope ∈ pmo | repo
    /data/secrets/harness/{VAR}.json                    harness/model keys

A connection file holds a flat {field: value} dict (e.g. {"api_key": …} for a
pmo, {"token", "token_ro", "reviewer_token"} for a repo); a harness file holds
{"value": …}. Reads are through-the-file (low call frequency); nothing is
cached in memory beyond the redaction registry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from . import security

log = logging.getLogger("devcake.secrets")


def _root() -> Path:
    return Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"


def _conn_path(scope: str, instance: str) -> Path:
    return _root() / "connections" / f"{scope}-{instance}.json"


def _harness_path(var: str) -> Path:
    return _root() / "harness" / f"{var}.json"


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # 0600 by default
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir(path.parent)          # make the rename itself durable
    except Exception:
        with __import__("contextlib").suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read(path: Path) -> dict:
    """Lenient read for status/read-through paths: a corrupt file reads as
    absent (the redaction scanner alarms on it separately)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        log.error("unreadable secret file %s", path)
        return {}


def _read_strict(path: Path) -> dict:
    """Strict read for read-modify-WRITE paths: silently treating a corrupt
    file as {} would drop the sibling fields it once held (audit A18)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        raise ValueError(
            f"corrupt secret file {path.name!r} — refusing read-modify-write; "
            f"delete or repair the file under /data/secrets/") from e


# ── connection secrets (pmo/repo credentials) ───────────────────────────────

def write_connection_secret(scope: str, instance: str, field: str,
                            value: str) -> None:
    path = _conn_path(scope, instance)
    data = _read_strict(path)
    data[field] = value
    _atomic_write(path, data)
    if value:
        security.register_runtime_secret(f"conn:{scope}:{instance}:{field}", value)


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
    path = _conn_path(scope, instance)
    for field in _read(path):
        security.unregister_runtime_secret(f"conn:{scope}:{instance}:{field}")
    path.unlink(missing_ok=True)


# ── harness/model secrets ───────────────────────────────────────────────────

def write_harness_secret(var: str, value: str) -> None:
    _atomic_write(_harness_path(var), {"value": value})
    if value:
        security.register_runtime_secret(f"harness:{var}", value)


def read_harness_secret(var: str) -> str:
    return _read(_harness_path(var)).get("value", "")


def delete_harness_secret(var: str) -> None:
    """Revoke a stored harness/model key (audit A10). Redaction registration
    is kept until restart — the safe direction for a just-revoked value."""
    _harness_path(var).unlink(missing_ok=True)


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
    return _status(_conn_path(scope, instance), field)


def harness_status(var: str) -> dict:
    return _status(_harness_path(var))


def _iso(mtime: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat()


def register_all() -> None:
    """Register every stored secret with the redaction layer at boot (the
    glob scan already covers them, but explicit registration means immediate
    coverage for exact-match replacement even below the 16-char scan floor).
    Keys MUST match write_/delete_'s scheme or unregister leaves the
    boot-registered copy behind."""
    root = _root()
    conn_dir = root / "connections"
    if conn_dir.exists():
        for p in conn_dir.glob("*.json"):
            # {scope}-{instance}.json; instance names can't contain hyphens
            scope, _, instance = p.stem.partition("-")
            for field, value in _read(p).items():
                if isinstance(value, str) and value:
                    security.register_runtime_secret(
                        f"conn:{scope}:{instance}:{field}", value)
    harness_dir = root / "harness"
    if harness_dir.exists():
        for p in harness_dir.glob("*.json"):
            value = _read(p).get("value")
            if isinstance(value, str) and value:
                security.register_runtime_secret(f"harness:{p.stem}", value)
