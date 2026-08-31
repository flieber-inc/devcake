"""The ONE repo-sourcing rule (ADR-0034 chokepoint; 2026-08-12 audit F11).

Which repo NAMES a run draws in — the primary work repo, ONBOARD's routing
set, every stage's reference repos, and done-blockers' work repos — used to
be written twice (`RepoCache.needed_for` and dispatch's `_extra_repos_for`)
and kept aligned by a "mirrors X's sourcing exactly" comment; the blocker
read-credential rule was written twice more (`_blocker_mount_ok` and the
`_extra_repos_for` blocker branch). Drift meant the mirror gate blocking on
repos the runspec never mounts, or mounting repos the gate never freshened
(a dead `file://` clone). Both rules now live here; consumers apply their
own per-name FILTERS (mirror eligibility, token enrichment) but never their
own sourcing.
"""

from __future__ import annotations

# stages whose runs receive reference repos + done-blockers' work repos
STAGES_WITH_EXTRAS = ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")
# consumer memory mounts apply on every mission stage including STEWARD
STAGES_WITH_MEMORY = STAGES_WITH_EXTRAS + ("STEWARD",)


def memory_mount_names(*, instance, dev_type, repo_ref: str) -> list[str]:
    """Consumer memory clones (PLAN_MEMORY §3.2): instance list then Dev
    Type list, deduped, minus the run's primary work repo (F2)."""
    wanted = list(getattr(instance, "memory_repos", None) or [])
    wanted += list(getattr(dev_type, "memory_repos", None) or [])
    out: list[str] = []
    seen: set[str] = set()
    for name in wanted:
        if name and name != repo_ref and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def inherited_curator_extras(cfg, instance, work_repo: str) -> list[str]:
    """Curator inherit (PLAN_MEMORY §7): when the work repo is memory-bound,
    pull consumer boards' repos ∪ reference_repos (plus this instance's
    own reference_repos). Never other notebooks. Never `m` itself."""
    from ..config import is_memory_bound
    if not work_repo or not is_memory_bound(cfg, work_repo):
        return []
    card_names = {r.name for r in cfg.repos}
    this_name = getattr(instance, "name", None)
    out: list[str] = []
    seen: set[str] = set()

    def _add(names) -> None:
        for n in names or []:
            if n and n != work_repo and n in card_names and n not in seen:
                seen.add(n)
                out.append(n)

    for other in cfg.pmos:
        if getattr(other, "name", None) == this_name:
            continue
        if work_repo in (other.memory_repos or []):
            _add(other.repos)
            _add(other.reference_repos)
    _add(getattr(instance, "reference_repos", None))
    return out


def unresolvable_memory_cards(cfg, memory_cards, *,
                              mirror_eligible) -> dict[str, str]:
    """Memory mounts the runspec could not deliver (PLAN_MEMORY §3.5).

    The mirror gate only ever sees mirror-eligible names, so a dangling
    binding (card deleted, list hand-edited) or an internal-forge card
    with no clone credential would silently produce a memoryless run
    whose prompt still claims the mount. Resolve them HERE, at dispatch:
    strict ⇒ defer, open ⇒ omit — never a silent drop."""
    bad: dict[str, str] = {}
    for name in memory_cards:
        inst = next((r for r in cfg.repos if r.name == name), None)
        if inst is None:
            bad[name] = "names no configured repo card"
            continue
        if mirror_eligible(name):
            continue                     # freshness/credentials: mirror gate
        if not (inst.url or "").strip():
            bad[name] = "card has no URL"
        elif not (inst.token_ro or inst.token):
            bad[name] = "card has no read credential for the memory mount"
    return bad


def classify_context_failures(why: dict[str, str], *, context_cards: set[str],
                              strict: bool, has_mirror) -> tuple[
        dict[str, str], set[str], set[str]]:
    """Split a failed ensure_fresh map (PLAN_MEMORY §3.5).

    Work / reference / blocker failures always defer. Memory and
    skill-source cards are toggle-governed: strict ⇒ defer (provisioning
    family, no attempt); open ⇒ last-good mirror is stale_cache, never-
    synced is omit-and-continue. ``has_mirror`` must mean last-good
    content (``RepoCache.has_last_good``), not bare-dir presence —
    ``mirror_path.is_dir`` is true after a failed first sync too.
    """
    defer: dict[str, str] = {}
    stale: set[str] = set()
    omit: set[str] = set()
    for name, reason in why.items():
        if name not in context_cards:
            defer[name] = reason
            continue
        if strict:
            defer[name] = reason
        elif has_mirror(name):
            stale.add(name)
        else:
            omit.add(name)
    return defer, stale, omit


