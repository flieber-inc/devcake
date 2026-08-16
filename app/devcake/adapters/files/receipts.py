"""Read row-level harness receipts written by the host bake verb.

Path: {root}/{template}@{cli_version}.json. Digest is inside the file;
a digest mismatch is a miss (this tree's receipts only).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

RECEIPTS_DIR = (
    Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "harness_receipts"
)


class FileReceiptStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else RECEIPTS_DIR

    def get(self, *, digest: str, template: str,
            cli_version: str) -> Mapping[str, Any] | None:
        path = self.root / f"{template}@{cli_version}.json"
        if not path.is_file():
            return None
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(rec, dict):
            return None
        if rec.get("digest") != digest:
            return None
        return rec
