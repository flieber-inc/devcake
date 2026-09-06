"""Prepare repository mirrors and optional context for every dispatch caller.

Work, reference and family repositories remain required. Memory and skills
follow the configured strict/open policy; omitted cards never reach a mount.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from ..config import AppConfig, DevType, PMOInstance
from ..ports.repository_context import RepositoryContextCache
from .repo_sourcing import (classify_context_failures, memory_mount_names,
                           resolved_skill_cards, unresolvable_memory_cards)

log = logging.getLogger("devcake.context")


@dataclass(frozen=True)
class RepositoryContext:
    mirrors: tuple[str, ...] = ()
    deferred: dict[str, str] = field(default_factory=dict)
    stale: frozenset[str] = frozenset()
    omitted: frozenset[str] = frozenset()
    problem: Literal["mirror", "memory"] | None = None


async def prepare_repository_context(
    cache: RepositoryContextCache, *, config: AppConfig, instance: PMOInstance,
    dev_type: DevType, work_repo: str, mission_type: str,
    blocker_entries: list[dict] | None = None, extra_repos: Iterable[str] = (),
) -> RepositoryContext:
    """Resolve aliases, freshen the union, and decide required/optional failures.

    ``extra_repos`` contains already mirror-eligible family work repositories.
    Skill sources deliberately bypass eligibility: missing sources must be
    reported by the cache. A skill alias that also supplies work stays required.
    """
    sourced = set(cache.needed_for(
        work_repo=work_repo, mission_type=mission_type, instance=instance,
        blocker_entries=blocker_entries, dev_type=dev_type, config=config))
    sourced.update(extra_repos)
    # ADR-0039: skill cards join the union resolved to physical mirror names
    # — the one spelling of that rule, shared with the launch snapshot
    skills = resolved_skill_cards(dev_type.skills, cache)
    needed = sorted(sourced | skills)
    memory = set(memory_mount_names(
        instance=instance, dev_type=dev_type, repo_ref=work_repo))
    ok, why = await cache.ensure_fresh(needed)
    stale: set[str] = set()
    omitted: set[str] = set()
    if not ok:
        deferred, stale, omitted = classify_context_failures(
            why, context_cards=memory | (skills - sourced),
            strict=config.context_sourcing_strict, has_mirror=cache.has_last_good)
        if deferred:
            return RepositoryContext(deferred=deferred, problem="mirror")
    bad_memory = unresolvable_memory_cards(
        config, memory - omitted, mirror_eligible=cache.eligible)
    if bad_memory:
        if config.context_sourcing_strict:
            return RepositoryContext(deferred=bad_memory, problem="memory")
        omitted.update(bad_memory)
    if stale or omitted:
        log.warning("repository context for %s: stale=%s omit=%s",
                    work_repo, sorted(stale), sorted(omitted))
    return RepositoryContext(
        mirrors=tuple(n for n in needed if n not in omitted),
        stale=frozenset(stale), omitted=frozenset(omitted))
