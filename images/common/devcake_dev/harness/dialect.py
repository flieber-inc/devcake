"""HarnessDialect — one chokepoint for per-CLI container behavior (docs/16 H1).

The app registry (`devcake.harness.HARNESSES`) owns image, credentials,
OAuth, and skills_dir. This module owns argv, render, parse, fault, and
session identity. Unknown ids fail closed — never fall through to Claude.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Protocol

WORKSPACE = pathlib.Path("/workspace")


@dataclasses.dataclass(frozen=True)
class ResumeSpec:
    """Capture-verified resume facts (ADR-0022). usage_cumulative: the
    resumed terminal event reports the WHOLE session — last-wins merge."""
    usage_cumulative: bool


@dataclasses.dataclass
class InvocationView:
    """What one harness invocation produced (docs/08 §5)."""
    result_text: str
    token_report: dict
    dump: str
    last_message: str = ""


class HarnessDialect(Protocol):
    id: str
    resume_spec: ResumeSpec | None
    dump_cumulative_on_resume: bool

    def argv(self, prompt: str, *, plan_mode: bool = False, model: str = "",
             extra=(), out_dir=None) -> list[str]: ...

    def resume_argv(self, session_id: str, prompt: str, *, model: str = "",
                    extra=(), out_dir=None) -> list[str] | None: ...

    def renderer(self):
        """Callable raw-line → condensed line | None (may be stateful)."""
        ...

    def parse_run(self, out: str, *, workspace: pathlib.Path,
                  model: str = "") -> InvocationView: ...

    def fault(self, out: str, harness_exit: int, *, dump: str = "",
              last_message: str = "", prompt: str = ""): ...

    def api_error_status(self, out: str): ...

    def session_identity(self, out: str) -> str: ...

    def terminal_evidence(self, out: str): ...


_DIALECTS: dict[str, HarnessDialect] = {}


def register(dialect: HarnessDialect) -> HarnessDialect:
    _DIALECTS[dialect.id] = dialect
    return dialect


def dialects() -> dict[str, HarnessDialect]:
    if not _DIALECTS:
        _load()
    return _DIALECTS


def get_dialect(harness: str) -> HarnessDialect:
    table = dialects()
    d = table.get(harness)
    if d is None:
        raise ValueError(
            f"unknown harness {harness!r} — refusing Claude fall-through "
            f"(known: {sorted(table)})")
    return d


def _load() -> None:
    # Imported for side-effect registration. Keep this list = HARNESSES keys.
    from . import dialects as _d  # noqa: F401
    _d.load_all()
