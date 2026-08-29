"""Internal-forge repos + skill store application service (M11, docs/16
skill store v1; ADR-0015 Decision 3). main.py keeps thin forwards that pass
the composition root's singletons at call time."""

from __future__ import annotations

import os
import re

from fastapi import HTTPException

from ..config import _INSTANCE_NAME_RE
from ..ports.internal_forge import ACTIVITY_PREFIX
from ..domain.skills import SkillStoreError


async def list_internal_repos(*, internal_forge):
    """Read-only admin surface (M11, founder decision): the auto-created
    internal-forge repos. Empty list when the internal forge is disabled."""
    if internal_forge is None:
        return {"repos": [], "ui_url": None}
    try:
        repos = await internal_forge.list_repos()
    except Exception as e:  # noqa: BLE001 — upstream-error contract: any forge failure surfaces as 502 with detail, never a raw 500
        raise HTTPException(502, f"internal forge unreachable: {str(e)[:200]}")
    return {"repos": [r.model_dump() for r in repos],
            "ui_url": os.environ.get("GITEA_UI_URL", "http://localhost:3300")}


async def create_internal_repo(body: dict, *, internal_forge):
    """Operator repo on the bundled Gitea (item 4 / PLAN_MEMORY §8).
    Never `activity-*`. Empty notebook — no README, no `.claims/`
    (the conveyor creates `.claims/` on first harvest)."""
    if internal_forge is None:
        raise HTTPException(503, "internal forge is disabled "
                                 "(GITEA_ADMIN_PASSWORD unset)")
    name = str(body.get("name") or "")
    if name.startswith(ACTIVITY_PREFIX) or name.startswith("activity-"):
        raise HTTPException(422, "name must not start with activity- "
                                 "(those repos are swept on Clear)")
    if not re.fullmatch(_INSTANCE_NAME_RE, name):
        raise HTTPException(422, f"name must match {_INSTANCE_NAME_RE} "
                                 f"(it doubles as the repo card name)")
    try:
        return await internal_forge.create_operator_repo(name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:  # noqa: BLE001 — upstream-error contract: any forge failure surfaces as 502 with detail, never a raw 500
        raise HTTPException(502, f"internal forge: {str(e)[:200]}")


async def list_skills(*, skill_service, config=None, dev_types=None):
    """Skill store catalog (v1): store-listed when the internal forge is up,
    bundled fallback otherwise — `store` tells the UI which it is (and where
    to edit). External `<source>/<skill>` rows (ADR-0016 addendum 2 /
    CAKE-146) join for every configured skill_sources entry whose mirror
    has a readable tree — read-only, origin-tagged. `dev_types` is retained
    for call-site compatibility but no longer gates catalog discovery."""
    _ = dev_types  # catalog is config-driven (CAKE-146); selection stays on Dev Types
    skills, store_status = await skill_service.list_skills()
    sources = [s.name for s in (getattr(config, "skill_sources", None) or [])]
    if sources:
        skills = skills + await skill_service.external_infos(sources)
    return {"skills": [s.model_dump() for s in skills], "store": store_status}


async def get_skill(name: str, *, skill_service):
    """Full skill content for the admin View dialog (store-first, bundled
    fallback). 404 unknown, 422 bad name."""
    try:
        return await skill_service.get_skill(name)
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))


async def create_skill(body: dict, *, skill_service):
    """'Add skill' form (docs/11): name + trigger description + markdown
    body. Frontmatter is generated app-side — the operator never touches
    YAML. 409 on collision unless overwrite is set."""
    name = str(body.get("name") or "").strip()
    description = str(body.get("description") or "").strip()
    md = str(body.get("body") or "").strip()
    if not (name and description and md):
        raise HTTPException(422, "name, description and instructions are "
                                 "all required")
    try:
        await skill_service.save_skill(
            name, skill_service.compose_skill(name, description, md),
            overwrite=bool(body.get("overwrite")))
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True, "name": name}


async def import_skill(body: dict, *, skill_service):
    """Import an uploaded skill: files = [{path, content_b64}] relative to
    the skill dir, one of them SKILL.md — the name comes from its
    frontmatter. 409 on collision unless overwrite is set."""
    files = body.get("files") or []
    try:
        name = skill_service.validate_import(files)
        await skill_service.save_skill(name, files,
                                       overwrite=bool(body.get("overwrite")))
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True, "name": name}


async def delete_skill_endpoint(name: str, *, skill_service):
    """Remove an operator skill (built-ins refuse — they re-seed at boot)."""
    try:
        await skill_service.delete_skill(name)
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True}


async def ensure_skill_sources_fresh(*, repo_cache,
                                     config) -> tuple[bool, dict[str, str]]:
    """ONE ensure_fresh chokepoint for skill-source mirrors (CAKE-146):
    every configured skill_sources name. Shared by Update now and
    Restore built-ins. Best-effort — never raises; returns ensure_fresh's
    (all_ok, {name: reason}) so callers can report failures honestly
    (they also land in the mirror ledger)."""
    if repo_cache is None:
        return True, {}
    names = [s.name for s in (getattr(config, "skill_sources", None) or [])]
    if not names:
        return True, {}
    return await repo_cache.ensure_fresh(names)


async def refresh_skill_sources(*, repo_cache, config):
    """Operator 'Update now': refresh every configured skill-source mirror
    without re-seeding built-ins. Catalog is a read of the mirror — SPA
    reloads via GET /skills after this returns. `failures` names each
    source whose fetch failed (empty when all synced) — the SPA must not
    show a green ✓ over a failed fetch."""
    ok, failures = await ensure_skill_sources_fresh(
        repo_cache=repo_cache, config=config)
    return {"ok": ok, "failures": failures}


async def sync_skills(*, internal_forge, skill_service, repo_cache=None,
                      config=None, dev_types=None):
    """Re-seed missing built-in skills without a restart — heals a first
    boot where Gitea came up after the app, and re-seeds after upgrades.
    Never overwrites operator edits (missing paths only). Also
    ensure_fresh ALL configured skill sources via the shared chokepoint
    (CAKE-146) — best-effort, failures land in the mirror ledger/health,
    never a refused sync. `dev_types` retained for call-site compatibility."""
    _ = dev_types
    await ensure_skill_sources_fresh(repo_cache=repo_cache, config=config)
    if internal_forge is None:
        raise HTTPException(503, "internal forge is disabled "
                                 "(GITEA_ADMIN_PASSWORD unset)")
    try:
        await internal_forge.ensure_skill_store(skill_service.builtin_seed())
    except Exception as e:  # noqa: BLE001 — upstream-error contract: any forge failure surfaces as 502 with detail, never a raw 500
        raise HTTPException(502, f"internal forge: {str(e)[:200]}")
    return {"ok": True}


async def delete_internal_repo(name: str, *, internal_forge, store,
                               forge_runtime):
    """Manual Clear (founder decision: retain-by-default, delete-on-demand).
    Refuses while a live run exists for the mission — its Dev still needs
    the repo. Deletes repo + machine user (revoking both tokens) + secret."""
    if internal_forge is None:
        raise HTTPException(404, "internal forge is not enabled")
    if any(r.repo_ref == name and r.state in ("dispatched", "running", "finalizing")
           for r in store.active()):
        raise HTTPException(409, "a live run is using this repo — wait for it "
                                 "to finish before clearing")
    try:
        await internal_forge.delete_repo(name)
    except Exception as e:  # noqa: BLE001 — upstream-error contract: any forge failure surfaces as 502 with detail, never a raw 500
        raise HTTPException(502, f"delete failed: {str(e)[:200]}")
    forge_runtime.unregister(name)
    return {"deleted": name}
