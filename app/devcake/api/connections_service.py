"""Connections application service (ADR-0015 Decision 3): GUI-stored
connection/harness secret writes (M12, F5), the presence check, the
connections registry, and the PMO/forge connection tests. main.py keeps
thin forwards that pass the composition root's singletons at call time
(`reload` is the composition root's reload_connections)."""

from __future__ import annotations

import re

from fastapi import HTTPException

from .. import secrets as secrets_store
from ..config import HARNESS_VAR_PATTERN, _INSTANCE_NAME_RE
from ..domain.model import ALL_LABELS
from ..harness import HARNESSES
from ..ports.forge import mission_branch
from .health import reset_protection_cache

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
        raise HTTPException(422, "harness var must match ^[A-Z][A-Z0-9_]{0,63}$")


async def put_secret(scope: str, instance: str, field: str, body: dict, *,
                     forge_runtime, reload):
    """Store a connection secret VALUE (never echoed). scope ∈ pmo|repo;
    instance is the config instance name; field ∈ api_key|token|token_ro|
    reviewer_token. Writing a repo/pmo secret clears any latched breaker."""
    _require_secret_ref(scope, instance, field)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "value must be a non-empty string")
    secrets_store.write_connection_secret(scope, instance, field, value)
    if scope == "repo":
        forge_runtime.breakers.pop(instance, None)
        reset_protection_cache()
    # adapters capture credentials by VALUE at construction — a rotated
    # secret takes effect only through a rebuild, same as a config PUT
    reload()
    return secrets_store.connection_status(scope, instance, field)


async def delete_secret(scope: str, instance: str, field: str, *,
                        forge_runtime, reload):
    _require_secret_ref(scope, instance, field)
    secrets_store.delete_connection_field(scope, instance, field)
    if scope == "repo":
        forge_runtime.breakers.pop(instance, None)
        reset_protection_cache()
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


async def connections_registry():
    """Available PMO systems and forges with display metadata — drives the
    admin Config page's selectors and paste guard, so adding an adapter never
    means editing the SPA (docs/11)."""
    from ..adapters.registry import PMO_SYSTEMS, forges
    forge_descriptors = forges()
    return {
        "pmo_systems": [{"id": s.id, "display_name": s.display_name}
                        for s in PMO_SYSTEMS.values()],
        "forges": [{"id": d.id, "display_name": d.display_name}
                   for d in forge_descriptors.values()],
        "secret_shape_prefixes": sorted(
            {p for s in PMO_SYSTEMS.values() for p in s.secret_shape_prefixes}
            | {p for d in forge_descriptors.values()
               for p in d.secret_shape_prefixes}),
        "managed_labels_expected": len(ALL_LABELS),
    }


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
        return {"ok": False, "error": str(e)[:300]}


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
        return {"ok": False, "error": str(e)[:300]}
