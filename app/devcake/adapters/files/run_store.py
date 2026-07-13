"""Run record persistence: one JSON file per run under /data/state/runs
(docs/10). Advisory telemetry only (INV-1) — wiping /data/state never corrupts
mission state. Atomic writes via tmp + fsync + rename."""

import os
import tempfile
from pathlib import Path
from typing import Optional

from ...domain.run import Run

RUNS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "runs"


class RunStore:
    def __init__(self, root: Path = RUNS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, run: Run) -> None:
        path = self.root / f"{run.run_id}.json"
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(run.model_dump_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, run_id: str) -> Optional[Run]:
        path = self.root / f"{run_id}.json"
        if not path.exists():
            return None
        return Run.model_validate_json(path.read_text())

    def all(self) -> list[Run]:
        runs = []
        for p in sorted(self.root.glob("*.json")):
            try:
                runs.append(Run.model_validate_json(p.read_text()))
            except Exception:
                continue  # unreadable file: skip, never crash the loop
        return runs

    def active(self) -> list[Run]:
        return [r for r in self.all() if r.state in ("dispatched", "running", "finalizing")]

    def clear(self) -> int:
        """Delete every run record. Returns how many files were removed."""
        n = 0
        for p in self.root.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                continue
        return n
