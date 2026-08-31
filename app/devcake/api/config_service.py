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


# Scopes whose cards own a name-keyed ADR-0024 mirror — the rename and
# removal cleanups below must treat them identically or one path orphans
# what the other migrates.
_MIRRORED_SCOPES = ("repo", "skill")


def _item_name(item) -> str:
    return item["name"] if isinstance(item, dict) else item.name


def _item_field(item, key: str) -> str:
    v = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
    return str(v or "").strip()


def repo_rename_anchor(item) -> str:
    """The identity a repo/skill-source rename never changes: its URL."""
    return _item_field(item, "url").rstrip("/").removesuffix(".git").lower()


def pmo_rename_anchor(item) -> str:
    """The identity a PMO rename never changes: system + team + api_base.
    Empty ("") for an idle card (no team) — pass-2 pairing skips those."""
    team = _item_field(item, "team_key")
    if not team:
        return ""
    return "|".join((_item_field(item, "system") or "linear", team,
                     _item_field(item, "api_base")))


def plan_list_renames(prev_items, new_items, *,
                      anchor=None) -> list[tuple[str, str]]:
    """Detect in-place card renames on a replaced config list.

    Same list index, old name left the name set, new name is genuinely new.
    Index inequality alone is NOT a rename — SPA Remove uses filter() and
    shifts later cards up; set-difference delete handles those removals.

    `anchor` (when given) is the identity a rename never changes — a repo's
    URL, a PMO's system/team. Index pairing then also requires the anchors
    to agree: without that guard, removing one card and renaming a LATER
    card in the same Save pairs the removed card's name with the renamed
    card's new name, moving the removed card's secrets onto the survivor
    and deleting the survivor's own. A second pass pairs leftover rows by
    unique non-empty anchor, so remove+rename in one Save still renames
    correctly. A rename that ALSO changes the anchor in the same Save is
    deliberately treated as remove+add (a card pointing somewhere new is a
    new identity — its secrets do not follow)."""
    old_names = {_item_name(old) for old in prev_items}
    new_names = {_item_name(new) for new in new_items}
    renames: list[tuple[str, str]] = []
    matched_old: set[str] = set()
    matched_new: set[str] = set()
    for i, old in enumerate(prev_items):
        if i >= len(new_items):
            break
        old_name = _item_name(old)
        new_name = _item_name(new_items[i])
        if old_name == new_name:
            continue
        if old_name in new_names or new_name in old_names:
            continue
        if anchor is not None and anchor(old) != anchor(new_items[i]):
            continue
        renames.append((old_name, new_name))
        matched_old.add(old_name)
        matched_new.add(new_name)
    if anchor is not None:
        by_anchor_old: dict[str, list[str]] = {}
        for old in prev_items:
            name = _item_name(old)
            if name in new_names or name in matched_old:
                continue
            a = anchor(old)
            if a:
                by_anchor_old.setdefault(a, []).append(name)
        by_anchor_new: dict[str, list[str]] = {}
        for new in new_items:
            name = _item_name(new)
            if name in old_names or name in matched_new:
                continue
            a = anchor(new)
            if a:
                by_anchor_new.setdefault(a, []).append(name)
        for a, olds in by_anchor_old.items():
            news = by_anchor_new.get(a) or []
            if len(olds) == 1 and len(news) == 1:
                renames.append((olds[0], news[0]))
    return renames


def inherit_pmo_intake(body: dict, current: dict,
                       *, renames: list[tuple[str, str]] | None = None
                       ) -> dict:
    """Preserve per-PMO intake owned by PUT /config/pmos/{name}/intake.

    `pmos` is replaced wholesale on a general config PUT. A draft Save that
    omits `intake_paused` (or never knew the live value) would otherwise re-
    default every instance to False and undo a pause. Incoming entries that
    omit the key inherit the live value by instance name; an explicit key
    still wins (profile apply, intentional API clients). New names keep the
    model default when the key is absent — except an in-place rename, which
    inherits from the prior name at the same card index (SPA strips the key).
    """
    if not isinstance(body.get("pmos"), list):
        return body
    live = {p["name"]: bool(p.get("intake_paused"))
            for p in (current.get("pmos") or [])
            if isinstance(p, dict) and p.get("name")}
    from_old = {new: old for old, new in (renames or [])}
    out = dict(body)
    rows = []
    for p in body["pmos"]:
        if not isinstance(p, dict):
            rows.append(p)
            continue
        row = dict(p)
        name = row.get("name")
        if "intake_paused" not in row:
            src = name if name in live else from_old.get(name)
            if src in live:
                row["intake_paused"] = live[src]
        rows.append(row)
    out["pmos"] = rows
    return out


