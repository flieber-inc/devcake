"""Boot-path coverage the lifespan test deliberately skips (2026-08-12 audit
test-gap). The full lifespan is I/O-bound (Redis, OO, Gitea) and belongs to
the live R9 drill, but two boot behaviors are unit-testable and were
uncovered: the background-task death reporter (an error path whose failure
is SILENCE) and the SEC-3 boot-refusal wiring."""

import ast
import asyncio
import logging
from pathlib import Path

from devcake.api.services import _log_task_death

MAIN = Path(__file__).resolve().parents[1] / "devcake" / "api" / "main.py"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_log_task_death_reports_a_crashed_background_task(caplog):
    """poll/watchdog tasks get add_done_callback(_log_task_death); a task that
    dies with an exception must be LOUD — a silent death is exactly how the
    2026-08 poll-loop deaths went unnoticed."""
    async def boom():
        raise RuntimeError("poll loop crashed")

    async def scenario():
        t = asyncio.create_task(boom(), name="poll:linear")
        t.add_done_callback(_log_task_death)
        try:
            await t
        except RuntimeError:
            pass
        await asyncio.sleep(0)   # let the done-callback run

    with caplog.at_level(logging.ERROR):
        _run(scenario())
    assert any("background task poll:linear DIED" in r.message
               for r in caplog.records)


def test_log_task_death_is_silent_on_clean_and_cancelled(caplog):
    async def clean():
        return 1

    async def scenario():
        ok = asyncio.create_task(clean(), name="ok")
        ok.add_done_callback(_log_task_death)
        await ok
        cancelled = asyncio.create_task(asyncio.sleep(10), name="cx")
        cancelled.add_done_callback(_log_task_death)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR):
        _run(scenario())
    assert not any("DIED" in r.message for r in caplog.records)


def test_boot_refuses_on_invalid_cross_store_semantics():
    """SEC-3 boot-refusal wiring: the lifespan calls validate_config_semantics
    after template seeding and converts a BundleError into a boot RuntimeError
    with remediation — so a hand-edited config referencing a deleted custom
    Dev Type refuses at startup, not at dispatch. Source-guarded (the full
    lifespan needs live Redis/OO); the semantics function itself is behavior-
    tested in test_config_schema/test_settings_bundle."""
    tree = ast.parse(MAIN.read_text())
    lifespan = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "lifespan")
    calls_validate = any(
        isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id == "validate_config_semantics")
            or (isinstance(n.func, ast.Attribute)
                and n.func.attr == "validate_config_semantics"))
        for n in ast.walk(lifespan))
    assert calls_validate, (
        "lifespan must validate cross-store semantics at boot (SEC-3) — "
        "a config referencing a deleted Dev Type must refuse at startup")
    # and the BundleError → boot RuntimeError conversion is present
    handlers = [n for n in ast.walk(lifespan)
                if isinstance(n, ast.ExceptHandler)
                and n.type is not None
                and ast.unparse(n.type) == "BundleError"]
    assert any(
        any(isinstance(s, ast.Raise) for s in ast.walk(h)) for h in handlers), (
        "a boot BundleError must re-raise as a loud RuntimeError, not be "
        "swallowed")
