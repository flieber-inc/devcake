"""Shared test fakes (not a test module)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from devcake.config import AppConfig, DevType, PMOInstance, RepoInstance
from devcake.domain.orchestrator import MissionManager


class FakeForgeRuntime:
    """Single-forge stand-in for the M10 ForgeRuntime: every repo name
    resolves to the one forge/instance (tests predate repo multiplicity and
    exercise single-repo flows; Run.repo_ref defaults to "main")."""

    def __init__(self, forge=None, inst=None):
        self._forge = forge
        self._inst = inst if inst is not None else RepoInstance(
            name="main", url="https://github.com/o/r")
        self.health: dict = {}
        self.breakers: dict = {}
        self.internal: set = set()

    @property
    def forges(self):
        return {self._inst.name: self._forge} if self._forge is not None else {}

    @property
    def instances(self):
        return {self._inst.name: self._inst} if self._forge is not None else {}

    def get(self, name):
        return self._forge

    def instance(self, name):
        return self._inst if self._forge is not None else None

    def latch(self, name, reason):
        self.breakers[name] = reason

    def apply_health(self, name, data):
        self.health[name] = data
        if data.get("ok"):
            self.breakers.pop(name, None)
        elif not data.get("transient"):
            self.breakers[name] = data.get("detail", "")


class NullMessaging:
    """Minimal MessagingPort stand-in for MissionManager construction."""

    async def create_run_user(self, rid):
        return "pw"

    async def delete_run_user(self, rid):
        pass

    async def delete_reply_stream(self, rid):
        pass


class FakeExecutor:
    """No-op ExecutorPort for constructing a real RunManager in tests."""

    def __init__(self):
        self.starts: list[tuple] = []

    async def start(self, params, dag_run_id):
        self.starts.append((params, dag_run_id))

    async def stop(self, dag_run_id):
        return True

    async def status(self, dag_run_id):
        return None


DEFAULT_INSTANCE = PMOInstance(name="linear", team_key="DEV")


def make_mission_manager(
    tmp_path: Path | None = None,
    *,
    pmo: Any = None,
    forge: Any = None,
    forge_runtime: Any = None,
    config: AppConfig | None = None,
    dev_types: dict[str, DevType] | None = None,
    messaging: Any = None,
    runs: Any = None,
    instance: PMOInstance | None = None,
    breakers: dict[str, str] | None = None,
    internal_forge: Any = None,
    skills: Any = None,
    noop_audit: bool = False,
) -> MissionManager:
    """Construct MissionManager via the real ``__init__`` (DI constructor).

    When ``tmp_path`` is given and ``runs`` is omitted, builds a real
    ``RunManager`` over a RunStore (not a SimpleNamespace). Opt into
    ``noop_audit=True`` only when a test needs to silence PMO audit feeds.

    Transitional: many tests still call private façade methods; construction
    path matches production. Full private-seam retarget is a follow-up epic.
    """
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.runs import RunManager

    cfg = config if config is not None else AppConfig()
    inst = instance if instance is not None else DEFAULT_INSTANCE
    dts = dev_types if dev_types is not None else {
        "senior-dev": DevType(name="senior-dev", harness_template="claude-code"),
    }
    fr = forge_runtime if forge_runtime is not None else FakeForgeRuntime(forge)
    msg = messaging if messaging is not None else NullMessaging()
    if runs is None:
        if tmp_path is not None:
            store = RunStore(Path(tmp_path) / "runs")
            runs = RunManager(store, msg, FakeExecutor())
        else:
            # No store path: lightweight stand-in for pure helper tests
            runs = SimpleNamespace(store=None, finalizer=None)
    mgr = MissionManager(
        cfg, dts, pmo, fr, runs, msg,
        instance=inst, breakers=breakers, internal_forge=internal_forge,
        skills=skills,
    )
    if noop_audit:
        mgr._audit = lambda *a, **k: None  # type: ignore[method-assign]
    return mgr