async def apply_config_patch(body: dict, *, config, dev_types, managers,
                             reload, repo_cache=None, rekey_pmo=None,
                             run_store=None,
                             cycle_lock: asyncio.Lock | None = None) -> dict:
    """Serialize the config transaction against an in-flight poll cycle."""
    if cycle_lock is None:
        return _apply_config_patch(
            body, config=config, dev_types=dev_types, managers=managers,
            reload=reload, repo_cache=repo_cache, rekey_pmo=rekey_pmo,
            run_store=run_store)
    async with cycle_lock:
        return _apply_config_patch(
            body, config=config, dev_types=dev_types, managers=managers,
            reload=reload, repo_cache=repo_cache, rekey_pmo=rekey_pmo,
            run_store=run_store)


def _apply_config_patch(body: dict, *, config, dev_types, managers,
                        reload, repo_cache=None, rekey_pmo=None,
                        run_store=None) -> dict:
    """Validate + apply a config PUT in place; hot-reload adapters; restore
    the previous config if the reload fails. `reload` is the composition
    root's reload_connections."""
    dirty_dts: list[tuple[object, list[str]]] = []
    skill_renames: list[tuple[str, str]] = []
    pmo_renames: list[tuple[str, str]] = []
    repo_renames: list[tuple[str, str]] = []
    try:
        reject_stale_patch(body)
        # inherit before merge — deep_merge replaces the list wholesale, so
        # defaults would land if we waited until after model_validate
        current = config.model_dump()
        # PMO renames must be known before intake inherit: the SPA strips
        # intake_paused on draft Save, and a renamed card's new name is not
        # in the live map — inherit from the prior name at the same index.
        early_pmo_renames = plan_list_renames(
            current.get("pmos") or [],
            body["pmos"] if isinstance(body.get("pmos"), list)
            else current.get("pmos") or [],
            anchor=pmo_rename_anchor)
        body = inherit_pmo_intake(body, current, renames=early_pmo_renames)
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
        merged_dict = deep_merge(current, body)
        # In-place card renames (SPA edits name on the same card index):
        # detect + rewrite citations BEFORE model_validate — AppConfig
        # refuses pmos[] / crons that still cite the pre-rename name.
        skill_renames = plan_list_renames(
            current.get("skill_sources") or [],
            merged_dict.get("skill_sources") or [],
            anchor=repo_rename_anchor)
        pmo_renames = plan_list_renames(
            current.get("pmos") or [], merged_dict.get("pmos") or [],
            anchor=pmo_rename_anchor)
        repo_renames = plan_list_renames(
            current.get("repos") or [], merged_dict.get("repos") or [],
            anchor=repo_rename_anchor)
        if repo_renames:
            # In-memory only until reload succeeds — same timing as secrets.
            dirty_dts = _rewrite_repo_citations(
                merged_dict, repo_renames, dev_types)
        if pmo_renames:
            _rewrite_pmo_citations(merged_dict, pmo_renames)
        merged = AppConfig.model_validate(merged_dict)
    except Exception as e:  # noqa: BLE001 — validation contract: whatever the merge/model raises on a bad patch surfaces as 422, never a 500
        _revert_devtype_memory_repos(dirty_dts, persist=True)
        raise HTTPException(422, str(e))
    previous = current
    renamed_from = {
        "skill": {old for old, _ in skill_renames},
        "pmo": {old for old, _ in pmo_renames},
        "repo": {old for old, _ in repo_renames},
    }
    # Renaming out from under an ACTIVE run breaks it: the finalizer routes
    # on run.pmo_ref (old name → fails the run), and repo resolution is
    # sticky on run.repo_ref (old name → gates the mission). Refuse now —
    # the dev-type-delete 409 pattern — rather than fail later, silently.
    if run_store is not None and (renamed_from["pmo"] or renamed_from["repo"]):
        active = list(run_store.active())
        busy = sorted(
            {r.pmo_ref for r in active
             if getattr(r, "pmo_ref", None) in renamed_from["pmo"]}
            | {r.repo_ref for r in active
               if getattr(r, "repo_ref", None) in renamed_from["repo"]})
        if busy:
            _revert_devtype_memory_repos(dirty_dts)   # in-memory only so far
            raise HTTPException(
                409, f"cannot rename {', '.join(map(repr, busy))} while "
                     f"runs are active on the old name — wait for them to "
                     f"finish (or Stop runs) and save again")
    # cross-store semantics + dry-run adapter construction live in
    # settings_bundle — ONE implementation shared with bundle apply
    # (ADR-0013); the PUT resolves templates against disk.
    try:
        validate_config_semantics(
            merged, set(dev_types),
            template_exists=lambda mt, name:
                not prompt_templates.resolve_playbook(mt, name)[1],
            dev_types=dev_types)
        dry_run_adapters(merged)
    except BundleError as e:
        _revert_devtype_memory_repos(dirty_dts, persist=True)
        raise HTTPException(e.status, str(e))
    # a removed instance's stored secrets go with it — otherwise a later
    # instance reusing the name silently inherits the dead credential
    prev_keys = set(secrets_store.connection_instances(previous))
    new_keys = set(secrets_store.connection_instances(merged))
    removed = sorted(
        (scope, name) for scope, name in (prev_keys - new_keys)
        if name not in renamed_from.get(scope, ()))
    for field in type(merged).model_fields:
        setattr(config, field, getattr(merged, field))
    save_config(config)
    # Rekey live managers before reload so build_managers treats the event
    # as identity continuity, not delete+add (intake/assignments ride the
    # renamed PMOInstance; advisory state rides the manager object).
    if rekey_pmo is not None:
        for old_name, new_name in pmo_renames:
            try:
                rekey_pmo(old_name, new_name)
            except Exception:  # noqa: BLE001 — best-effort continuity; reload still rebuilds
                log.exception("could not rekey PMO managers %r → %r",
                              old_name, new_name)
    try:
        reload()                                 # hot reload pmo + forge
    except Exception as e:  # noqa: BLE001 — rollback contract: ANY reload failure restores the previous config; the 500 carries the cause
        log.exception("reload_connections failed — restoring previous config")
        # Reverse rekey BEFORE restore reload so build_managers keeps the
        # original manager object under the prior name (not delete+add).
        if rekey_pmo is not None:
            for old_name, new_name in pmo_renames:
                try:
                    rekey_pmo(new_name, old_name)
                except Exception:  # noqa: BLE001 — best-effort; restore reload still runs
                    log.exception("could not reverse PMO rekey %r → %r",
                                  new_name, old_name)
        _revert_devtype_memory_repos(dirty_dts, persist=True)
        restored = AppConfig.model_validate(previous)
        for field in type(restored).model_fields:
            setattr(config, field, getattr(restored, field))
        save_config(config)
        try:
            reload()
        except Exception:
            log.exception("restore reload also failed")
        raise HTTPException(500, f"config reload failed; previous config restored: {e}")
    # Dev Type citations persist only after reload — parity with secrets/mirrors.
    _persist_devtype_memory_repos(dirty_dts)
    for scope, pairs in (("skill", skill_renames),
                         ("pmo", pmo_renames),
                         ("repo", repo_renames)):
        for old_name, new_name in pairs:
            try:
                secrets_store.rename_connection_instance(
                    scope, old_name, new_name)
            except Exception:  # noqa: BLE001 — best-effort: config change is APPLIED; orphan named in the log
                log.exception("could not rename %s secrets %r → %r",
                              scope, old_name, new_name)
            if scope in _MIRRORED_SCOPES and repo_cache is not None:
                try:
                    repo_cache.rename_mirror(old_name, new_name)
                except Exception:  # noqa: BLE001 — best-effort; config applied
                    log.exception("could not rename mirror %r → %r",
                                  old_name, new_name)
    if pmo_renames or repo_renames:
        # Adapters capture their credential VALUE at construction (make_pmo /
        # make_forge pass inst.api_key/token in), and the reload above ran
        # BEFORE the secret files moved — so every renamed instance's adapter
        # was built with an empty key and would poll dead until the next
        # unrelated reload. Rebuild once now that the files are under the new
        # names. Best-effort: the config change is APPLIED either way, and
        # the ordinary rollback contract stayed with the first reload.
        try:
            reload()
        except Exception:  # noqa: BLE001 — heal-only rebuild; failure logged, healed at next reload/boot
            log.exception("post-rename adapter rebuild failed — renamed "
                          "instances may stay degraded until the next "
                          "config save or boot")
    renamed_onto = {new_name
                    for pairs in (skill_renames, pmo_renames, repo_renames)
                    for _, new_name in pairs}
    for scope, name in removed:                  # only once the new config took
        try:
            secrets_store.delete_connection_instance(scope, name)
        except Exception:  # noqa: BLE001 — cleanup is best-effort: the config change is APPLIED; a failure must not 500 it (audit A21); orphan named in the log
            log.exception("could not delete stored secrets of removed "
                          "%s instance %r", scope, name)
        if (scope in _MIRRORED_SCOPES and repo_cache is not None
                and name not in renamed_onto):
            # ADR-0024: the removed card's mirror goes with it (same
            # best-effort contract as the secret deletion above). Skill
            # sources maintain a mirror under the same namespace as repo
            # cards, so both scopes clean up here. Never delete a name a
            # rename just migrated ONTO in this same Save (remove skill X
            # + rename repo Y→X would destroy Y's live mirror). A same-name
            # same-scope card added later cold-clones by design: mirrors
            # are name-keyed, and inheriting a removed card's freshness
            # without a URL check is the riskier direction.
            try:
                repo_cache.delete_mirror(name)
            except Exception:  # noqa: BLE001 — cleanup only; the config change is APPLIED
                log.exception("could not delete mirror of removed %s %r",
                              scope, name)
    # Per-repo auto_merge OFF→ON (founder request 2026-07-15, ADR-0020):
    # re-arm the deferred-merge window only for missions whose work repo
    # flipped — the next sweep posts a fresh window entry for those.
    apply_auto_merge_rearm(previous.get("repos") or [], config.repos, managers)
    return config.model_dump()