def sourced_repo_names(*, work_repo: str, mission_type: str, instance,
                       blocker_entries: list[dict] | None,
                       dev_type=None, config=None) -> list[str]:
    """Ordered, deduped repo names this run draws from (docs/07 §5a):
    primary first, then (ONBOARD only) the instance's routing set, then
    reference repos, then blocker work repos, then consumer memory
    mounts, then Curator inherit extras. May include ineligible or
    uncredentialed names — filtering is the caller's job, sourcing is not."""
    wanted: list[str] = [work_repo]
    if mission_type == "ONBOARD":
        wanted += list(instance.repos or [])
    if mission_type in STAGES_WITH_EXTRAS:
        wanted += list(instance.reference_repos or [])
        wanted += [bw.get("repo_ref") or "" for bw in blocker_entries or []]
    if mission_type == "STEWARD":
        # ADR-0033 discovery flavor: the family's work repos ride
        # blocker-entry-shaped extras (RO clones for evidence anchoring).
        # Deliberately WITHOUT reference_repos — family WORK repos only;
        # the relations flavor passes no entries, so it still sources
        # exactly [work_repo] (+ memory, below).
        wanted += [bw.get("repo_ref") or "" for bw in blocker_entries or []]
    if mission_type in STAGES_WITH_MEMORY:
        wanted += memory_mount_names(
            instance=instance, dev_type=dev_type, repo_ref=work_repo)
        if config is not None:
            wanted += inherited_curator_extras(config, instance, work_repo)
    out: list[str] = []
    seen: set[str] = set()
    for name in wanted:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def blocker_read_credential(mgr, name: str):
    """The ONE blocker-work read-credential rule: internal mission
    credentials first (token_read), else the configured card's read token
    (token_ro, write fallback — the primary-token rule). Returns
    ``("internal", creds)`` / ``("configured", inst, forge, token)`` /
    ``None`` (not mountable now — cleared internal repo, removed instance,
    or no token). A dispatch-time snapshot only: a clear between dispatch
    and runspec still omits silently at runspec (non-fatal)."""
    if mgr.internal_forge is not None:
        creds = mgr.internal_forge.mission_credentials(name)
        if creds is not None and creds.token_read:
            return ("internal", creds)
    inst = mgr.forges.instance(name)
    forge = mgr.forges.get(name)
    if (inst is not None and forge is not None
            and (inst.token_ro or inst.token)):
        return ("configured", inst, forge, inst.token_ro or inst.token)
    return None


def skill_source_cards(skill_names) -> set[str]:
    """Repo cards referenced as `<card>/<skill>` in a Dev Type's skills —
    the ADR-0016-addendum addition to the mirror gate's needed-set
    (fail-closed, founder ruling). Deliberately NOT part of
    sourced_repo_names: skill cards feed the runspec skills payload from
    the mirror READ-side and are never cloned into /workspace."""
    return {n.split("/", 1)[0] for n in (skill_names or []) if "/" in n}


def resolved_skill_cards(skill_names, repo_cache) -> set[str]:
    """`skill_source_cards` mapped through the mirror resolver (ADR-0039):
    a repo-backed source's sync — and therefore its `ensure_fresh` failure
    KEY — rides its backing repository card's physical mirror name. Every
    gate and launch snapshot must union THIS set, never the raw source
    names: comparing raw names against resolved failure keys silently
    reclassifies a backed source's failure as a sourcing failure (defer)
    where the toggle-governed context split should apply. A backing card
    that ALSO rides sourcing classifies as sourcing — callers subtract
    their sourced set from this one before building `context_cards`."""
    cards = skill_source_cards(skill_names)
    resolver = getattr(repo_cache, "mirror_name_of", None)
    return {resolver(c) for c in cards} if callable(resolver) else cards
