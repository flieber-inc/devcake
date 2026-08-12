"""bus.py send/heartbeat resilience (2026-08-12 audit OPS-M5): the FIRST
heartbeat was outside any try (a boot-window Redis blip killed the thread
permanently, and heartbeat grace then killed a healthy run), and the
finalize send used the narrow retry budget. bus.py has import-time env reads
+ a Redis connection, so the functions are AST-extracted and exec'd against
a fake client instead of imported (the test_hello_bus_contract pattern)."""

import ast
import threading
import time
from pathlib import Path

import pytest

BUS = Path("/srv/images/common/devcake_dev/adapters/bus.py")


class _FlakyRedis:
    """RedisError for the first `fail_n` xadds, then succeeds."""

    class RedisError(Exception):
        pass

    def __init__(self, fail_n):
        self.fail_n = fail_n
        self.adds = 0

    def xadd(self, *a, **k):
        self.adds += 1
        if self.adds <= self.fail_n:
            raise self.RedisError("connection refused")


def _load_bus(fake, monkeypatch):
    assert BUS.exists(), (
        "mount missing — bind images/common → /srv/images/common"
    )
    monkeypatch.setenv("DEVCAKE_RUN_ID", "R-bus-1")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("REDIS_USER", "u")
    monkeypatch.setenv("REDIS_PASSWORD", "p")
    tree = ast.parse(BUS.read_text())
    keep = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in (
                "send", "heartbeat_loop", "send_artifacts", "_fit_payload"):
            keep.append(node)
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(n.isupper() for n in names):
                keep.append(node)
    ns = {"os": __import__("os"), "json": __import__("json"),
          "time": time, "threading": threading, "redis": fake,
          "hashlib": __import__("hashlib"), "uuid": __import__("uuid"),
          "datetime": __import__("datetime").datetime,
          "timezone": __import__("datetime").timezone,
          "RUN_ID": "R-bus-1", "INGRESS": "ingress", "r": fake,
          "sys": __import__("sys")}
    exec(compile(ast.Module(body=keep, type_ignores=[]),  # noqa: S102 — repo-controlled bus.py functions
                 str(BUS), "exec"), ns)
    return ns


def test_send_default_budget_gives_up_after_its_attempts(monkeypatch):
    fake = _FlakyRedis(fail_n=99)
    bus = _load_bus(fake, monkeypatch)
    with pytest.raises(fake.RedisError):
        bus["send"]("run.heartbeat", {}, )
    assert fake.adds == bus["SEND_ATTEMPTS"]


def test_send_recovers_within_the_resilient_budget(monkeypatch):
    # a blip that clears after more than the default budget but within the
    # resilient one — the finalize/first-heartbeat path must ride it out
    fail_n = 4
    fake = _FlakyRedis(fail_n=fail_n)
    bus = _load_bus(fake, monkeypatch)
    monkeypatch.setattr(bus["time"], "sleep", lambda *_: None)  # no real waits
    assert bus["SEND_ATTEMPTS"] <= fail_n < bus["SEND_ATTEMPTS_RESILIENT"]
    bus["send"]("run.artifacts", {"ok": True},
                attempts=bus["SEND_ATTEMPTS_RESILIENT"])
    assert fake.adds == fail_n + 1


def test_first_heartbeat_blip_does_not_kill_the_thread(monkeypatch):
    """The regression: a boot-window blip on the FIRST beat used to raise out
    of heartbeat_loop and the thread died silently. It must survive and go on
    to send the working beats."""
    fail_n = 3   # first beat blips, then recovers
    fake = _FlakyRedis(fail_n=fail_n)
    bus = _load_bus(fake, monkeypatch)
    monkeypatch.setattr(bus["time"], "sleep", lambda *_: None)
    stop = threading.Event()

    class _OneShot:
        def wait(self, _):
            fired = stop.is_set()
            stop.set()
            return fired          # False once (send a working beat), then True

    bus["heartbeat_loop"](_OneShot())
    # the starting beat blipped 3x then landed, and one working beat followed
    assert fake.adds >= fail_n + 1
