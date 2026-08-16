"""subprocess.run-shaped waiter that tees child output.

The host baker's beating_run uses this so a failed bake's last lines
reach the status file, not only .factory/watch.log.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable


class Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def tee_run(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict | None = None,
    stamp: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    interval: float = 5.0,
    sink=None,
    popen=subprocess.Popen,
    tail: int = 800,
) -> Completed:
    """Run argv, heartbeat via stamp(), tee stdout+stderr to sink, keep a tail."""
    sink = sys.stdout if sink is None else sink
    proc = popen(
        argv, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    # Bounded ring: last `tail` characters, not the whole BuildKit log.
    ring: deque[str] = deque()
    held = 0

    def _read() -> None:
        nonlocal held
        stream = proc.stdout
        if stream is None:
            return
        for line in iter(stream.readline, ""):
            ring.append(line)
            held += len(line)
            while held > tail and ring:
                held -= len(ring.popleft())
            try:
                sink.write(line)
                sink.flush()
            except Exception:  # noqa: BLE001 — log tee must not kill the bake
                pass

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    while proc.poll() is None:
        if stamp is not None:
            stamp()
        sleep(interval)
    reader.join(timeout=5)
    return Completed(returncode=int(proc.returncode or 0),
                     stdout="".join(ring), stderr="")
