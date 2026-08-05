"""Config-profiles application service (ADR-0013, ADR-0015 Decision 3):
named snapshots of settings + secrets; apply is THE one world-swap path."""

from __future__ import annotations

from fastapi import HTTPException

from .. import profiles as profiles_store
from ..settings_bundle import (BundleError, apply_bundle, audit_event,
                               diff_bundle, serialize_current)


def require_no_active_runs(store, action: str) -> None:
    """World-swaps are blocked while runs are in flight (founder decision) —
    the internal-repo delete guard pattern, applied to whole-settings
    replacement."""
    n = len(store.active())
    if n:
        raise HTTPException(
            409, f"{n} run(s) active — wait for them to finish or clear "
                 f"runs before {action}")


def snapshot_warnings(bundle: dict) -> list[str]:
    """A snapshot silently missing credentials its config expects is a trap
    at apply time — name the gaps at save time instead."""
    warns = []
    sec = bundle.get("secrets") or {}
    conns = sec.get("connections") or {}
    cfg = bundle.get("config") or {}
    for p in (cfg.get("app") or {}).get("pmos") or []:
        if p.get("team_key") and not (conns.get(f"pmo-{p['name']}") or {}).get("api_key"):
            warns.append(f"PMO {p['name']!r} is configured but has no stored "
                         "API key — the snapshot carries none")
    for r in (cfg.get("app") or {}).get("repos") or []:
        stored = conns.get(f"repo-{r['name']}") or {}
        if r.get("url") and not (stored.get("token") or stored.get("token_ro")):
            warns.append(f"repo {r['name']!r} is configured but has no stored "
                         "token — the snapshot carries none")
    return warns


def list_profiles_rows(config, dev_types) -> dict:
    """Profile rows for the admin table — counts and presence only. The
    last-applied breadcrumb + divergence boolean ride the matching row
    (dict compare + secret timestamps, never values — ADR-0011)."""
    rows = profiles_store.list_profiles()
    la = profiles_store.last_applied()
    for row in rows:
        row["last_applied_at"] = None
        row["diverged"] = None
        if la and la.get("name") == row["name"] and not row.get("broken"):
            row["last_applied_at"] = la.get("at")
            try:
                bundle = profiles_store.read_profile(row["name"])
                row["diverged"] = profiles_store.diverged_since(
                    bundle, la.get("at") or "", config, dev_types)
            except BundleError:
                row["diverged"] = None
    return {"profiles": rows}


def get_profile_payload(name: str, config, dev_types) -> dict:
    """Full section A + a secrets PRESENCE map + the apply-preview diff.
    Secret values never leave this endpoint."""
    try:
        bundle = profiles_store.read_profile(name)
        diff = diff_bundle(bundle, config, dev_types)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    presence = None
    if "secrets" in (bundle.get("sections") or []):
        sec = bundle.get("secrets") or {}
        presence = {"connections": {k: sorted(f) for k, f in
                                    (sec.get("connections") or {}).items()},
                    "harness": sorted(sec.get("harness") or {})}
    return {"name": bundle.get("name", name),
            "created_at": bundle.get("created_at"),
            "devcake_tag": bundle.get("devcake_tag", ""),
            "sections": bundle.get("sections") or [],
            "config": bundle.get("config"),
            "secrets_present": presence,
            "diff": diff}


def save_profile_body(body: dict, config, dev_types) -> dict:
    """Save-current-as: snapshot the live settings (A) + secret values (B)
    under a name. 409 on collision unless overwrite — the UI chains an
    explicit overwrite confirm."""
    name = str(body.get("name") or "")
    bundle = serialize_current(config, dev_types,
                               include_config=True, include_secrets=True)
    try:
        profiles_store.save_profile(name, bundle,
                                    overwrite=bool(body.get("overwrite")))
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    warnings = snapshot_warnings(bundle)
    sec = bundle.get("secrets") or {}
    audit_event("profile_saved",
                f"name={name} secrets="
                f"{sum(len(f) for f in (sec.get('connections') or {}).values()) + len(sec.get('harness') or {})}")
    return {"saved": True, "name": name, "warnings": warnings}


def apply_profile_body(name: str, *, config, dev_types, store, reload,
                       shared_breakers, forge_breakers, managers=None) -> dict:
    """THE world-swap: replaces the sections the profile contains (a profile
    without secrets keeps the live ones). Blocked while runs are active;
    rollback-by-reapply on reload failure (settings_bundle)."""
    require_no_active_runs(store, "applying a profile")
    try:
        bundle = profiles_store.read_profile(name)
        result = apply_bundle(bundle, config=config, dev_types=dev_types,
                              reload=reload, managers=managers)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    if "secrets" in result["applied"]:
        # fresh credentials clear latched auth state, same as the individual
        # secret PUT endpoints do
        shared_breakers.clear()
        forge_breakers.clear()
    profiles_store.record_applied(name)
    audit_event("profile_applied",
                f"name={name} sections={'+'.join(result['applied'])}")
    return {"profile": name, **result}


def rename_profile_body(name: str, body: dict) -> dict:
    new = str(body.get("new_name") or "")
    try:
        profiles_store.rename_profile(name, new)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    audit_event("profile_renamed", f"{name} -> {new}")
    return {"renamed": True, "name": new}


def delete_profile_body(name: str) -> dict:
    try:
        profiles_store.delete_profile(name)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    audit_event("profile_deleted", f"name={name}")
    return {"deleted": name}
