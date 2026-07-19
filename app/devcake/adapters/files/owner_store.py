"""Cross-instance mission-ownership persistence (audit A15): pmo_id → owning
instance name, one JSON file at /data/state/mission_owner.json.

In-memory-only ownership reopened the duplicate-dispatch window on every app
restart: a shared mission claimed and dispatched by instance A could be
claimed by instance B (earlier in config order) on the first post-restart
cycle — A's active run is invisible to B's in-flight guard, which filters on
its own pmo_ref. Atomic writes via tmp + fsync + rename (the RunStore idiom);
an unreadable file degrades to empty and never wedges boot.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("devcake.ownerstore")


def _default_path() -> Path:
    return (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
            / "state" / "mission_owner.json")


class OwnerStore:
    def __init__(self, path: Path | None = None):
        self.path = path or _default_path()

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
            return {str(k): str(v) for k, v in data.items()}
        except Exception:  # noqa: BLE001 — unreadable owner store starts empty (logged); ownership is advisory
            log.error("unreadable owner store %s — starting empty (a shared "
                      "mission may need its duplicate-claim anomaly resolved "
                      "by hand)", self.path)
            return {}

    def save(self, owner: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(owner, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
