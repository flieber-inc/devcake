"""Dev Types / prompt templates / assignments application service
(ADR-0015 Decision 3): per-Mission-Type and per-Dev-Type prompt template
CRUD, the harness registry, Dev Type CRUD + rename + credential upload, and
mission-type assignments. main.py keeps thin forwards that pass the
composition root's singletons at call time."""

from __future__ import annotations

import logging
import os
import re

from fastapi import HTTPException

from ..config import (Assignment, DEFAULT_ASSIGNMENTS, DevType,
                      delete_dev_type, save_config, save_dev_type)
from ..harness import HARNESSES, dev_type_status
from ..prompts import templates as prompt_templates

log = logging.getLogger("devcake")


async def get_prompt_templates(*, config, dev_types):
    """Every stored template per mission type (the built-in default first),
    the per-type variable allowlists (drives the SPA's hint chips), and the
    active selection."""
    from ..prompts import PLAYBOOK_VARS
    return {
        "variables": {mt: list(v) for mt, v in PLAYBOOK_VARS.items()},
        "templates": prompt_templates.list_templates(),
        "active": {mt: config.active_prompt_templates.get(mt, "Development")
                   for mt in PLAYBOOK_VARS},
        # dev-type identifying-prompt templates (2026-07-15): same workflow
        # names, per Dev Type; all editable (Development is seeded user data)
        "dev_types": prompt_templates.list_devtype_prompts(dev_types),
        "active_dev": {n: config.active_devtype_prompts.get(n, "Development")
                       for n in dev_types},
    }


async def put_prompt_template(mission_type: str, name: str, body: dict):
    text = body.get("template")
    if not isinstance(text, str):
        raise HTTPException(422, "body must carry a string 'template'")
    try:
        prompt_templates.save_template(mission_type, name, text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"mission_type": mission_type, "name": name, "saved": True}


async def delete_prompt_template(mission_type: str, name: str, *, config):
    if config.active_prompt_templates.get(mission_type) == name:
        raise HTTPException(
            409, f"template {name!r} is the ACTIVE template for "
                 f"{mission_type} — switch back to 'default' first")
    try:
        prompt_templates.delete_template(mission_type, name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"deleted": True}


async def put_devtype_prompt(dev_type: str, name: str, body: dict, *,
                             dev_types):
    if dev_type not in dev_types:
        raise HTTPException(404, f"no Dev Type named {dev_type!r}")
    text = body.get("template")
    if not isinstance(text, str):
        raise HTTPException(422, "body must carry a string 'template'")
    try:
        prompt_templates.save_devtype_prompt(dev_type, name, text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"dev_type": dev_type, "name": name, "saved": True}


async def delete_devtype_prompt(dev_type: str, name: str, *, config):
    active = config.active_devtype_prompts.get(dev_type, "Development")
    if active == name or (name == "Development" and active in ("Development",)):
        raise HTTPException(409, f"template {name!r} is ACTIVE for "
                                 f"{dev_type} — switch first")
    try:
        prompt_templates.delete_devtype_prompt(dev_type, name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"deleted": True}


async def list_harnesses():
    """The harness registry — image + credential requirements per
    harness_template. Read-only; the admin Dev Type card derives its display
    (including previews of unsaved harness switches) from this."""
    return {name: {"docker_image": h.image,
                   "default_model": h.default_model,
                   "credential_env": h.credential_env,
                   "credential_files": [cf.model_dump() for cf in h.credential_files],
                   "oauth_available": h.oauth is not None,
                   "skills_dir": h.skills_dir}
            for name, h in HARNESSES.items()}


async def list_dev_types(*, dev_types):
    return [dev_type_status(d) for d in dev_types.values()]


async def upsert_dev_type(body: dict, name: str | None = None, *, dev_types):
    try:
        dt = DevType.model_validate(body if name is None else {**body, "name": name})
    except Exception as e:  # noqa: BLE001 — validation contract: whatever model_validate raises on a bad body surfaces as 422, never a 500
        raise HTTPException(422, str(e))
    dev_types[dt.name] = dt
    save_dev_type(dt)
    prompt_templates.seed_devtype_prompts({dt.name: dt})
    return dt.model_dump()


