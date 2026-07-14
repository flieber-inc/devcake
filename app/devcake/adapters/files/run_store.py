"""Run record persistence: one JSON file per run under /data/state/runs
(docs/10). Advisory telemetry only (INV-1) — wiping /data/state never corrupts
mission state. Atomic writes via tmp + fsync + rename."""

import json
import os
import logging
import tempfile
from pathlib import Path
from typing import Optional

from ...domain.run import Run

RUNS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "runs"

log = logging.getLogger("devcake.runstore")


class RunStore:
    def __init__(self, root: Path = RUNS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _write_text(self, path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def save(self, run: Run) -> None:
        self._write_text(self.root / f"{run.run_id}.json", run.model_dump_json(indent=2))

    @staticmethod
    def _sensitive_env_name(name: str) -> bool:
        upper = name.upper()
        return any(part in upper for part in (
            "TOKEN", "PASSWORD", "SECRET", "API_KEY", "AUTHORIZATION",
            # dead in M8+ runspecs, kept for the quarantine scrub: legacy
            # (v1-era) records may persist OTEL_EXPORTER_OTLP_BASIC — a
            # base64 OO login that must never land in quarantine/ plaintext
            "OTLP_BASIC",
        ))

    def _quarantine(self, path: Path, why: str) -> None:
        """Move an unreadable record aside so it can never wedge startup.

        A record that still parses as JSON is scrubbed of known
        credential-bearing fields first — quarantine must not become
        secret-at-rest (docs/14, docs/10 §5). An unparseable file keeps its
        bytes (nothing to scrub selectively — hence the restrictive modes).
        Either way the file leaves the non-recursive *.json glob, so it
        disappears from all() and the API.
        """
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                raw.pop("redis_password", None)
                raw.pop("spec_files", None)
                env = raw.get("spec_env")
                if isinstance(env, dict):
                    raw["spec_env"] = {k: v for k, v in env.items()
                                       if not self._sensitive_env_name(k)}
                self._write_text(path, json.dumps(raw, indent=2, default=str))
        except Exception:
            pass  # not valid JSON: preserve the bytes under 0600
        qdir = self.root / "quarantine"
        qdir.mkdir(mode=0o700, exist_ok=True)
        dest = qdir / path.name
        os.replace(path, dest)
        os.chmod(dest, 0o600)
        log.error("run record %s unreadable (%s) — quarantined to %s", path.name, why, dest)

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

    def quarantine_unreadable(self) -> list[str]:
        """Boot-time integrity sweep: move every unparseable, model-invalid,
        or pre-v2 run record to quarantine/ so it can never wedge startup or
        hide silently inside all(). The version floor is a security tripwire,
        not a migration: v1 records persisted credentials, and the v1→v2
        scrub was removed at v0 (docs/10 §5).

        Returns the quarantined run ids (file stems) so the caller can tear
        down anything the forgotten record may have left live (a running
        container, a per-run Redis ACL user, a reply stream)."""
        moved: list[str] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                run = Run.model_validate_json(path.read_text())
                if run.schema_version < 2:
                    raise ValueError("pre-v2 run record (may carry credentials)")
            except Exception as e:
                self._quarantine(path, str(e))
                moved.append(path.stem)
        return moved

    def clear(self) -> int:
        """Delete every run record. Returns how many files were removed."""
        n = 0
        for p in list(self.root.glob("*.json")) + list(self.root.glob("quarantine/*.json")):
            try:
                p.unlink()
                n += 1
            except OSError:
                continue
        return n
