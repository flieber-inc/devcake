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
    """Store a connection secret VALUE (never echoed). scope ∈ pmo|repo|skill;
    instance is the config instance name; field ∈ the scope's
    CONNECTION_FIELDS allowlist. Writing a repo/pmo secret clears any
    latched breaker."""
    _require_secret_ref(scope, instance, field)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "value must be a non-empty string")
    async with _cycle(cycle_lock):
        secrets_store.write_connection_secret(scope, instance, field, value)
        if scope == "repo":
            forge_runtime.clear_breaker(instance)
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
            forge_runtime.clear_breaker(instance)
            reset_health_caches()
        reload()
    return {"present": False}


def _url_host(url_or_base: str) -> str:
    """Lowercased hostname of a URL or bare host ("" when empty or
    unparsable) — the CAKE-113 shape from security.py's mono-repo check."""
    from urllib.parse import urlsplit
    v = (url_or_base or "").strip()
    if not v:
        return ""
    parts = urlsplit(v if "://" in v else f"https://{v}")
    return (parts.hostname or "").lower()


def _hosts_equivalent(a: str, b: str, aliases) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    for group in aliases or []:
        members = {h.lower() for h in group}
        if a in members and b in members:
            return True
    return False


def _pmo_host(pmo, info) -> str:
    """The host a forge-issues board talks to: its api_base, else the
    system's registered default host ("" = undeterminable)."""
    return _url_host(pmo.api_base or "") or (info.default_host or "").lower()


def _copy_plan(source_scope: str, source, target_scope: str, target
               ) -> dict[str, str] | str:
    """{target_field: source_field} for one copy pair, or a refusal reason
    (a STRING return is the refusal — copy_secrets raises it as a 422 on a
    real run and reports it as a `refused` row in dry-run).

    Same forge is NOT enough: gitea and gitlab self-host and GitHub has
    Enterprise, so a PAT is only fleet-valid on the SAME HOST. repo→repo
    compares URL hosts; repo→board compares the repo host against the
    board's api_base (or the system's registered default, honoring the
    registry's alias groups — api.github.com ≡ github.com). A repo card
    seeds same-forge same-host repo cards slot for slot, and that host's
    *_issues board api_key from its WRITE token (the board's key IS a
    forge PAT — those systems address a repository, not a workspace). A
    PMO key maps onto nothing narrower than its own system + host:
    pmo→pmo only, never pmo→repo. The `{forge}_issues` naming convention
    is load-bearing (all registered systems follow it); a system that
    breaks it must extend this table."""
    from ..adapters.registry import PMO_SYSTEMS
    if source_scope == "repo":
        src_host = _url_host(source.url)
        if not src_host:
            return "the source card has no repository URL — set it first"
        if target_scope == "repo":
            if target.forge != source.forge:
                return (f"different forge ({target.forge} vs "
                        f"{source.forge}) — its tokens cannot work there")
            tgt_host = _url_host(target.url)
            if not tgt_host:
                return "card has no repository URL yet — set it first"
            if src_host != tgt_host:
                return (f"different host ({tgt_host} vs {src_host}) — a "
                        f"{source.forge} token is per host")
            return {f: f for f in sorted(_SECRET_FIELDS["repo"])}
        if target.system != f"{source.forge}_issues":
            return (f"system {target.system!r} does not take a "
                    f"{source.forge} token")
        info = PMO_SYSTEMS.get(target.system)
        tgt_host = _pmo_host(target, info) if info is not None else ""
        if not tgt_host:
            return "the board has no API base yet — set it first"
        if not _hosts_equivalent(src_host, tgt_host,
                                 info.host_aliases if info else []):
            return (f"different host ({tgt_host} vs {src_host}) — a "
                    f"{source.forge} token is per host")
        return {"api_key": "token"}
    if target_scope != "pmo" or target.system != source.system:
        return "a PMO key only fits PMO cards of the same system"
    info = PMO_SYSTEMS.get(source.system)
    if info is not None and info.forge_issue:
        a, b = _pmo_host(source, info), _pmo_host(target, info)
        if (a or b) and not _hosts_equivalent(a, b, info.host_aliases):
            return (f"different host ({b or 'unset'} vs {a or 'unset'}) — "
                    f"its key is per host")
    return {"api_key": "api_key"}


