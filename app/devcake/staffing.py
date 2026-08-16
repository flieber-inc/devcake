"""Is this Dev Type staffable for the running app digest?

Chokepoint for Slice 2: dispatch, steward, and OAuth call require_staffed
before bootstrap.launch. Hello never does. Domain depends on ReceiptStore
(port), not the file adapter.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping

from .house_pins import (
    HOUSE_PINS,
    LAUNCH_SUPPORTED,
    SENTINEL_DIGEST,
    app_digest,
    effective_cli_version,
)


class HarnessNotStaffed(ValueError):
    """Pin is not staffable. `row` names the failing required row when known."""

    def __init__(self, message: str, *, row: str | None = None,
                 kind: str = "receipt"):
        super().__init__(message)
        self.row = row
        self.kind = kind


def require_staffed(dev_type, *, digest: str | None = None,
                    store) -> None:
    """Raise HarnessNotStaffed unless a matching ok receipt exists.

    Experimental templates are not gated (no v1 probe matrix).
    """
    template = dev_type.harness_template
    if template not in LAUNCH_SUPPORTED:
        return
    digest = SENTINEL_DIGEST if digest is None else digest
    if digest == SENTINEL_DIGEST:
        raise HarnessNotStaffed(
            "this app was built without the bake wrapper",
            kind="sentinel")
    version = effective_cli_version(dev_type)
    rec = None if store is None else store.get(
        digest=digest, template=template, cli_version=version)
    if rec is None:
        raise HarnessNotStaffed(
            f"no receipt for {template} {version}", kind="missing")
    if rec.get("gated") is False:
        raise HarnessNotStaffed(
            f"{template} {version} receipt is not gated", kind="fabricated")
    if rec.get("ok") is True:
        return
    row = _first_unpassed_required(rec)
    named = f" ({row})" if row else ""
    raise HarnessNotStaffed(
        f"{template} {version} receipt is not ok{named}",
        row=row, kind="receipt")


def _first_unpassed_required(rec: Mapping[str, Any]) -> str | None:
    for row in rec.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("required") and row.get("status") != "pass":
            name = row.get("name")
            return str(name) if name else None
    return None


def receipt_summary(dev_types: Mapping[str, Any], *, digest: str,
                    store, bake_status: Mapping[str, Any] | None = None) -> dict:
    """Per-template staffing view for /health (and the editor copy)."""
    templates = sorted({dt.harness_template for dt in dev_types.values()})
    rows = {}
    for template in templates:
        # One row per template using the first Dev Type's effective pin
        # (unique pins also appear under dev_types).
        version = next(
            (effective_cli_version(dt) for dt in dev_types.values()
             if dt.harness_template == template),
            HOUSE_PINS.get(template, ""))
        rows[template] = _pin_entry(
            template, version, digest, store, bake_status)
    per_dt = {}
    for name, dt in sorted(dev_types.items()):
        template = dt.harness_template
        version = effective_cli_version(dt)
        entry = _pin_entry(template, version, digest, store, bake_status)
        entry["template"] = template
        entry["house"] = not bool((getattr(dt, "cli_version", "") or "").strip())
        per_dt[name] = entry
    return {
        "digest": digest,
        "sentinel": digest == SENTINEL_DIGEST,
        "templates": rows,
        "dev_types": per_dt,
    }


def _job_state(template: str, version: str,
               bake_status: Mapping[str, Any] | None) -> str | None:
    if not bake_status:
        return None
    for job in bake_status.get("jobs") or []:
        if not isinstance(job, Mapping):
            continue
        if job.get("template") == template and job.get("cli_version") == version:
            state = job.get("state")
            return str(state) if state else None
    return None


def _pin_entry(template: str, version: str, digest: str, store,
               bake_status: Mapping[str, Any] | None = None) -> dict:
    entry: dict[str, Any] = {
        "cli_version": version,
        "gated": template in LAUNCH_SUPPORTED,
    }
    if template not in LAUNCH_SUPPORTED:
        entry["ok"] = None
        entry["state"] = "experimental"
        entry["reason"] = "experimental — house pin only"
        return entry
    job_state = _job_state(template, version, bake_status)
    fake = SimpleNamespace(harness_template=template, cli_version=version)
    try:
        require_staffed(fake, digest=digest, store=store)
    except HarnessNotStaffed as exc:
        entry["ok"] = False
        entry["reason"] = str(exc)
        entry["row"] = exc.row
        if job_state in ("baking", "pending"):
            entry["state"] = "baking"
            entry["reason"] = f"{template} {version} is baking on the host"
        elif job_state == "error" or (bake_status or {}).get("state") == "error":
            entry["state"] = "error"
            entry["reason"] = (bake_status or {}).get("detail") or str(exc)
        elif exc.kind == "sentinel":
            entry["state"] = "error"
        elif exc.kind == "missing":
            entry["state"] = "waiting"
        else:
            entry["state"] = "error"
        return entry
    entry["ok"] = True
    entry["state"] = "ready"
    return entry
