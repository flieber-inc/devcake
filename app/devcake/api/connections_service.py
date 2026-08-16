"""Connections application service (ADR-0015 Decision 3): GUI-stored
connection/harness secret writes (M12, F5), the presence check, the
connections registry, and the PMO/forge connection tests. main.py keeps
thin forwards that pass the composition root's singletons at call time
(`reload` is the composition root's reload_connections)."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import nullcontext

from fastapi import HTTPException

from .. import secrets as secrets_store
from ..config import HARNESS_VAR_PATTERN, _INSTANCE_NAME_RE
from ..domain.model import ALL_LABELS
from ..harness import HARNESSES
from ..ports.forge import ForgeError, mission_branch
from ..ports.pmo import PMOTransient
from ..security import redact
from .health import reset_health_caches

log = logging.getLogger("devcake.connections")

# ── GUI-stored secrets (M12, F5): write-only VALUES, never echoed back ───────

_SECRET_SCOPES = set(secrets_store.CONNECTION_FIELDS)
# per-scope field allowlist — ONE definition (secrets.CONNECTION_FIELDS,
# shared with settings_bundle); scope/instance/field all reach the filesystem
# as path components, so every entry point validates against it (audit A5/A9)
_SECRET_FIELDS = secrets_store.CONNECTION_FIELDS
_HARNESS_VAR_RE = re.compile(f"^{HARNESS_VAR_PATTERN}$")   # one definition: config.py


def _valid_secret_ref(scope: str, instance: str, field: str) -> bool:
    return (scope in _SECRET_FIELDS and field in _SECRET_FIELDS[scope]
            and re.fullmatch(_INSTANCE_NAME_RE, instance) is not None)


def _require_secret_ref(scope: str, instance: str, field: str) -> None:
    if scope not in _SECRET_SCOPES:
        raise HTTPException(404, f"unknown secret scope {scope!r}")
    if not _valid_secret_ref(scope, instance, field):
        raise HTTPException(
            422, f"invalid secret ref: instance must match {_INSTANCE_NAME_RE}"
                 f" and field ∈ {sorted(_SECRET_FIELDS[scope])}")


def _require_harness_var(var: str) -> None:
    if not _HARNESS_VAR_RE.fullmatch(var):
        raise HTTPException(
            422, f"harness var must match ^{HARNESS_VAR_PATTERN}$")


def _cycle(lock: asyncio.Lock | None):
    """Serialize the write + adapter-graph swap against an in-flight poll
    cycle — the config-PUT precedent (PR #104). The poll loop holds
    `poll_rt.lock` for a whole cycle but suspends at awaits; without this,
    a secret write mid-cycle swaps the graph (or deletes the credential)
    underneath the suspended cycle. None (tests, scripts) = no
    serialization, mirroring `apply_config_patch`."""
    return lock if lock is not None else nullcontext()


async def put_secret(scope: str, instance: str, field: str, body: dict, *,
                     forge_runtime, reload,
                     cycle_lock: asyncio.Lock | None = None):
    """Store a connection secret VALUE (never echoed). scope ∈ pmo|repo;
    instance is the config instance name; field ∈ api_key|token|token_ro|
    reviewer_token. Writing a repo/pmo secret clears any latched breaker."""
    _require_secret_ref(scope, instance, field)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "value must be a non-empty string")
    async with _cycle(cycle_lock):
        secrets_store.write_connection_secret(scope, instance, field, value)
        if scope == "repo":
            forge_runtime.breakers.pop(instance, None)
            reset_health_caches()
        # adapters capture credentials by VALUE at construction — a rotated
        # secret takes effect only through a rebuild, same as a config PUT
        reload()
    return secrets_store.connection_status(scope, instance, field)


async def delete_secret(scope: str, instance: str, field: str, *,
                        forge_runtime, reload,
                        cycle_lock: asyncio.Lock | None = None):
    _require_secret_ref(scope, instance, field)
    async with _cycle(cycle_lock):
        secrets_store.delete_connection_field(scope, instance, field)
        if scope == "repo":
            forge_runtime.breakers.pop(instance, None)
            reset_health_caches()
        reload()
    return {"present": False}


async def put_harness_secret(var: str, body: dict, *, dev_types,
                             shared_breakers):
    """Store a harness/model key VALUE (e.g. ANTHROPIC_API_KEY)."""
    _require_harness_var(var)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "value must be a non-empty string")
    secrets_store.write_harness_secret(var, value)
    # fresh key clears the DEV_AUTH breaker of every dev type running a
    # harness that consumes this var (mirrors the credential-file path)
    for dt_name, dt in dev_types.items():
        if var in HARNESSES[dt.harness_template].credential_env:
            shared_breakers.pop(dt_name, None)
    return secrets_store.harness_status(var)


async def delete_harness_secret(var: str):
    """Revoke a stored harness/model key (audit A10) — previously a
    compromised key could only be overwritten, never removed, from the GUI.
    No reload needed: harness keys are read live at dispatch."""
    _require_harness_var(var)
    secrets_store.delete_harness_secret(var)
    return {"present": False}


async def secrets_check(conn: str = "", harness: str = ""):
    """Presence + updated_at (NEVER the value) for the ✓/✗ UI. `conn` is a
    comma list of scope:instance:field triples; `harness` a comma list of
    var names. Invalid refs are silently dropped — they previously reached
    the filesystem, an existence/mtime oracle for arbitrary *.json paths
    (audit A5)."""
    out: dict = {"conn": {}, "harness": {}}
    for triple in (t for t in conn.split(",") if t):
        parts = triple.split(":")
        if len(parts) == 3 and _valid_secret_ref(*parts):
            out["conn"][triple] = secrets_store.connection_status(*parts)
    for var in (v for v in harness.split(",") if v):
        if _HARNESS_VAR_RE.fullmatch(var):
            out["harness"][var] = secrets_store.harness_status(var)
    return out


async def secrets_inventory():
    """Presence-only catalog for the Clear-secrets modal — never values."""
    return secrets_store.inventory()


async def clear_secrets(body: dict, *, forge_runtime, reload, config,
                        shared_breakers, dev_types,
                        cycle_lock: asyncio.Lock | None = None):
    """Delete the operator-selected subset of stored secrets.

    Body:
      harness: [VAR, …]
      connections: [{scope, instance, field}, …]
      credential_files: [{dev_type, filename}, …]
      pause_intake: bool  (optional; default false when omitted)

    Order is load-bearing: validate → optional master intake pause (fail
    aborts with no deletes) → deletes → breakers/reload → audit. The
    pause-through-reload transaction runs under the poll-cycle lock
    (`_cycle`): a cycle observes either the pre-clear world or the
    post-reload one, never a half-swapped adapter graph over deleted keys.

    SPA always sends pause_intake explicitly; API-safe default when omitted
    is false so scripts do not freeze intake by accident.
    """
    from ..config import save_config
    from ..settings_bundle import audit_event

    harness_req = body.get("harness") or []
    conn_req = body.get("connections") or []
    files_req = body.get("credential_files") or []
    if not isinstance(harness_req, list) or not isinstance(conn_req, list) \
            or not isinstance(files_req, list):
        raise HTTPException(422, "harness, connections, and credential_files "
                                 "must each be a list when present")
    if not harness_req and not conn_req and not files_req:
        raise HTTPException(422, "select at least one secret to clear")

    if "pause_intake" not in body:
        pause_intake = False
    else:
        pause_intake = body.get("pause_intake")
        if not isinstance(pause_intake, bool):
            raise HTTPException(422, "pause_intake must be a boolean")

    harness_vars: list[str] = []
    for var in harness_req:
        if not isinstance(var, str) or not _HARNESS_VAR_RE.fullmatch(var):
            raise HTTPException(422, f"invalid harness var {var!r}")
        harness_vars.append(var)

    connections: list[tuple[str, str, str]] = []
    for item in conn_req:
        if not isinstance(item, dict):
            raise HTTPException(422, "each connection entry must be an object")
        scope = item.get("scope")
        instance = item.get("instance")
        field = item.get("field")
        if not (isinstance(scope, str) and isinstance(instance, str)
                and isinstance(field, str)):
            raise HTTPException(422, "connection entries need string "
                                     "scope/instance/field")
        _require_secret_ref(scope, instance, field)
        connections.append((scope, instance, field))

    credential_files: list[tuple[str, str]] = []
    for item in files_req:
        if not isinstance(item, dict):
            raise HTTPException(
                422, "each credential_files entry must be an object")
        dev_type = item.get("dev_type")
        filename = item.get("filename")
        if not (isinstance(dev_type, str) and isinstance(filename, str)):
            raise HTTPException(
                422, "credential_files entries need string dev_type/filename")
        try:
            secrets_store.require_credential_ref(dev_type, filename)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        credential_files.append((dev_type, filename))

    async with _cycle(cycle_lock):
        # ── pause first (if requested) — no deletes yet ─────────────────────
        if pause_intake:
            config.intake_paused = True
            save_config(config)

        deleted_h: list[str] = []
        for var in harness_vars:
            secrets_store.delete_harness_secret(var)
            deleted_h.append(var)
            for dt_name, dt in dev_types.items():
                if var in HARNESSES[dt.harness_template].credential_env:
                    shared_breakers.pop(dt_name, None)

        deleted_c: list[dict] = []
        repo_touched = False
        for scope, instance, field in connections:
            secrets_store.delete_connection_field(scope, instance, field)
            deleted_c.append({"scope": scope, "instance": instance,
                              "field": field})
            if scope == "repo":
                forge_runtime.breakers.pop(instance, None)
                repo_touched = True

        deleted_f: list[dict] = []
        for dev_type, filename in credential_files:
            secrets_store.delete_credential_file(dev_type, filename)
            deleted_f.append({"dev_type": dev_type, "filename": filename})
            shared_breakers.pop(dev_type, None)

        if repo_touched:
            reset_health_caches()
        if connections:
            # connection secrets are captured at adapter construction
            reload()

    audit_event(
        "secrets_cleared",
        f"harness={len(deleted_h)} connections={len(deleted_c)} "
        f"files={len(deleted_f)} pause_intake={pause_intake}",
    )

    return {
        "ok": True,
        "deleted": {
            "harness": deleted_h,
            "connections": deleted_c,
            "credential_files": deleted_f,
        },
        "intake_paused": bool(config.intake_paused),
    }


async def connections_registry():
    """Available PMO systems and forges with display metadata — drives the
    admin Config page's selectors and paste guard, so adding an adapter never
    means editing the SPA (docs/11)."""
    from ..adapters.registry import PMO_SYSTEMS, forges
    forge_descriptors = forges()
    return {
        "pmo_systems": [
            {
                "id": s.id,
                "display_name": s.display_name,
                "needs_api_base": s.needs_api_base,
                "team_key_label": s.team_key_label,
                "team_key_help": s.team_key_help,
                "api_base_help": s.api_base_help,
                "supports_priority": s.supports_priority,
                "operator_note": s.operator_note,
                "attachments_supported": s.attachments_supported,
                "relations_supported": s.relations_supported,
            }
            for s in PMO_SYSTEMS.values()
        ],
        "forges": [{"id": d.id, "display_name": d.display_name}
                   for d in forge_descriptors.values()],
        "secret_shape_prefixes": sorted(
            {p for s in PMO_SYSTEMS.values() for p in s.secret_shape_prefixes}
            | {p for d in forge_descriptors.values()
               for p in d.secret_shape_prefixes}),
        "managed_labels_expected": len(ALL_LABELS),
    }


def _probe_client_error(e: Exception) -> str:
    """Client-facing probe error: never echo raw vendor bodies (tokens/URLs).

    Domain exceptions keep a short redacted message so operators still get a
    signal; everything else is a stable generic string. Full detail stays in
    app logs only."""
    log.warning("connection probe failed: %s", redact(repr(e))[:500])
    if isinstance(e, (PMOTransient, ForgeError)):
        return redact(str(e))[:200]
    return "connection probe failed — see app logs for details"


async def test_pmo(name: str, *, config, managers):
    inst = next((i for i in config.pmos if i.name == name), None)
    if inst is None:
        raise HTTPException(404, f"no PMO instance named {name!r}")
    if not inst.configured:
        return {"ok": False, "error": "team key is empty — the instance is "
                                      "idle until one is set"}
    if not inst.api_key:
        return {"ok": False, "error": "API key not set — enter it on the "
                                      "Config page (it is stored securely, "
                                      "never in .env)"}
    mgr = managers.get(name)
    if mgr is None:
        return {"ok": False, "error": "instance not active — save the config "
                                      "first, then test"}
    try:
        h = await mgr.pmo.health_probe(inst.team_key)
        missions = await mgr.pmo.list_all(inst.team_key)
        return {"ok": h.ok, "instance": name,
                "team": h.workspace or inst.team_key,
                "labels": h.managed_labels_present,
                "labels_expected": h.managed_labels_expected,
                "missions_visible": len(missions)}
    except Exception as e:  # noqa: BLE001 — connection-test contract: any probe failure → ok:False + error in the response, never a 500
        return {"ok": False, "error": _probe_client_error(e)}


async def test_forge(name: str, *, config, forge_runtime):
    inst = next((r for r in config.repos if r.name == name), None)
    if inst is None:
        raise HTTPException(404, f"no repo named {name!r}")
    if not inst.configured:
        return {"ok": False, "error": "repository URL is empty — the repo is "
                                      "idle until one is set"}
    f = forge_runtime.get(name)
    if f is None:
        return {"ok": False, "error": "repo not active — save the config "
                                      "first, then test"}
    # a read-only token alone is a valid, testable state (reference-only —
    # founder decision 2026-07-15); only ZERO stored tokens refuses
    if not inst.token and not inst.token_ro:
        return {"ok": False, "error": "no token stored — enter an Access "
                                      "token (work repo) or a Read-only token "
                                      "(reference-only) on this card"}
    try:
        health = await forge_runtime.refresh_health(name)
        if not health["ok"]:
            return health
        # reference-only: read access is the WHOLE contract — the PR-listing
        # and branch-protection probes need API scopes a read-only PAT may
        # lack, and DevCake never opens PRs here anyway
        if inst.reference_only:
            return {"ok": True, "repo_name": name, "forge": inst.forge,
                    "repo": inst.url, "can_push": False,
                    "reference_only": True,
                    "reviewer_token_configured": False, "probe_pr": None,
                    "branch_protection": None}
        # v4 allows a repo-only (0-pmo) config — probe with the SYS
        # pseudo-instance then (HELLO/OAUTH precedent, never a real branch)
        probe = config.pmos[0].name if config.pmos else "sys"
        pr = await f.get_pr_by_branch(mission_branch(probe, "__connection_test__"))
        reviewer = bool(getattr(f, "reviewer_token", None))
        protection = await f.default_branch_protection(inst.default_branch)
        return {"ok": True, "repo_name": name, "forge": inst.forge,
                "repo": inst.url, "can_push": health["can_push"],
                "reference_only": inst.reference_only,
                "reviewer_token_configured": reviewer, "probe_pr": pr is None,
                "branch_protection": protection.model_dump() if protection else None}
    except Exception as e:  # noqa: BLE001 — connection-test contract: any probe failure → ok:False + error in the response, never a 500
        return {"ok": False, "error": _probe_client_error(e)}
