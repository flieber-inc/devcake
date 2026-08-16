"""App-published keep-set for the host bake verb.

Host scripts read this file — never Dev Type YAML. Concrete pins are
the baking set; the host factory validates them independently.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

KEEP_SET_NAME = "harness_keep_set.json"


def publish_keep_set(dev_types: dict, *, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(
        os.environ.get("DEVCAKE_DATA_DIR", "/data"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / KEEP_SET_NAME
    from .house_pins import effective_cli_version

    templates = sorted({dt.harness_template for dt in dev_types.values()})
    seen: set[tuple[str, str]] = set()
    pins: list[dict] = []
    for dt in sorted(dev_types.values(),
                     key=lambda d: (d.harness_template,
                                    effective_cli_version(d))):
        pair = (dt.harness_template, effective_cli_version(dt))
        if pair in seen or not pair[1]:
            continue
        seen.add(pair)
        pins.append({"template": pair[0], "cli_version": pair[1]})
    text = json.dumps({"templates": templates, "pins": pins}, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=base, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
