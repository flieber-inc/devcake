"""Probe command: run the matrix, write a row-level receipt, exit on ok."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from .matrix import RowSpec, matrix_for
from .receipt import compile_receipt

ReceiptDir = Path
RunRow = Callable[[RowSpec], dict]


def receipt_path(out_dir: Path, template: str, cli_version: str) -> Path:
    return Path(out_dir) / f"{template}@{cli_version}.json"


def run_probe(
    *,
    digest: str,
    template: str,
    cli_version: str,
    out_dir: Path,
    run_row: RunRow,
) -> tuple[dict, int]:
    reports: dict[str, dict] = {}
    for spec in matrix_for(template):
        try:
            reports[spec.name] = run_row(spec)
        except Exception as exc:  # noqa: BLE001 — row error, not a skip
            reports[spec.name] = {"error": f"{type(exc).__name__}: {exc}"}
    rec = compile_receipt(
        digest=digest,
        template=template,
        cli_version=cli_version,
        reports=reports,
    )
    dest = receipt_path(out_dir, template, cli_version)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(rec, indent=2) + "\n")
    return rec, (0 if rec["ok"] else 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", required=True)
    ap.add_argument("--cli-version", required=True)
    ap.add_argument("--digest", required=True)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    from .live import run_live_row
    rec, code = run_probe(
        digest=args.digest,
        template=args.template,
        cli_version=args.cli_version,
        out_dir=args.out,
        run_row=lambda spec: run_live_row(args.template, spec),
    )
    print(json.dumps({"ok": rec["ok"], "path": str(
        receipt_path(args.out, args.template, args.cli_version))}))
    return code


if __name__ == "__main__":
    sys.exit(main())
