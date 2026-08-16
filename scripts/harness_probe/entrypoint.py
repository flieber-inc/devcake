"""Load the image's baked entrypoint. Never the capture-rig /srv tree."""

from __future__ import annotations

import importlib.util
from pathlib import Path

BAKED_ENTRYPOINT = "/dev_entrypoint.py"


def load_baked_entrypoint(path: str = BAKED_ENTRYPOINT):
    """Import the entrypoint at `path`. Refuse the working-tree bind."""
    posix = Path(path).as_posix()
    if posix.endswith("images/common/dev_entrypoint.py") or "/srv/images/" in posix:
        raise ValueError(
            "probe grades the baked entrypoint only, not the working tree")
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"no baked entrypoint at {path}")
    spec = importlib.util.spec_from_file_location("ep_probe", src)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load entrypoint from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
