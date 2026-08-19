"""App-published keep-set for the host bake verb.

Host scripts read this file — never Dev Type YAML. Concrete pins are
the baking set; the host factory validates them independently.

Contract (pins only): ``{"pins": [{"template": <id>, "cli_version": <semver>}]}``.
No free-form image strings. Publish re-validates against house pins so a
caller object cannot smuggle an unknown template into the file.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .versions import CLI_VERSION_SEMVER_RE as _SEMVER

KEEP_SET_NAME = "harness_keep_set.json"


def publish_keep_set(dev_types: dict, *, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(
        os.environ.get("DEVCAKE_DATA_DIR", "/data"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / KEEP_SET_NAME
    from .house_pins import HOUSE_PINS, effective_cli_version

    seen: set[tuple[str, str]] = set()
    pins: list[dict] = []
    for dt in sorted(dev_types.values(),
                     key=lambda d: (getattr(d, "harness_template", ""),
                                    effective_cli_version(d))):
        template = getattr(dt, "harness_template", None)
        version = effective_cli_version(dt)
        if not isinstance(template, str) or not template:
            raise ValueError("keep-set pin is missing harness_template")
        if template not in HOUSE_PINS:
            raise ValueError(
                f"keep-set refuses unknown template {template!r}")
        if not isinstance(version, str) or not version:
            raise ValueError(
                f"keep-set pin {template!r} has no effective cli_version")
        if version.lower() == "latest" or not _SEMVER.fullmatch(version):
            raise ValueError(
                f"keep-set cli_version must be a semver, got {version!r}")
        pair = (template, version)
        if pair in seen:
            continue
        seen.add(pair)
        # Never emit docker_image / free-form image fields — pins only.
        pins.append({"template": pair[0], "cli_version": pair[1]})
    text = json.dumps({"pins": pins}, indent=2) + "\n"
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
