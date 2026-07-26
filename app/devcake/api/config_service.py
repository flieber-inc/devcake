"""Config PUT application service (docs/11 §1, ADR-0015 Decision 3).

The config OBJECT is shared by identity across managers and runtimes — the
patch is applied via per-field setattr, never by rebinding (a rebound config
would orphan every holder of the old object).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from .. import secrets as secrets_store
from ..config import AppConfig, deep_merge, reject_stale_patch, save_config
from ..prompts import templates as prompt_templates
from ..settings_bundle import (BundleError, dry_run_adapters,
                               validate_config_semantics)

log = logging.getLogger("devcake")


async def apply_config_patch(body: dict, *, config, dev_types, managers,
                             reload) -> dict:
    """Validate + apply a config PUT in place; hot-reload adapters; restore
    the previous config if the reload fails. `reload` is the composition
    root's reload_connections."""
    try:
        reject_stale_patch(body)
        merged = AppConfig.model_validate(deep_merge(config.model_dump(), body))
    except Exception as e:  # noqa: BLE001 — validation contract: whatever the merge/model raises on a bad patch surfaces as 422, never a 500
        raise HTTPException(422, str(e))
    # cross-store semantics + dry-run adapter construction (ISSUES #11) live
    # in settings_bundle — ONE implementation shared with bundle apply
    # (ADR-0013); the PUT resolves templates against disk
    try:
        validate_config_semantics(
            merged, set(dev_types),
            template_exists=lambda mt, name:
                not prompt_templates.resolve_playbook(mt, name)[1])
        dry_run_adapters(merged)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    previous = config.model_dump()
    # a removed instance's stored secrets go with it — otherwise a later
    # instance reusing the name silently inherits the dead credential
    removed = [("pmo", p["name"]) for p in previous["pmos"]
               if p["name"] not in {i.name for i in merged.pmos}]
    removed += [("repo", r["name"]) for r in previous["repos"]
                if r["name"] not in {i.name for i in merged.repos}]
    for field in type(merged).model_fields:
        setattr(config, field, getattr(merged, field))
    save_config(config)
    try:
        reload()                                 # hot reload pmo + forge
    except Exception as e:  # noqa: BLE001 — rollback contract: ANY reload failure restores the previous config; the 500 carries the cause
        log.exception("reload_connections failed — restoring previous config")
        restored = AppConfig.model_validate(previous)
        for field in type(restored).model_fields:
            setattr(config, field, getattr(restored, field))
        save_config(config)
        try:
            reload()
        except Exception:
            log.exception("restore reload also failed")
        raise HTTPException(500, f"config reload failed; previous config restored: {e}")
    for scope, name in removed:                  # only once the new config took
        try:
            secrets_store.delete_connection_instance(scope, name)
        except Exception:  # noqa: BLE001 — cleanup is best-effort: the config change is APPLIED; a failure must not 500 it (audit A21); orphan named in the log
            log.exception("could not delete stored secrets of removed "
                          "%s instance %r", scope, name)
    if not previous["auto_merge"] and config.auto_merge:
        # auto_merge flipped OFF→ON (founder request 2026-07-15): re-arm the
        # deferred-merge window for missions already parked at DEVCAKE-MERGE —
        # the next sweep posts a fresh window entry and drives their merges
        for mgr in managers.values():
            mgr.rearm_merge_windows = True
        log.info("auto_merge flipped ON — parked DEVCAKE-MERGE missions "
                 "re-armed for the deferred-merge sweep")
    return config.model_dump()


def set_pmo_intake(*, name: str, paused: bool, config) -> dict:
    """Flip one PMO instance's intake switch in place (docs/11).

    Narrow write path: mutates the existing PMOInstance object (managers hold
    the same identity after build_managers) and persists. Does NOT replace the
    pmos list, so no deep_merge wholesale-list race and no secret-deletion
    side effect from apply_config_patch's removed-instance cleanup.
    """
    if not isinstance(paused, bool):
        raise HTTPException(422, "paused must be a boolean")
    for inst in config.pmos:
        if inst.name == name:
            inst.intake_paused = paused
            save_config(config)
            return {"name": name, "intake_paused": paused}
    raise HTTPException(404, f"no PMO instance named {name!r}")
