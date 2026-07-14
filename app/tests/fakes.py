"""Shared test fakes (not a test module)."""

from devcake.config import RepoInstance


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
