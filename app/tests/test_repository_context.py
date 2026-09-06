"""Repository-context policy at its shared preparation seam."""
from __future__ import annotations

import asyncio

import pytest

from devcake.config import AppConfig, DevType, PMOInstance, RepoInstance
from devcake.domain.repo_mirror import NullRepoCache
from devcake.domain.repository_context import prepare_repository_context


class Cache(NullRepoCache):
    def __init__(self, *, failures=None, last_good=(), sourced=("work",)):
        super().__init__()
        self.failures = failures or {}
        self.last_good = set(last_good)
        self.sourced = list(sourced)

    def needed_for(self, **kwargs):
        return self.sourced

    def mirror_name_of(self, name):
        return "skills" if name == "shelf" else name

    def eligible(self, name):
        return name in {"work", "memory", "skills"}

    def has_last_good(self, name):
        return name in self.last_good

    async def ensure_fresh(self, names):
        failures = {n: self.failures[n] for n in names if n in self.failures}
        return not failures, failures


def prepare(cache, *, strict=False, stage="ONBOARD", memory=(), extra=()):
    config = AppConfig(context_sourcing_strict=strict)
    config.repos = [RepoInstance(name="work", url="https://example.com/org/work")]
    instance = PMOInstance(name="board", team_key="T", memory_repos=list(memory))
    dev_type = DevType(name="dev", harness_template="claude-code", skills=["shelf/tdd"])
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(prepare_repository_context(
            cache, config=config, instance=instance, dev_type=dev_type,
            work_repo="work", mission_type=stage, extra_repos=extra))
    finally:
        loop.close()


@pytest.mark.parametrize("stage", ["ONBOARD", "STEWARD"])
@pytest.mark.parametrize("last_good", [False, True])
def test_open_context_uses_last_good_or_omits(stage, last_good):
    cache = Cache(failures={"skills": "fetch unavailable"},
                  last_good={"skills"} if last_good else ())
    result = prepare(cache, stage=stage)
    assert result.deferred == {}
    assert result.stale == (frozenset({"skills"}) if last_good else frozenset())
    assert result.omitted == (frozenset() if last_good else frozenset({"skills"}))
    assert result.mirrors == (("skills", "work") if last_good else ("work",))


@pytest.mark.parametrize("strict", [False, True])
def test_work_repository_is_never_downgraded_to_optional_context(strict):
    cache = Cache(failures={"skills": "fetch unavailable"}, last_good={"skills"},
                  sourced=("work", "skills"))
    result = prepare(cache, strict=strict)
    assert result.deferred == {"skills": "fetch unavailable"}
    assert result.problem == "mirror"


def test_strict_context_failure_defers():
    result = prepare(Cache(failures={"skills": "fetch unavailable"}), strict=True)
    assert result.deferred == {"skills": "fetch unavailable"}
    assert result.stale == result.omitted == frozenset()


@pytest.mark.parametrize("strict", [False, True])
def test_missing_memory_binding_is_explicit(strict):
    result = prepare(Cache(), strict=strict, memory=["missing"])
    if strict:
        assert result.deferred == {"missing": "names no configured repo card"}
        assert result.problem == "memory"
    else:
        assert result.deferred == {}
        assert result.omitted == frozenset({"missing"})


def test_family_work_failure_stays_required_in_open_mode():
    result = prepare(Cache(failures={"family": "fetch unavailable"}),
                     stage="STEWARD", extra=["family"])
    assert result.deferred == {"family": "fetch unavailable"}
