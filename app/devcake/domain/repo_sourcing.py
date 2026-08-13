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


def sourced_repo_names(*, work_repo: str, mission_type: str, instance,
                       blocker_entries: list[dict] | None) -> list[str]:
    """Ordered, deduped repo names this run draws from (docs/07 §5a):
    primary first, then (ONBOARD only) the instance's routing set, then
    reference repos, then blocker work repos. May include ineligible or
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
        # exactly [work_repo].
        wanted += [bw.get("repo_ref") or "" for bw in blocker_entries or []]
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
