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


class FakeInternalForge:
    """InternalForgePort activity surface (ADR-0014 D4) — recording fake.
    Extend per-test for the mission-repo methods when needed."""

    def __init__(self, ro_token: str = "act-ro-tok", push_exc=None):
        self.ensured: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, list, str]] = []
        self.deleted: list[str] = []
        self.ro_token = ro_token
        self.push_exc = push_exc

    async def ensure_activity_repo(self, instance, mission_key):
        from devcake.ports.internal_forge import activity_repo_name
        self.ensured.append((instance, mission_key))
        return activity_repo_name(instance, mission_key)

    async def push_activity_snapshot(self, repo_name, files, message):
        if self.push_exc:
            raise self.push_exc
        self.pushes.append((repo_name, files, message))

    def activity_credentials(self, repo_name):
        from devcake.ports.internal_forge import ActivityRepoCredentials
        if not self.ro_token:
            return None
        return ActivityRepoCredentials(
            repo_name=repo_name,
            clone_url=f"http://gitea:3000/devcake-repos/{repo_name}.git",
            username="devcake-activity-ro", token=self.ro_token)

    async def list_activity_repos(self):
        return []

    async def delete_activity_repo(self, repo_name):
        if not repo_name.startswith("activity-"):   # honor the Protocol guard
            raise ValueError(f"not an activity repo: {repo_name!r}")
        self.deleted.append(repo_name)


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
    blocker_locator: Any = None,
) -> MissionManager:
    """Construct MissionManager via the real ``__init__`` (DI constructor).

    When ``tmp_path`` is given and ``runs`` is omitted, builds a real
    ``RunManager`` over a RunStore (not a SimpleNamespace). Opt into
    ``noop_audit=True`` only when a test needs to silence PMO audit feeds.

    Transitional: many tests still call private façade methods; construction
    path matches production. Behavior seams are the modules' public
    functions (ADR-0015).
    """
    from devcake.adapters.files.run_store import RunStore
    from devcake.domain.runs import RunManager

    cfg = config if config is not None else AppConfig()
    inst = instance if instance is not None else DEFAULT_INSTANCE
    dts = dev_types if dev_types is not None else {
        "judgment": DevType(name="judgment", harness_template="claude-code"),
        "implementer": DevType(name="implementer", harness_template="grok-build"),
        "steward": DevType(name="steward", harness_template="claude-code",
                          model="claude-opus-5"),
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
    # Locator is a required gate/dispatch dependency in production
    # (build_managers sets ONE shared instance); single-manager tests get a
    # self-only locator with the same resolution semantics.
    if blocker_locator is None:
        from devcake.domain.blocker_locator import BlockerLocator
        blocker_locator = BlockerLocator(
            {mgr.instance_name: mgr}, lambda bid: None)
    mgr.blocker_locator = blocker_locator
    if noop_audit:
        mgr._audit = lambda *a, **k: None  # type: ignore[method-assign]
    return mgr


def fake_pmo_capabilities(*, global_ids=True, relations_supported=True,
                          attachments_supported=True,
                          comment_max_chars=None):
    """Shared capability row for the test fakes. Default is Linear-shaped
    (global ids ON, so peer-resolution tests exercise the allowed path);
    colliding-id scenarios pass global_ids=False — the capability replaced
    GLOBAL_ID_SYSTEMS in the 2026-08 cleanups."""
    from devcake.ports.pmo import PMOCapabilities
    return PMOCapabilities(
        projects_supported=True, project_labels_supported=True,
        attachment_max_bytes=50 * 1024 * 1024,
        native_label_swap_atomic=True, relations_supported=relations_supported,
        attachments_supported=attachments_supported,
        comment_max_chars=comment_max_chars,
        global_ids=global_ids)


# ── ADR-0028: the test-side service graph ────────────────────────────────────

class _Unwired:
    """Explode-on-touch sentinel for Services slots a test did not wire.
    Reading an attribute or calling it names the missing slot instead of
    failing three frames later on a None."""

    def __init__(self, slot: str):
        object.__setattr__(self, "_slot", slot)

    def __getattr__(self, attr):
        raise AssertionError(
            f"test touched services.{self._slot}.{attr} — wire it via "
            f"make_services({self._slot}=...)")

    def __call__(self, *a, **k):
        raise AssertionError(
            f"test called services.{self._slot} — wire it via "
            f"make_services({self._slot}=...)")


def make_services(**overrides):
    """A Services instance for tests: named slots wired, everything else an
    explode-on-touch sentinel. NEVER calls the real build_services() — that
    reads /data, writes config.yaml, and constructs live adapters."""
    import dataclasses

    from devcake.api.services import Services

    # methods stubbable as instance attributes (the sanctioned seam — the
    # dataclass is deliberately unslotted)
    method_overrides = {
        name: overrides.pop(name)
        for name in ("reload_connections", "refresh_forge_health",
                     "build_managers", "managers_in_config_order")
        if name in overrides}
    names = [f.name for f in dataclasses.fields(Services)]
    unknown = set(overrides) - set(names)
    assert not unknown, f"make_services: unknown Services fields {sorted(unknown)}"
    s = Services(**{n: overrides.get(n, _Unwired(n)) for n in names})
    for name, stub in method_overrides.items():
        setattr(s, name, stub)
    return s
