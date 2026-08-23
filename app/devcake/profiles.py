"""Config profiles (ADR-0013): named, fully independent snapshots of the
deployment's runtime settings — section A (configs) and optionally section B
(secret values) of a settings bundle.

Storage splits a profile across the two stores it snapshots, preserving each
store's invariants:
    /data/config/profiles/{name}.yaml    section A + metadata (plain, diffable)
    /data/secrets/profiles/{name}.json   section B (0600, redaction-glob level)

A profile is fire-and-forget: applying one replaces the live world, but later
edits do not update the profile, and there is no "active profile" — an honest
match indicator would need value-derived secret fingerprints, which ADR-0011
bans. What we track instead is the last-applied breadcrumb (a /data/state
sidecar, deliberately outside bundles and harmlessly wiped by clear-runs) and
a divergence boolean computed from non-secret dict comparison plus secret
updated_at timestamps — never values.

Profiles have no built-ins, no seeding, and no fallback pointer: deleting one
breaks nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import yaml

from . import secrets as secrets_store
from .config import AppConfig, DevType, _atomic_yaml
from .pathsafety import confined
from .settings_bundle import (BUNDLE_KIND, BUNDLE_SCHEMA_VERSION, BundleError,
                              _utcnow, serialize_current)

log = logging.getLogger("devcake.profiles")

# prompt-template naming convention (templates.py) — locked after creation;
# rename is an explicit endpoint. Validated BEFORE any path join (audit A5/A9).
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


def _dir() -> Path:
    return (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
            / "config" / "profiles")


def _path(name: str) -> Path:
    return confined(_dir(), f"{name}.yaml")


def _state_path() -> Path:
    return (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
            / "state" / "profiles.json")


def require_name(name: str) -> None:
    if not PROFILE_NAME_RE.fullmatch(name or ""):
        raise BundleError(422, "profile name must match "
                               "^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


# ── CRUD ─────────────────────────────────────────────────────────────────────

def save_profile(name: str, bundle: dict, *, overwrite: bool = False) -> None:
    """Persist a bundle as a named profile: section A + metadata to the yaml,
    section B (when present) to the sibling secrets json."""
    require_name(name)
    if _path(name).exists() and not overwrite:
        raise BundleError(409, f"a profile named {name!r} already exists")
    doc = {k: v for k, v in bundle.items() if k != "secrets"}
    doc["kind"] = BUNDLE_KIND
    doc["name"] = name
    _atomic_yaml(_path(name), doc)
    if "secrets" in bundle:
        secrets_store.write_profile_secrets(name, bundle["secrets"])
    else:
        secrets_store.delete_profile_secrets(name)   # overwrite may narrow


def read_profile(name: str) -> dict:
    """Recombine yaml + secrets json into one bundle dict. A profile whose
    sections list promises secrets but whose json is missing REFUSES — a
    silent A-only apply would delete every live secret's pairing."""
    require_name(name)
    p = _path(name)
    if not p.exists():
        raise BundleError(404, f"no profile named {name!r}")
    doc = yaml.safe_load(p.read_text())
    if not isinstance(doc, dict):
        raise BundleError(422, f"profile {name!r} is unreadable")
    if doc.get("bundle_schema_version") != BUNDLE_SCHEMA_VERSION:
        raise BundleError(
            422, f"profile {name!r} carries bundle_schema_version "
                 f"{doc.get('bundle_schema_version')!r}, current is "
                 f"{BUNDLE_SCHEMA_VERSION} — profiles are not auto-migrated; "
                 "re-save the profile from current settings")
    if "secrets" in (doc.get("sections") or []):
        try:
            sec = secrets_store.read_profile_secrets(name)
        except ValueError as e:
            raise BundleError(422, str(e))
        if sec is None:
            raise BundleError(
                422, f"profile {name!r} promises a secrets section but its "
                     "snapshot file is missing — re-save the profile")
        doc["secrets"] = sec
    return doc


def delete_profile(name: str) -> None:
    require_name(name)
    p = _path(name)
    if not p.exists():
        raise BundleError(404, f"no profile named {name!r}")
    p.unlink()
    secrets_store.delete_profile_secrets(name)