async def copy_secrets(body: dict, *, config, forge_runtime, reload,
                       cycle_lock: asyncio.Lock | None = None):
    """Copy one card's stored tokens onto selected sibling cards, slot for
    slot (write→write, read-only→read-only, reviewer→reviewer). VALUES
    never ride the request or the response — the server reads the source's
    store entries and writes the targets'.

    Two modes. `dry_run: true` answers what WOULD happen: per-target rows
    with the slots that would move (`receives`) and the mapped-but-empty
    ones (`skipped`); ineligible or unknown targets come back as
    non-eligible rows with the reason instead of failing the call, so a
    stale client card list degrades readably. The REAL run is strict and
    all-or-nothing up front (the /secrets/clear contract): an unknown
    card, a duplicate target, the source among the targets, or an
    incompatible pair each 422 with nothing written. Validation, the
    source reads, and the writes all run under the poll-cycle lock so a
    concurrent clear or rotation cannot interleave — a copy must never
    resurrect a just-cleared token. A write failure mid-batch still
    rebuilds the adapters to match the disk and names what was already
    written. One audit event records source, targets, and field NAMES.
    The skill scope is deliberately excluded: repo-backed sources need no
    token of their own, and a read-token family can join later without
    changing this shape."""
    dry_run = bool(body.get("dry_run"))
    src = body.get("source")
    if (not isinstance(src, dict) or not isinstance(src.get("scope"), str)
            or not isinstance(src.get("name"), str)):
        raise HTTPException(
            422, "source must be {scope: repo|pmo, name: <card>}")
    raw_targets = body.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise HTTPException(422, "targets must be a non-empty list of "
                                 "{scope, name}")
    for t in raw_targets:
        if (not isinstance(t, dict) or not isinstance(t.get("scope"), str)
                or not isinstance(t.get("name"), str)):
            raise HTTPException(
                422, "each target must be {scope: repo|pmo, name: <card>}")

    async with _cycle(cycle_lock):
        cards = {"repo": {r.name: r for r in config.repos},
                 "pmo": {p.name: p for p in config.pmos}}
        src_scope, src_name = src["scope"], src["name"]
        if src_scope not in cards:
            raise HTTPException(
                422, "source must be {scope: repo|pmo, name: <card>}")
        source = cards[src_scope].get(src_name)
        if source is None:
            raise HTTPException(404, f"no {src_scope} card named {src_name!r}")

        values = {f: secrets_store.read_connection_secret(
                      src_scope, src_name, f)
                  for f in sorted(_SECRET_FIELDS[src_scope])}
        if not any(values.values()):
            raise HTTPException(422, f"{src_scope} {src_name!r} has no "
                                     f"stored tokens to copy")

        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for t in raw_targets:
            scope, name = t["scope"], t["name"]
            refusal, plan = None, None
            if scope not in cards:
                refusal = "unknown scope — repo|pmo"
            elif (scope, name) == (src_scope, src_name):
                refusal = "the source itself"
            elif (scope, name) in seen:
                refusal = "duplicate target"
            elif cards[scope].get(name) is None:
                # refuse rather than skip: writing by name would create an
                # orphan secret file for a card that does not exist (the
                # settings-bundle apply skips these for the same reason)
                refusal = f"no {scope} card named {name!r}"
            else:
                plan = _copy_plan(src_scope, source, scope,
                                  cards[scope][name])
                if isinstance(plan, str):
                    refusal, plan = plan, None
            seen.add((scope, name))
            if refusal is not None:
                if not dry_run:
                    raise HTTPException(422, f"{scope} {name!r}: {refusal}")
                rows.append({"scope": scope, "name": name,
                             "eligible": False, "reason": refusal})
                continue
            rows.append({
                "scope": scope, "name": name, "eligible": True,
                "plan": plan,
                "receives": sorted(f for f in plan if values[plan[f]]),
                "skipped": sorted(f for f in plan if not values[plan[f]])})

        if dry_run:
            for row in rows:
                row.pop("plan", None)
            return {"ok": True, "dry_run": True,
                    "source": {"scope": src_scope, "name": src_name},
                    "targets": rows}

        results: list[dict] = []
        written: list[str] = []
        repo_written = False
        try:
            for row in rows:
                fields = {f: values[row["plan"][f]] for f in row["receives"]}
                if fields:
                    secrets_store.write_connection_fields(
                        row["scope"], row["name"], fields)
                    written.append(f"{row['scope']}:{row['name']}")
                    if row["scope"] == "repo":
                        forge_runtime.clear_breaker(row["name"])
                        repo_written = True
                results.append({"scope": row["scope"], "name": row["name"],
                                "copied": row["receives"],
                                "skipped": row["skipped"]})
        except Exception as e:  # noqa: BLE001 — adapters must be rebuilt to match whatever reached the disk before the error surfaces
            if repo_written:
                reset_health_caches()
            reload()
            raise HTTPException(
                500, f"copy interrupted after writing "
                     f"{', '.join(written) or 'nothing'} — "
                     f"{type(e).__name__}; re-run to complete") from e
        if repo_written:
            reset_health_caches()
        # one rebuild for the whole batch — adapters capture credentials
        # by VALUE at construction, same as put_secret
        reload()

    from ..settings_bundle import audit_event
    audit_event("secrets_copied",
                f"{src_scope}:{src_name} → " + "; ".join(
                    f"{r['scope']}:{r['name']}"
                    f"[{','.join(r['copied']) or '-'}]" for r in results))
    log.info("copied %s %s tokens onto %d card(s)", src_scope, src_name,
             len(results))
    return {"ok": True, "source": {"scope": src_scope, "name": src_name},
            "results": results}


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
                forge_runtime.clear_breaker(instance)
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
    means editing the SPA (docs/11). Single projection:
    adapters.registry.connections_registry_payload (spa-contracts pin)."""
    from ..adapters.registry import connections_registry_payload
    return connections_registry_payload()


def _with_error(payload: dict, *, detail: str = "") -> dict:
    """SPA Test connection prints `error`. Probe DTOs use `detail`."""
    if payload.get("ok"):
        return payload
    err = (payload.get("error") or payload.get("detail") or detail or "").strip()
    if not err:
        err = "connection probe failed — see app logs for details"
    return {**payload, "error": err}


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
        return _with_error({
            "ok": h.ok, "instance": name,
            "team": h.workspace or inst.team_key,
            "labels": h.managed_labels_present,
            "labels_expected": h.managed_labels_expected,
            "missions_visible": len(missions),
            "detail": h.detail,
        })
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
            return _with_error(health)
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


def _work_eligible_repo(inst) -> str | None:
    """Return a refusal reason when ``inst`` is not a work target for apply,
    else None. Mirrors the single-endpoint gates (empty URL / reference-only)."""
    if not inst.configured:
        return ("repository URL is empty — the repo is idle until one "
                "is set")
    if inst.reference_only:
        return ("reference-only repo — apply-protection is for work "
                "repos with a write token")
    return None


async def _repo_looks_unprotected(forge, branch: str) -> bool:
    """True when the thin health DTO says unprotected or the probe is unknown.

    Bulk membership uses this filter; apply itself may still return
    ``already_as_strict`` when the richer shape is already strict.
    """
    try:
        prot = await forge.default_branch_protection(branch)
    except Exception:  # noqa: BLE001 — membership probe: failure → treat as unprotected so the operator apply still runs
        return True
    if prot is None:
        return True
    return not bool(getattr(prot, "protected", False))


async def apply_forge_protection(name: str, *, config, forge_runtime):
    """Operator-explicit apply of default-branch protection for one work repo.

    Never auto-runs on connect/create. Returns `{ok, repo, outcome, shape}` on
    success or `{ok: false, repo, error, status?}` on refusal / ForgeError —
    same family as the connection-test contract (never a 500 for vendor
    capability/permission failures). Unknown repo → HTTP 404.
    """
    from ..settings_bundle import audit_event

    inst = next((r for r in config.repos if r.name == name), None)
    if inst is None:
        raise HTTPException(404, f"no repo named {name!r}")
    refuse = _work_eligible_repo(inst)
    if refuse is not None:
        out = {"ok": False, "repo": name, "error": refuse}
        audit_event("forge_protection_applied",
                    f"repo={name} error={out['error']}")
        return out
    f = forge_runtime.get(name)
    if f is None:
        out = {
            "ok": False, "repo": name,
            "error": "repo not active — save the config first, then apply",
        }
        audit_event("forge_protection_applied",
                    f"repo={name} error={out['error']}")
        return out
    caps = getattr(f, "capabilities", None)
    if caps is not None and not getattr(caps, "branch_protection_write", True):
        out = {
            "ok": False, "repo": name,
            "error": "this forge does not support writing branch protection "
                     "(capabilities.branch_protection_write=False)",
        }
        audit_event("forge_protection_applied",
                    f"repo={name} error={out['error']}")
        return out
    try:
        result = await f.apply_default_branch_protection(inst.default_branch)
        reset_health_caches()
        out = {
            "ok": True,
            "repo": name,
            "outcome": result.outcome,
            "shape": result.shape.model_dump(),
        }
        audit_event(
            "forge_protection_applied",
            f"repo={name} outcome={result.outcome}",
        )
        return out
    except ForgeError as e:
        status = e.status
        err = redact(str(e))[:200]
        out: dict = {"ok": False, "repo": name, "error": err}
        if status is not None:
            out["status"] = status
        audit_event(
            "forge_protection_applied",
            f"repo={name} error={err}",
        )
        return out
    except Exception as e:  # noqa: BLE001 — operator-apply contract: any unexpected failure → ok:False + redacted error, never a 500
        err = _probe_client_error(e)
        out = {"ok": False, "repo": name, "error": err}
        audit_event(
            "forge_protection_applied",
            f"repo={name} error={err}",
        )
        return out


async def apply_forge_protection_bulk(*, config, forge_runtime):
    """Apply protection to every currently-unprotected work repo.

    Membership: configured URL, not reference-only, active forge adapter, and
    thin ``default_branch_protection`` is None or ``protected=False``. One
    repo's 403 never aborts siblings — results are per-repo (same shape as
    single). Skipped ineligible repos are omitted from ``results``.
    """
    results: list[dict] = []
    for inst in config.repos:
        if _work_eligible_repo(inst) is not None:
            continue
        f = forge_runtime.get(inst.name)
        if f is None:
            continue
        if not await _repo_looks_unprotected(f, inst.default_branch):
            continue
        # Reuse the single chokepoint so audit + cache reset stay one path.
        results.append(await apply_forge_protection(
            inst.name, config=config, forge_runtime=forge_runtime))
    return {"ok": True, "results": results}


async def test_skill_source(name: str, *, config, repo_cache):
    """Read-only connectivity probe for a dedicated skill source (CAKE-146).

    Skill sources have no live forge adapter — remote reachability via
    RepoCache.remote_head, with the stored read token when one exists. A
    public repository needs no token (the card's own help says so), so a
    missing token never fails the probe up front — it only sharpens the
    error when the remote is unreachable. Same SPA contract as forge/PMO
    tests: `{ok, error?, …}` never a 500.
    """
    inst = next((s for s in (config.skill_sources or []) if s.name == name),
                None)
    if inst is None:
        raise HTTPException(404, f"no skill source named {name!r}")
    if not inst.configured:
        return {"ok": False, "error": "repository URL is empty — the skill "
                                      "source is idle until one is set"}
    if repo_cache is None:
        return {"ok": False, "error": "mirror cache unavailable — save the "
                                      "config and retry"}
    backed = inst.backed_by
    backing = (next((r for r in config.repos if r.name == backed), None)
               if backed else None)
    # what the probe actually rides: the backing card's forge and remote for
    # a backed source (ADR-0039), the card's own otherwise — one derivation
    # for the failure and success payloads so the two can never diverge
    forge_label = backing.forge if backing is not None else inst.forge
    repo_label = inst.url or f"backed by {backed}"
    try:
        head = await repo_cache.remote_head(name)
        if not head:
            detail = ("could not reach the remote — check the URL, "
                      "token, and network")
            if backed:
                # ADR-0039: the backing repo card owns URL + token — its
                # own Test connection is where the fix lives. A branch
                # pinned on THIS card is probed too, so a missing pinned
                # branch also lands here.
                detail = (f"could not reach the remote through repository "
                          f"card {backed!r} (or this card pins a branch "
                          f"the remote lacks) — test that card")
            elif not (inst.token or inst.token_ro):
                detail = ("could not reach the remote and no token is "
                          "stored — a public repository needs none; a "
                          "private one needs a Read token on this card")
            return _with_error({
                "ok": False,
                "skill_source": name,
                "forge": forge_label,
                "repo": repo_label,
                "detail": detail,
            })
        return {"ok": True, "skill_source": name, "forge": forge_label,
                "repo": repo_label, "remote_head": head}
    except Exception as e:  # noqa: BLE001 — connection-test contract: any probe failure → ok:False + error in the response, never a 500
        return {"ok": False, "error": _probe_client_error(e)}
