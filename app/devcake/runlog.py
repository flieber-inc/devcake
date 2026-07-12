"""Per-run live log store: one append-only text file per run under
/data/state/runlogs, plus in-process pub/sub for SSE followers (docs/11 §2a).

Advisory telemetry (INV-1) — wiping runlogs never corrupts mission state.
The pub/sub is in-memory and assumes the single-worker uvicorn deployment
(app/Dockerfile): the ingress consumer and the SSE endpoints share one event
loop, so append() → put_nowait() needs no cross-process fan-out.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Callable

log = logging.getLogger("devcake.runlog")

RUNLOGS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "runlogs"
MAX_LOG_BYTES = 10 * 1024 * 1024   # per-run cap: appends become no-ops past it
QUEUE_SIZE = 1000                  # slow SSE clients drop lines, never backpressure
HEARTBEAT_SECONDS = 15             # SSE keepalive (< nginx's 60s proxy_read_timeout)
CAP_MARKER = "[log capped at 10MB — see Dagu step log for the full output]"


class RunLogStore:
    def __init__(self, root: Path = RUNLOGS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._seq: dict[str, int] = {}                    # lines written, per run
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._capped: set[str] = set()

    def path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.log"

    def _init_seq(self, run_id: str) -> int:
        """Lazy counter init from the file (adopted runs after an app restart)."""
        if run_id not in self._seq:
            p = self.path(run_id)
            self._seq[run_id] = (
                p.read_text(errors="replace").count("\n") if p.exists() else 0)
        return self._seq[run_id]

    def append(self, run_id: str, lines: list[str]) -> None:
        """File append + publish. Runs entirely on the event loop (no awaits
        between write and publish), so seq ordering is race-free."""
        if not lines:
            return
        # one queue item == one SSE data: frame — embedded newlines would break
        # the framing, so split them here
        lines = [part for l in lines for part in (str(l).splitlines() or [""])]
        self._init_seq(run_id)
        p = self.path(run_id)
        if run_id in self._capped:
            return
        if p.exists() and p.stat().st_size >= MAX_LOG_BYTES:
            self._capped.add(run_id)
            lines = [CAP_MARKER]
        with p.open("a") as f:
            for line in lines:
                f.write(line.rstrip("\n") + "\n")
        for line in lines:
            self._seq[run_id] += 1
            seq = self._seq[run_id]
            for q in self._subs.get(run_id, set()):
                try:
                    q.put_nowait((seq, line))
                except asyncio.QueueFull:
                    pass  # slow client: drop, never block the ingress consumer

    def read(self, run_id: str, tail: int | None = None) -> tuple[int, str]:
        """Return (seq_now, text). tail=N keeps only the last N lines."""
        seq = self._init_seq(run_id)
        p = self.path(run_id)
        if not p.exists():
            return seq, ""
        text = p.read_text(errors="replace")
        if tail is not None and tail >= 0:
            text = "\n".join(text.splitlines()[-tail:] if tail else [])
            if text:
                text += "\n"
        return seq, text

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(run_id)
        if subs:
            subs.discard(q)
            if not subs:
                del self._subs[run_id]

    def close(self, run_id: str) -> None:
        """Terminal state reached: wake every follower with the end sentinel."""
        for q in self._subs.get(run_id, set()):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass  # follower is saturated; its terminal check will end it

    def clear(self) -> int:
        """Delete every log file and end all live streams. Returns files removed."""
        for run_id in list(self._subs):
            self.close(run_id)
        self._seq.clear()
        self._capped.clear()
        n = 0
        for p in self.root.glob("*.log"):
            try:
                p.unlink()
                n += 1
            except OSError:
                continue
        return n

    async def stream(self, run_id: str,
                     is_terminal: Callable[[], bool]) -> AsyncIterator[str]:
        """SSE-formatted follow: replay the file, then live lines until the run
        ends. Subscribe-first + seq filter → no gaps, no duplicates."""
        q = self.subscribe(run_id)
        try:
            seq_now, text = self.read(run_id)
            for line in text.splitlines():
                yield f"data: {line}\n\n"
            # already-terminal run: drain what's queued, skip the wait loop.
            # (Live runs rely on FIFO ordering — close()'s sentinel is enqueued
            # after every append, so waiting on the queue never drops lines.)
            done = is_terminal()
            while True:
                if done:
                    try:
                        item = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                else:
                    try:
                        item = await asyncio.wait_for(q.get(),
                                                      timeout=HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        if is_terminal():
                            break
                        yield ": ping\n\n"
                        continue
                if item is None:
                    break
                seq, line = item
                if seq <= seq_now:
                    continue  # already replayed from the file
                yield f"data: {line}\n\n"
            yield "event: end\ndata: {}\n\n"
        finally:
            self.unsubscribe(run_id, q)
