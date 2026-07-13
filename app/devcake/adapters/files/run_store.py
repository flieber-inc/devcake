"""Run record persistence: one JSON file per run under /data/state/runs
(docs/10). Advisory telemetry only (INV-1) — wiping /data/state never corrupts
mission state. Atomic writes via tmp + fsync + rename."""

import os
import json
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

    def _quarantine(self, path: Path, why: str) -> None:
        """Move an unreadable record aside so it can never wedge startup.

        Bytes are preserved (they may contain secret fragments — hence the
        restrictive modes) but the file leaves the non-recursive *.json glob,
        so it disappears from all(), the API, and the post-scrub check.
        """
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

    @staticmethod
    def _sensitive_env_name(name: str) -> bool:
        upper = name.upper()
        return any(part in upper for part in (
            "TOKEN", "PASSWORD", "SECRET", "API_KEY", "AUTHORIZATION",
            "OTLP_BASIC",
        ))

    def scrub_legacy_secrets(self) -> list[Run]:
        """Rewrite v1 records without credentials; return active legacy runs.

        Active v1 runs cannot safely be adopted because their short-lived
        Redis run-spec record never existed. Startup aborts the returned runs.
        """
        from ...security import redact_value

        active: list[Run] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(path.read_text())
                if not isinstance(raw, dict):
                    raise ValueError("run record is not a JSON object")
            except Exception as e:
                self._quarantine(path, f"invalid JSON: {e}")
                continue
            legacy = int(raw.get("schema_version", 1)) < 2
            has_secrets = bool(raw.get("redis_password") or raw.get("spec_files"))
            env = dict(raw.get("spec_env") or {})
            filtered = {k: str(v) for k, v in env.items()
                        if not self._sensitive_env_name(k)}
            has_secrets = has_secrets or len(filtered) != len(env)
            if not (legacy or has_secrets):
                continue
            raw["schema_version"] = 2
            raw.pop("redis_password", None)
            raw.pop("spec_files", None)
            raw["spec_env"] = filtered
            raw["result"] = redact_value(raw.get("result"))
            raw["token_report"] = redact_value(raw.get("token_report"))
            try:
                run = Run.model_validate(raw)
            except Exception as e:
                # Secrets are gone either way: persist the scrubbed raw record
                # even when it no longer fits the model.
                self._write_text(path, json.dumps(raw, indent=2, default=str))
                log.warning("run record %s scrubbed but not model-valid (%s)%s",
                            path.name, e,
                            "; active run could not be aborted"
                            if raw.get("state") in ("dispatched", "running", "finalizing")
                            else "")
                continue
            if run.state in ("dispatched", "running", "finalizing"):
                active.append(run)
            self.save(run)
        return active

    def legacy_secrets_remaining(self) -> list[str]:
        """Names of run files still carrying credentials after the scrub."""
        offending: list[str] = []
        for path in self.root.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except Exception:
                offending.append(path.name)
                continue
            if not isinstance(raw, dict):
                offending.append(path.name)
                continue
            if raw.get("redis_password") or raw.get("spec_files"):
                offending.append(path.name)
                continue
            if any(self._sensitive_env_name(k) for k in (raw.get("spec_env") or {})):
                offending.append(path.name)
        return offending

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
