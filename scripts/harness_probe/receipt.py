"""Row-level receipt: run report + ok from the matrix, not from the run."""

from __future__ import annotations

from typing import Any, Mapping

from .matrix import RowSpec, matrix_for


def compile_receipt(
    *,
    digest: str,
    template: str,
    cli_version: str,
    reports: Mapping[str, Mapping[str, Any]],
) -> dict:
    """Grade `reports` against the matrix. Always emits every matrix row.

    A report is one of:
      {"observed": {exit, class, reason}}
      {"skipped": "why"}
      {"error": "why"}
    Missing report on a required row → status error (absent).
    """
    specs = matrix_for(template)
    rows = [_grade(spec, reports.get(spec.name)) for spec in specs]
    return {
        "digest": digest,
        "template": template,
        "cli_version": cli_version,
        "rows": rows,
        "ok": _ok(specs, rows),
    }


def receipt_ok(receipt: Mapping[str, Any]) -> bool:
    """Reader-side: required matrix rows must be present and status==pass."""
    try:
        specs = matrix_for(str(receipt.get("template") or ""))
    except ValueError:
        return False
    by_name = {r.get("name"): r for r in (receipt.get("rows") or [])
               if isinstance(r, Mapping)}
    return _ok(specs, list(by_name.values()) if by_name else [], by_name=by_name)


def _ok(
    specs: tuple[RowSpec, ...],
    rows: list[dict],
    by_name: Mapping[str, Mapping] | None = None,
) -> bool:
    index = by_name if by_name is not None else {r["name"]: r for r in rows}
    for spec in specs:
        if not spec.required:
            continue
        row = index.get(spec.name)
        if row is None or row.get("status") != "pass":
            return False
    return True


def _grade(spec: RowSpec, report: Mapping[str, Any] | None) -> dict:
    row: dict[str, Any] = {
        "name": spec.name,
        "required": spec.required,
    }
    if spec.expected is not None:
        row["expected"] = dict(spec.expected)
    if report is None:
        if spec.required:
            row["status"] = "error"
            row["detail"] = "absent"
        else:
            row["status"] = "skipped"
            row["detail"] = spec.skip_reason or "not required"
        return row
    if "skipped" in report:
        row["status"] = "skipped"
        row["detail"] = str(report["skipped"])
        return row
    if "error" in report:
        row["status"] = "error"
        row["detail"] = str(report["error"])
        return row
    if spec.kind == "flag_accept":
        accepted = report.get("flag_accepted")
        if accepted is True:
            row["status"] = "pass"
        elif accepted is False:
            row["status"] = "fail"
            row["detail"] = "flag rejected"
        else:
            row["status"] = "error"
            row["detail"] = "missing flag_accepted"
        return row
    observed = report.get("observed")
    if not isinstance(observed, Mapping):
        row["status"] = "error"
        row["detail"] = "missing observed"
        return row
    triple = {
        "exit": observed.get("exit"),
        "class": observed.get("class"),
        "reason": observed.get("reason"),
    }
    row["observed"] = triple
    if spec.expected is not None and triple == spec.expected:
        row["status"] = "pass"
    else:
        row["status"] = "fail"
    return row
