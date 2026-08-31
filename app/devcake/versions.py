"""Resolve-once `latest` gesture. Never stored; never called on editor open.

Also owns the CLI-version semver shape (ADR-0034 / CAKE-87) — keep_set,
config DevType.cli_version, and the host factory import this constant.

Module scope must stay stdlib-only: the host baker (`devcake baker run`,
or its deprecated `python3 -m dev_factory` alias — host python, no venv)
imports this module for the semver constant. The harness registry needs pydantic, which a host python
does not have — resolve_latest imports it at call time (app-side only).
"""

from __future__ import annotations

import re

from .house_pins import LAUNCH_SUPPORTED

# ONE CLI pin shape: major.minor.patch with optional pre-release suffix.
CLI_VERSION_SEMVER = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?"
CLI_VERSION_SEMVER_RE = re.compile(CLI_VERSION_SEMVER)


def resolve_latest(template: str, *, source) -> str:
    """Look up the remote semver. Unknown ids refuse."""
    from .harness import HARNESSES
    if template not in HARNESSES:
        raise ValueError(f"unknown harness {template!r}")
    if template not in LAUNCH_SUPPORTED:
        raise ValueError(f"unknown harness {template!r}")
    pin = (source.latest(template) or "").strip()
    if not pin:
        raise ValueError(f"no latest version for {template}")
    if not CLI_VERSION_SEMVER_RE.fullmatch(pin):
        raise ValueError(f"remote version is not a semver: {pin!r}")
    return pin
