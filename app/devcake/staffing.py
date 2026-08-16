"""Is this Dev Type staffable for the running app digest?

Chokepoint for Slice 2: dispatch, steward, and OAuth call require_staffed
before bootstrap.launch. Hello never does. Domain depends on ReceiptStore
(port), not the file adapter.
"""

from __future__ import annotations

from typing import Any, Mapping

from .house_pins import (
    HOUSE_PINS,
    LAUNCH_SUPPORTED,
    SENTINEL_DIGEST,
    app_digest,
    effective_cli_version,
)


class HarnessNotStaffed(Exception):
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
                    store) -> dict:
    """Per-template staffing view for /health (and Slice 3 copy)."""
    templates = sorted({dt.harness_template for dt in dev_types.values()})
    rows = {}
    for template in templates:
        # One row per template using the first Dev Type's effective pin
        # (unique pins also appear under dev_types).
        version = next(
            (effective_cli_version(dt) for dt in dev_types.values()
             if dt.harness_template == template),
            HOUSE_PINS.get(template, ""))
        rows[template] = _pin_entry(template, version, digest, store)
    per_dt = {}
    for name, dt in sorted(dev_types.items()):
        template = dt.harness_template
        version = effective_cli_version(dt)
        entry = _pin_entry(template, version, digest, store)
        entry["template"] = template
        entry["house"] = not bool((getattr(dt, "cli_version", "") or "").strip())
        per_dt[name] = entry
    return {
        "digest": digest,
        "sentinel": digest == SENTINEL_DIGEST,
        "templates": rows,
        "dev_types": per_dt,
    }


def bake_command(template: str, version: str) -> str:
    return f"bash scripts/harness_probe/host_probe.sh {template} {version}"


def _pin_entry(template: str, version: str, digest: str, store) -> dict:
    entry: dict[str, Any] = {
        "cli_version": version,
        "gated": template in LAUNCH_SUPPORTED,
        "command": bake_command(template, version),
    }
    if template not in LAUNCH_SUPPORTED:
        entry["ok"] = None
        entry["reason"] = "experimental — house pin only"
        return entry
    if digest == SENTINEL_DIGEST:
        entry["ok"] = False
        entry["reason"] = "this app was built without the bake wrapper"
        return entry
    rec = None if store is None else store.get(
        digest=digest, template=template, cli_version=version)
    if rec is None:
        entry["ok"] = False
        entry["reason"] = f"no receipt for {template} {version}"
        return entry
    entry["ok"] = rec.get("ok") is True
    if not entry["ok"]:
        row = _first_unpassed_required(rec)
        entry["row"] = row
        entry["reason"] = (
            f"{template} {version} receipt is not ok"
            + (f" ({row})" if row else ""))
    return entry
