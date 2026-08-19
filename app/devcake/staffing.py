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
                    store, baker_alive: bool | None = None) -> None:
    """Raise HarnessNotStaffed unless a matching ok receipt exists."""
    if baker_alive is None:
        from .bake_status import baker_liveness, read_bake_status
        # Always evaluate liveness. Missing/never-checked-in heartbeat is
        # dead (baker_liveness already says so) — do not skip and staff.
        baker_alive = bool(baker_liveness(read_bake_status()).get("alive"))
    if baker_alive is False:
        raise HarnessNotStaffed(
            "host baker is not running — cannot vouch for images",
            kind="baker")
    template = dev_type.harness_template
    if template not in LAUNCH_SUPPORTED:
        raise HarnessNotStaffed(
            f"no receipt for {template}", kind="missing")
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
    # gated must be True — absence or null is fabricated (fail-closed).
    if rec.get("gated") is not True:
        raise HarnessNotStaffed(
            f"{template} {version} receipt is not gated", kind="fabricated")
    if rec.get("ok") is True:
        return
    row = _first_unpassed_required(rec)
    raise HarnessNotStaffed(
        f"{template} {version} receipt is not ok: {receipt_fail_reason(rec)}",
        row=row, kind="receipt")


def receipt_fail_reason(rec: Mapping[str, Any]) -> str:
    """Required rows that did not pass — shared with the baker's detail."""
    bits: list[str] = []
    for row in rec.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        if not row.get("required") or row.get("status") == "pass":
            continue
        name = str(row.get("name") or "?")
        status = str(row.get("status") or "fail")
        extra = str(row.get("detail") or "").strip()
        bits.append(f"{name} {status} ({extra})" if extra else f"{name} {status}")
    return "; ".join(bits) if bits else "receipt not ok"


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