def _rewrite_repo_citations(merged_dict: dict,
                            renames: list[tuple[str, str]],
                            dev_types: dict
                            ) -> list[tuple[object, list[str]]]:
    """Rewrite every config citation of a renamed repo card name.

    Mutates the pre-validate merged dict (pmos lists) and live Dev Type
    objects so model_validate / semantics see the post-rename name set.
    Does NOT persist Dev Types — caller saves only after successful reload
    (and reverts on failure). Returns (dt, prior_memory_repos) pairs.
    """
    if not renames:
        return []
    mapping = dict(renames)

    def _map_names(names: list) -> list[str]:
        return [mapping.get(n, n) for n in names]

    for pmo in merged_dict.get("pmos") or []:
        if not isinstance(pmo, dict):
            continue
        for field in ("repos", "reference_repos", "memory_repos"):
            if field in pmo and pmo[field] is not None:
                pmo[field] = _map_names(list(pmo[field]))
    for src in merged_dict.get("skill_sources") or []:
        # ADR-0039: a backed skill source cites its backing repo card by
        # name — the citation follows a rename like every other one
        if isinstance(src, dict) and src.get("backed_by"):
            src["backed_by"] = mapping.get(src["backed_by"], src["backed_by"])
    dirty: list[tuple[object, list[str]]] = []
    for dt in (dev_types or {}).values():
        before = list(getattr(dt, "memory_repos", None) or [])
        after = _map_names(before)
        if after != before:
            dt.memory_repos = after
            dirty.append((dt, before))
    return dirty


