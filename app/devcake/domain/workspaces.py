"""Per-run workspace directories on the host-bind tree (ADR-0025).

The app owns the whole lifecycle: pre-create + sentinel at dispatch (inside
RunBootstrap.launch, AFTER the durable save — record-before-dir makes "dir
without a record" always garbage), best-effort cleanup at every terminal
hook, and a sweep that guarantees reclamation. Best-effort + sweep is the
doctrine, never a synchronous invariant: a kill can race a container that
holds the bind through Dagu's stop grace (~35 s), and dockerd re-creates an
absent bind source root-owned — so nothing here ever deletes between
executor.start and a terminal state, and nothing here ever raises out of a
teardown path.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger("devcake.workspaces")

# Same charset as make_run_id output (docs/02 §7); the fence keeps the sweep
# and the clear-runs wipe from ever touching an operator's stray file if the
# base is misconfigured onto a shared directory (ADR-0025 R10).
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SENTINEL_REL = ".devcake/created-by-app"
TERMINAL_STATES = ("finished", "failed", "timed_out", "orphaned")
SWEEP_AGE_SECONDS = 600  # belt-and-suspenders on top of record-before-dir


class WorkspaceStore:
    """Modeled on RepoCache (ADR-0024): plain filesystem methods, a boot
    writability probe, /health surfaces. All methods are synchronous and
    cheap; cleanup/sweep never raise."""

    def __init__(self, root: Path | str = "/workspaces"):
        self.root = Path(root)
        self.volume_error: str | None = None

    # ── path discipline ──────────────────────────────────────────────────────

    def _path(self, run_id: str) -> Path | None:
        """Charset-validated, lexically-contained child path — or None.
        Defense-in-depth: every real caller passes a make_run_id value."""
        if not RUN_ID_RE.match(run_id or ""):
            return None
        p = self.root / run_id
        return p if p.parent == self.root else None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def create(self, run_id: str) -> Path:
        """Pre-create this run's dir + sentinel (Hook C). exist_ok=False on
        purpose: a pre-existing dir means a collision or a daemon-created
        husk — loud beats adopted. Raises on any failure (the caller's
        dispatch gates the mission); a partial create is removed first."""
        p = self._path(run_id)
        if p is None:
            raise ValueError(f"invalid run id for workspace: {run_id!r}")
        try:
            p.mkdir(parents=False, exist_ok=False)
            os.chmod(p, 0o700)          # mkdir mode is umask-masked (R2)
            d = p / ".devcake"
            d.mkdir(exist_ok=False)
            os.chmod(d, 0o700)
            # R4: provision's first act verifies this names ITS run — one
            # check closes daemon-autocreate AND silent WS_HOST base drift
            (p / SENTINEL_REL).write_text(run_id)
        except OSError:
            self.cleanup(run_id)
            raise
        return p

    def cleanup(self, run_id: str) -> bool:
        """Best-effort removal (Hooks A/B/D/F). Never raises; a partial
        removal (dying container still writing, Dev-made unreadable subdir)
        is the sweep's job. True iff the dir is gone afterwards."""
        p = self._path(run_id)
        if p is None:
            return False
        return self._remove(p)

    def _remove(self, p: Path) -> bool:
        if not os.path.lexists(p):
            return False
        try:
            if not p.is_dir() or p.is_symlink():
                # never rmtree through a link; a stray file/link is unlinked
                p.unlink(missing_ok=True)
                return not p.exists()
        except OSError:
            return False
        try:
            shutil.rmtree(p)
            return True
        except OSError:
            # Dev-created mode-0000 subdir etc.: we own everything (uid
            # 1000), so chmod-and-retry clears what a plain walk cannot
            with contextlib.suppress(OSError):
                for dirpath, dirnames, _files in os.walk(p):
                    for d in [dirpath] + [os.path.join(dirpath, x)
                                          for x in dirnames]:
                        with contextlib.suppress(OSError):
                            os.chmod(d, 0o700)
            shutil.rmtree(p, ignore_errors=True)
            return not p.exists()

    def _candidates(self, store) -> list[os.DirEntry]:
        """Sweep-eligible children: run-id charset, older than the age
        guard, and record-absent-or-terminal. lstat only — symlinks are
        never followed (R10)."""
        out: list[os.DirEntry] = []
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return out
        now = time.time()
        for e in entries:
            if not RUN_ID_RE.match(e.name):
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if now - st.st_mtime < SWEEP_AGE_SECONDS:
                continue
            run = store.get(e.name)
            if run is not None and run.state not in TERMINAL_STATES:
                continue
            out.append(e)
        return out

    def sweep(self, store) -> int:
        """Hook G — the reclamation guarantee behind every best-effort hook.
        Runs at boot (reconcile, before anything serves) and periodically
        from the watchdog. Never raises."""
        removed = 0
        for e in self._candidates(store):
            try:
                if not e.is_dir(follow_symlinks=False):
                    with contextlib.suppress(OSError):
                        os.unlink(e.path)
                        removed += 1
                    continue
                if self._remove(Path(e.path)):
                    removed += 1
            except OSError:  # noqa: PERF203 — per-entry isolation on purpose
                continue
        if removed:
            log.info("workspace sweep removed %d leaked entr%s",
                     removed, "y" if removed == 1 else "ies")
        return removed

    def wipe_all(self) -> int:
        """Hook E (clear-runs, inside the dispatch_lock wrap): the records
        are being wiped, so every run-id-shaped child goes — no age guard,
        no store lookup. Undrained containers keep writing into a lazily
        detached mount that dies with them (ADR-0025 C3)."""
        removed = 0
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return 0
        for e in entries:
            if not RUN_ID_RE.match(e.name):
                continue
            if e.is_dir(follow_symlinks=False):
                if self._remove(Path(e.path)):
                    removed += 1
            else:
                with contextlib.suppress(OSError):
                    os.unlink(e.path)
                    removed += 1
        return removed

    # ── hygiene / surfaces ───────────────────────────────────────────────────

    def verify_writable(self) -> None:
        """Boot probe, no network: is the workspace base there and
        app-writable? First-touched-by-dockerd (or a uid≠1000 operator
        mkdir) means root-owned and every dispatch would fail confusingly —
        /health names the one-line fix (docs/13 §8)."""
        probe = self.root / ".devcake-writable"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_text("")
            probe.unlink()
            self.volume_error = None
        except OSError as e:
            self.volume_error = (
                f"{type(e).__name__}: {e} — the workspace base is not "
                f"writable by the app (uid 1000). Fix on the host: "
                f"`chown -R 1000:1000 $DEVCAKE_WS_HOST && chmod 700 "
                f"$DEVCAKE_WS_HOST` (docs/13 §8), then restart the app.")
            log.error("workspace base unusable: %s", self.volume_error)

    def leaked_count(self, store) -> int:
        """Sweep candidates that still exist — non-zero means hooks AND the
        sweep are losing to something; alerts.js warns on it."""
        return len(self._candidates(store))

    def disk_stats(self) -> dict | None:
        try:
            sv = os.statvfs(self.root)
            return {"total_bytes": sv.f_frsize * sv.f_blocks,
                    "free_bytes": sv.f_frsize * sv.f_bavail}
        except OSError:
            return None


class NullWorkspaceStore:
    """Test/default stand-in (NullRepoCache precedent): every hook is a
    no-op, so the existing suite's RunManager/RunBootstrap constructions
    stay untouched."""

    def __init__(self):
        self.volume_error = None
        self.root = Path("/workspaces")

    def create(self, run_id: str) -> Path:
        return self.root / run_id

    def cleanup(self, run_id: str) -> bool:
        return False

    def sweep(self, store) -> int:
        return 0

    def wipe_all(self) -> int:
        return 0

    def verify_writable(self) -> None:
        return None

    def leaked_count(self, store) -> int:
        return 0

    def disk_stats(self) -> dict | None:
        return None