def rename_profile(name: str, new_name: str) -> None:
    require_name(name)
    require_name(new_name)
    src = _path(name)
    if not src.exists():
        raise BundleError(404, f"no profile named {name!r}")
    if _path(new_name).exists():
        raise BundleError(409, f"a profile named {new_name!r} already exists")
    doc = yaml.safe_load(src.read_text()) or {}
    doc["name"] = new_name
    _atomic_yaml(_path(new_name), doc)
    src.unlink()
    secrets_store.rename_profile_secrets(name, new_name)
    state = _read_state()
    if (state.get("last_applied") or {}).get("name") == name:
        state["last_applied"]["name"] = new_name
        _write_state(state)


def list_profiles() -> list[dict]:
    """Metadata rows for the admin table — counts and presence only, never a
    secret value. Unreadable profile files list as broken rather than vanish."""
    if not _dir().is_dir():
        return []
    out = []
    for p in sorted(_dir().glob("*.yaml")):
        name = p.stem
        try:
            doc = yaml.safe_load(p.read_text())
            assert isinstance(doc, dict)
        except Exception:  # noqa: BLE001 — a broken profile renders as broken in the list; listing must not 500
            out.append({"name": name, "broken": True})
            continue
        cfg = doc.get("config") or {}
        app = cfg.get("app") or {}
        secret_count = 0
        if "secrets" in (doc.get("sections") or []):
            try:
                sec = secrets_store.read_profile_secrets(name) or {}
                secret_count = (
                    sum(len(f) for f in (sec.get("connections") or {}).values())
                    + len(sec.get("harness") or {}))
            except ValueError:
                out.append({"name": name, "broken": True})
                continue
        out.append({
            "name": name,
            "created_at": doc.get("created_at"),
            "devcake_tag": doc.get("devcake_tag", ""),
            "sections": doc.get("sections") or [],
            "counts": {
                "pmos": len(app.get("pmos") or []),
                "repos": len(app.get("repos") or []),
                "dev_types": len(cfg.get("dev_types") or {}),
                "prompt_templates": sum(
                    len(v) for v in (cfg.get("prompt_templates") or {}).values()),
                "secrets": secret_count,
            },
        })
    return out


# ── last-applied breadcrumb + divergence ─────────────────────────────────────

def _read_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001 — corrupt last-applied breadcrumb reads as empty; it is an advisory divergence hint only
        return {}


def _write_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))


def record_applied(name: str) -> None:
    state = _read_state()
    state["last_applied"] = {"name": name, "at": _utcnow()}
    _write_state(state)


def last_applied() -> dict | None:
    return _read_state().get("last_applied")


def diverged_since(bundle: dict, applied_at: str, config: AppConfig,
                   dev_types: dict[str, DevType]) -> bool:
    """Honest divergence: has the live world changed since this profile was
    applied? Non-secret sections compare as dicts; secrets compare by NAME
    SET and updated_at timestamps — never by value (ADR-0011)."""
    if bundle.get("config") is not None:
        cur = serialize_current(config, dev_types,
                                include_config=True, include_secrets=False)
        if cur.get("config") != bundle.get("config"):
            return True
    sec = bundle.get("secrets")
    if sec is not None:
        live = {(k, f) for k, fs in
                secrets_store.list_connection_secrets().items() for f in fs}
        snap = {(k, f) for k, fs in
                (sec.get("connections") or {}).items() for f in fs}
        if live != snap:
            return True
        live_h = set(secrets_store.list_harness_secrets())
        if live_h != set(sec.get("harness") or {}):
            return True
        for key, fields in (sec.get("connections") or {}).items():
            scope, _, instance = key.partition("-")
            for field in fields:
                st = secrets_store.connection_status(scope, instance, field)
                if st["updated_at"] and st["updated_at"] > applied_at:
                    return True
        for var in sec.get("harness") or {}:
            st = secrets_store.harness_status(var)
            if st["updated_at"] and st["updated_at"] > applied_at:
                return True
    return False