def _revert_devtype_memory_repos(
        dirty: list[tuple[object, list[str]]], *, persist: bool = False
) -> None:
    """Undo in-memory Dev Type citation rewrites after a failed PUT."""
    if not dirty:
        return
    from ..config import save_dev_type
    for dt, before in dirty:
        dt.memory_repos = list(before)
        if persist:
            try:
                save_dev_type(dt)
            except Exception:  # noqa: BLE001 — best-effort heal of any premature disk write
                log.exception(
                    "could not restore Dev Type %r memory_repos after "
                    "failed repo rename", getattr(dt, "name", "?"))


def _persist_devtype_memory_repos(
        dirty: list[tuple[object, list[str]]]) -> None:
    """Write Dev Type citation rewrites only after successful reload."""
    if not dirty:
        return
    from ..config import save_dev_type
    for dt, _before in dirty:
        try:
            save_dev_type(dt)
        except Exception:  # noqa: BLE001 — best-effort persist; in-memory already rewritten
            log.exception("could not persist Dev Type %r after repo rename",
                          getattr(dt, "name", "?"))


def _rewrite_pmo_citations(merged_dict: dict,
                           renames: list[tuple[str, str]]) -> None:
    """Rewrite cron targets that cite a renamed PMO instance name."""
    if not renames:
        return
    mapping = dict(renames)
    for job in merged_dict.get("crons") or []:
        if not isinstance(job, dict) or job.get("reserved"):
            continue
        pmo = job.get("pmo")
        if pmo in mapping:
            job["pmo"] = mapping[pmo]


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
