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
    version = HOUSE_PINS[template]
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
        version = HOUSE_PINS.get(template, "")
        entry: dict[str, Any] = {
            "cli_version": version,
            "gated": template in LAUNCH_SUPPORTED,
        }
        if template not in LAUNCH_SUPPORTED:
            entry["ok"] = None
            entry["reason"] = "experimental — house pin only"
            rows[template] = entry
            continue
        if digest == SENTINEL_DIGEST:
            entry["ok"] = False
            entry["reason"] = "this app was built without the bake wrapper"
            rows[template] = entry
            continue
        rec = None if store is None else store.get(
            digest=digest, template=template, cli_version=version)
        if rec is None:
            entry["ok"] = False
            entry["reason"] = f"no receipt for {template} {version}"
            rows[template] = entry
            continue
        entry["ok"] = rec.get("ok") is True
        if not entry["ok"]:
            row = _first_unpassed_required(rec)
            entry["row"] = row
            entry["reason"] = (
                f"{template} {version} receipt is not ok"
                + (f" ({row})" if row else ""))
        rows[template] = entry
    return {
        "digest": digest,
        "sentinel": digest == SENTINEL_DIGEST,
        "templates": rows,
    }
