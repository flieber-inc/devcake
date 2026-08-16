"""Resolve-once `latest` gesture. Never stored; never called on editor open."""

from __future__ import annotations

import re

from .house_pins import LAUNCH_SUPPORTED
from .harness import HARNESSES

_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.]+)?")


def resolve_latest(template: str, *, source) -> str:
    """Look up the remote semver. Experimental and unknown ids refuse."""
    if template not in HARNESSES:
        raise ValueError(f"unknown harness {template!r}")
    if template not in LAUNCH_SUPPORTED or HARNESSES[template].experimental:
        raise ValueError(f"{template} is experimental — house-pin only")
    pin = (source.latest(template) or "").strip()
    if not pin:
        raise ValueError(f"no latest version for {template}")
    if not _SEMVER.fullmatch(pin):
        raise ValueError(f"remote version is not a semver: {pin!r}")
    return pin