async def rename_dev_type(name: str, body: dict, *, config, dev_types,
                          shared_breakers):
    """Rename a Dev Type in place (2026-07-15): moves its YAML, credential
    dir, and prompt-template dir, and remaps every reference (assignments,
    mapper, active prompt selection, breaker)."""
    import shutil
    from pathlib import Path as _P
    new = str(body.get("new_name") or "")
    if name not in dev_types:
        raise HTTPException(404, f"no Dev Type named {name!r}")
    if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", new) or ":" in new:
        raise HTTPException(422, "new_name must match ^[A-Za-z0-9][A-Za-z0-9_-]*$")
    if new in dev_types:
        raise HTTPException(409, f"a Dev Type named {new!r} already exists")
    dt = dev_types.pop(name).model_copy(update={"name": new})
    dev_types[new] = dt
    save_dev_type(dt)
    delete_dev_type(name)
    data = _P(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
    for sub in ("secrets", "config/devtype_prompt_templates"):
        src = data / sub / name
        if src.is_dir():
            shutil.move(str(src), str(data / sub / new))
    changed = False
    for mt, a in config.assignments.items():
        if a.dev_type == name:
            a.dev_type = new
            changed = True
    for inst in config.pmos:                      # ADR-0019 override maps
        for a in inst.assignments.values():
            if a.dev_type == name:
                a.dev_type = new
                changed = True
    if config.relations_mapper.dev_type == name:
        config.relations_mapper.dev_type = new
        changed = True
    if name in config.active_devtype_prompts:
        config.active_devtype_prompts[new] = config.active_devtype_prompts.pop(name)
        changed = True
    if changed or True:
        save_config(config)
    if name in shared_breakers:
        shared_breakers[new] = shared_breakers.pop(name)
    return {"renamed": True, "name": new}


async def remove_dev_type(name: str, *, config, dev_types):
    """Delete a Dev Type and every config/file reference that would otherwise
    poison later saves (active_devtype_prompts deep_merge ghosts, leftover
    prompt-template and credential dirs). Mirrors rename_dev_type's
    reference hygiene; DELETE still refuses while assigned / mapper-bound."""
    import shutil
    from pathlib import Path as _P

    if name not in dev_types:
        raise HTTPException(404, f"no Dev Type named {name!r}")
    if any(a.dev_type == name for a in config.assignments.values()):
        raise HTTPException(409, f"{name} is assigned to a mission type")
    holders = sorted(p.name for p in config.pmos
                     if any(a.dev_type == name
                            for a in p.assignments.values()))
    if holders:                                   # ADR-0019 override maps
        raise HTTPException(
            409, f"{name} is assigned on PMO instance(s) "
                 f"{', '.join(holders)} — remove the override(s) first")
    if config.relations_mapper.dev_type == name:
        raise HTTPException(409, f"{name} is the Relations Mapper's Dev Type — "
                                 "repoint or disable the mapper first")
    dev_types.pop(name, None)
    delete_dev_type(name)
    data = _P(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
    for sub in ("secrets", "config/devtype_prompt_templates"):
        target = data / sub / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    if name in config.active_devtype_prompts:
        config.active_devtype_prompts.pop(name, None)
        save_config(config)
    return {"deleted": name}


async def upload_credentials(name: str, body: dict, *, dev_types,
                             shared_breakers):
    """{"filename": "...", "content": "..."} → /data/secrets/{name}/ (0600).

    Path components validated via secrets.require_credential_ref; size capped
    at secrets.MAX_CREDENTIAL_FILE_BYTES; write is atomic (docs/14 §11)."""
    if name not in dev_types:
        raise HTTPException(404)
    from .. import secrets as secrets_store
    fname = body.get("filename") or "creds.json"
    content = body.get("content") or ""
    try:
        secrets_store.require_credential_ref(name, fname)
        secrets_store.write_credential_file(name, fname, content)
    except ValueError as e:
        raise HTTPException(422, str(e))
    shared_breakers.pop(name, None)   # fresh credential clears the breaker
    return {"stored": f"{name}/{fname}"}


async def get_assignments(*, config):
    return {k: v.model_dump() for k, v in config.assignments.items()}


async def put_assignments(body: dict, *, config, dev_types):
    try:
        new = {k: Assignment.model_validate(v) for k, v in body.items()}
    except Exception as e:  # noqa: BLE001 — validation contract: whatever model_validate raises on a bad body surfaces as 422, never a 500
        raise HTTPException(422, str(e))
    missing = set(DEFAULT_ASSIGNMENTS) - set(new)
    if missing:
        raise HTTPException(422, f"unassigned mission types: {sorted(missing)}")
    unknown = {a.dev_type for a in new.values()} - set(dev_types)
    if unknown:
        raise HTTPException(422, f"unknown dev types: {sorted(unknown)}")
    # Independent review is default config, not a hard invariant (ISSUES #19)
    warnings: list[str] = []
    ex = new.get("EXECUTE")
    rev = new.get("REVIEW")
    if ex and rev and ex.dev_type == rev.dev_type:
        msg = (f"EXECUTE and REVIEW share Dev Type {ex.dev_type!r} — "
               "independent AI review is not enforced")
        log.warning(msg)
        warnings.append(msg)
    config.assignments = new
    save_config(config)
    # warnings ride in their own field — mixing them into the mission-type
    # mapping handed clients a phantom "_warnings" mission type
    return {"assignments": {k: v.model_dump()
                            for k, v in config.assignments.items()},
            "warnings": warnings}
