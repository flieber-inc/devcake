"""Config PUT application service (docs/11 §1, ADR-0015 Decision 3).

The config OBJECT is shared by identity across managers and runtimes — the
patch is applied via per-field setattr, never by rebinding (a rebound config
would orphan every holder of the old object).
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import HTTPException

from .. import secrets as secrets_store
from ..config import (AppConfig, apply_auto_merge_rearm, deep_merge,
                      reconcile_managed_pmos, reconcile_reserved_crons,
                      reject_stale_patch, save_config)
from ..prompts import templates as prompt_templates
from ..settings_bundle import (BundleError, dry_run_adapters,
                               validate_config_semantics)

log = logging.getLogger("devcake")


def inherit_pmo_intake(body: dict, current: dict) -> dict:
    """Preserve per-PMO intake owned by PUT /config/pmos/{name}/intake.

    `pmos` is replaced wholesale on a general config PUT. A draft Save that
    omits `intake_paused` (or never knew the live value) would otherwise re-
    default every instance to False and undo a pause. Incoming entries that
    omit the key inherit the live value by instance name; an explicit key
    still wins (profile apply, intentional API clients). New names keep the
    model default when the key is absent.
    """
    if not isinstance(body.get("pmos"), list):
        return body
    live = {p["name"]: bool(p.get("intake_paused"))
            for p in (current.get("pmos") or [])
            if isinstance(p, dict) and p.get("name")}
    out = dict(body)
    rows = []
    for p in body["pmos"]:
        if not isinstance(p, dict):
            rows.append(p)
            continue
        row = dict(p)
        name = row.get("name")
        if "intake_paused" not in row and name in live:
            row["intake_paused"] = live[name]
        rows.append(row)
    out["pmos"] = rows
    return out


async def apply_config_patch(body: dict, *, config, dev_types, managers,
                             reload, repo_cache=None,
                             cycle_lock: asyncio.Lock | None = None) -> dict:
    """Serialize the config transaction against an in-flight poll cycle."""
    if cycle_lock is None:
        return _apply_config_patch(
            body, config=config, dev_types=dev_types, managers=managers,
            reload=reload, repo_cache=repo_cache)
    async with cycle_lock:
        return _apply_config_patch(
            body, config=config, dev_types=dev_types, managers=managers,
            reload=reload, repo_cache=repo_cache)


def _apply_config_patch(body: dict, *, config, dev_types, managers,
                        reload, repo_cache=None) -> dict:
    """Validate + apply a config PUT in place; hot-reload adapters; restore
    the previous config if the reload fails. `reload` is the composition
    root's reload_connections."""
    try:
        reject_stale_patch(body)
        # inherit before merge — deep_merge replaces the list wholesale, so
        # defaults would land if we waited until after model_validate
        current = config.model_dump()
        body = inherit_pmo_intake(body, current)
        # ADR-0030: managed rows survive the wholesale list replace — and
        # this MUST precede the removed-instance computation below, or a PUT
        # omitting the board row would delete pmo-board.json with it
        if isinstance(body.get("pmos"), list):
            body = {**body, "pmos": reconcile_managed_pmos(
                current.get("pmos") or [], body["pmos"],
                internal_forge_present=bool(
                    os.environ.get("GITEA_ADMIN_PASSWORD")))}
        if isinstance(body.get("crons"), list):
            body = {**body, "crons": reconcile_reserved_crons(
                current.get("crons") or [], body["crons"])}
        merged = AppConfig.model_validate(deep_merge(current, body))
    except Exception as e:  # noqa: BLE001 — validation contract: whatever the merge/model raises on a bad patch surfaces as 422, never a 500
        raise HTTPException(422, str(e))
    # cross-store semantics + dry-run adapter construction live in
    # settings_bundle — ONE implementation shared with bundle apply
    # (ADR-0013); the PUT resolves templates against disk
    try:
        validate_config_semantics(
            merged, set(dev_types),
            template_exists=lambda mt, name:
                not prompt_templates.resolve_playbook(mt, name)[1],
            dev_types=dev_types)
        dry_run_adapters(merged)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    previous = config.model_dump()
    # In-place skill-source rename (SPA edits name on the same card index):
    # plan the move now; apply it only after reload succeeds so a rolled-back
    # PUT does not leave tokens under the new name while config reverts.
    # Index inequality alone is NOT a rename — SPA Remove uses filter() and
    # shifts later cards up; treat as rename only when old left the name set
    # and new is genuinely new (else set-difference delete handles remove).
    prev_skills = previous.get("skill_sources") or []
    new_skills = list(merged.skill_sources)
    old_names = {
        (old["name"] if isinstance(old, dict) else old.name)
        for old in prev_skills}
    new_names = {s.name for s in new_skills}
    skill_renames: list[tuple[str, str]] = []
    renamed_from: set[str] = set()
    for i, old in enumerate(prev_skills):
        if i >= len(new_skills):
            break
        old_name = old["name"] if isinstance(old, dict) else old.name
        new_name = new_skills[i].name
        if old_name == new_name:
            continue
        if old_name not in new_names and new_name not in old_names:
            skill_renames.append((old_name, new_name))
            renamed_from.add(old_name)
    # a removed instance's stored secrets go with it — otherwise a later
    # instance reusing the name silently inherits the dead credential
    prev_keys = set(secrets_store.connection_instances(previous))
    new_keys = set(secrets_store.connection_instances(merged))
    removed = sorted(
        (scope, name) for scope, name in (prev_keys - new_keys)
        if not (scope == "skill" and name in renamed_from))
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
    for old_name, new_name in skill_renames:
        try:
            secrets_store.rename_connection_instance(
                "skill", old_name, new_name)
        except Exception:  # noqa: BLE001 — best-effort: config change is APPLIED; orphan named in the log
            log.exception("could not rename skill-source secrets %r → %r",
                          old_name, new_name)
    for scope, name in removed:                  # only once the new config took
        try:
            secrets_store.delete_connection_instance(scope, name)
        except Exception:  # noqa: BLE001 — cleanup is best-effort: the config change is APPLIED; a failure must not 500 it (audit A21); orphan named in the log
            log.exception("could not delete stored secrets of removed "
                          "%s instance %r", scope, name)
        if scope == "repo" and repo_cache is not None:
            # ADR-0024: the removed card's mirror goes with it (same
            # best-effort contract as the secret deletion above)
            try:
                repo_cache.delete_mirror(name)
            except Exception:  # noqa: BLE001 — cleanup only; the config change is APPLIED
                log.exception("could not delete mirror of removed repo %r",
                              name)
    # Per-repo auto_merge OFF→ON (founder request 2026-07-15, ADR-0020):
    # re-arm the deferred-merge window only for missions whose work repo
    # flipped — the next sweep posts a fresh window entry for those.
    apply_auto_merge_rearm(previous.get("repos") or [], config.repos, managers)
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
