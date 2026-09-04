"""Per-mission repo resolution (M10, docs/16 F3): 0-or-1 configured repo per
mission, resolved as marker > instance default > zero-repo gate — and STICKY
once a run exists.

Stickiness is load-bearing (v0.1 plan finding H3): attempt 1 mints the branch
and PR on the resolved repo; if a marker edit re-routed a mission mid-flight,
rework would open a duplicate PR on the new repo and orphan the old one — the
PR-reuse invariant (M4) would silently break. So for a mission with run
history the latest run's repo_ref wins; a conflicting MARKER edit gates with
an explicit human-action reason, while a changed instance DEFAULT does not
gate — sticky wins silently (founder decision 2026-07-14, audit A25: a
config default edit must not park every in-flight mission of the instance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .orchestrator.markers import RAW_REPO_MARKER, REPO_MARKER

if TYPE_CHECKING:
    from ..config import PMOInstance
    from .model import Mission
    from .run import Run

# gate reasons (also matched by tests; keep stable)
REASON_ZERO_REPO = ("no repository resolved — gated until the internal "
                    "fallback forge (M11)")


def marker_repo(description: str) -> str | None:
    """The `devcake-repo:<name>` override from a mission description
    (first match wins; the name is lowercased)."""
    m = REPO_MARKER.search(description or "")
    return m.group(1).lower() if m else None


def repo_slug(url: str) -> str:
    """The repository URL's last path segment, `.git`-stripped, lowercased —
    the name Devs and humans reach for (it is the workspace folder and the
    tail of the URL they just read)."""
    return (url or "").rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()


def _slug_candidates(token: str, repo_names: set[str],
                     repo_urls: "dict[str, str] | None") -> list[str]:
    """Configured cards whose URL slug equals `token` (case-insensitive)."""
    if not repo_urls:
        return []
    return sorted(n for n, u in repo_urls.items()
                  if n in repo_names and repo_slug(u) == token.lower())


def _cards_with_slugs(repo_names: set[str],
                      repo_urls: "dict[str, str] | None") -> str:
    """`alpha (billing-api), beta (inventory-sync)` — the fix-the-
    marker hint that shows the mapping instead of sending the human to the
    admin panel."""
    parts = []
    for n in sorted(repo_names):
        slug = repo_slug((repo_urls or {}).get(n, ""))
        parts.append(f"{n} ({slug})" if slug and slug != n else n)
    return ", ".join(parts) or "(none)"


def repo_urls_of(instances) -> "dict[str, str] | None":
    """Card name → URL from a forge registry's `instances` map; None when
    the registry exposes names only (stubs), which disables the alias."""
    items = getattr(instances, "items", None)
    if items is None:
        return None
    return {n: getattr(i, "url", "") or "" for n, i in items()}


def resolve_marker(description: str, repo_names: set[str],
                   repo_urls: "dict[str, str] | None" = None) -> str | None:
    """The CARD a description's marker names, or None: the card name itself,
    or — field finding, hyphenated URL slugs written by triage Devs and
    humans alike — the one configured card whose URL slug matches. Anything
    ambiguous or unknown is None; `resolve_repo` owns the gate reasons. This
    is the ONE marker→card rule, shared with decomposition's inheritance."""
    strict = marker_repo(description)
    if strict is not None and strict in repo_names:
        return strict
    raw = RAW_REPO_MARKER.search(description or "")
    token = strict if strict is not None else (raw.group(1).strip() if raw else None)
    if not token:
        return None
    cands = _slug_candidates(token, repo_names, repo_urls)
    return cands[0] if len(cands) == 1 else None


def resolve_draft_repo(token: str, instance: "PMOInstance",
                       repo_names: set[str],
                       repo_urls: "dict[str, str] | None" = None
                       ) -> tuple[str | None, str | None]:
    """→ (card, None) for a decomposition draft's `repo` field, or
    (None, reason). The same rule a description marker obeys — card name
    or unique URL slug; a work repo of THIS instance, never a reference
    repo — applied to structured data the app stamps itself, so a child's
    routing can never be neutralized with the Dev-written prose around it."""
    from .orchestrator.markers import defang
    tok = (token or "").strip().lower()
    shown = defang(token or "")[:40]
    if not tok:
        return None, "empty repo"
    card = tok if tok in repo_names else None
    if card is None:
        cands = _slug_candidates(tok, repo_names, repo_urls)
        if len(cands) == 1:
            card = cands[0]
        elif len(cands) > 1:
            return None, (f"ambiguous repo {shown!r} — the URL slug "
                          f"matches several cards: {', '.join(cands)}; "
                          f"use the card name")
        else:
            return None, (f"unknown repo {shown!r} — use the card name "
                          f"or the repository URL's last path segment; "
                          f"configured: {_cards_with_slugs(repo_names, repo_urls)}")
    if card in (instance.reference_repos or []):
        return None, (f"repo '{card}' is a REFERENCE repo of this instance "
                      f"(read-only context) — work cannot route to it")
    if card not in (instance.repos or []):
        return None, (f"repo '{card}' is not in this PMO instance's repo set "
                      f"{list(instance.repos or [])}")
    return card, None


def resolve_repo(mission: "Mission", instance: "PMOInstance",
                 repo_names: set[str],
                 run_history: "list[Run]",
                 repo_urls: "dict[str, str] | None" = None
                 ) -> tuple[str | None, str | None]:
    """→ (repo_name, None) when resolved; (None, reason) when gated.

    `run_history`: this mission's prior runs (any state), newest first —
    only their repo_ref is read. Steward/hello records never carry a
    mission's repo and must not be passed in.

    `repo_urls`: card name → URL for the configured repos. Enables the slug
    alias: a marker that is not a card name but equals exactly one work
    repo's URL slug resolves to that card (an exact secondary key on
    operator-owned config — NOT the silent default fall-through A26
    forbids: zero or several matches still gate, with the mapping in the
    reason). None = no aliasing (legacy callers, tests).
    """
    marker = marker_repo(mission.description)
    raw = RAW_REPO_MARKER.search(mission.description or "")
    token = marker if marker is not None else (
        raw.group(1).strip() if raw else None)
    if token is not None and marker not in repo_names:
        # not a card name (unparseable, or parseable but unknown): try the
        # slug alias before any gate
        cands = _slug_candidates(token, repo_names, repo_urls)
        if len(cands) == 1:
            marker = cands[0]
        elif len(cands) > 1:
            return None, (f"ambiguous `devcake-repo:` marker {token[:40]!r} — "
                          f"the URL slug matches several cards: "
                          f"{', '.join(cands)}; use the card name")
    if marker is None:
        if raw:
            # devcake-repo:-shaped but unparseable = a typo'd routing intent
            # — silently landing on the default (and then latching sticky
            # there) is the exact hazard the marker exists to avoid (A26)
            return None, (f"unparseable `devcake-repo:` marker "
                          f"{raw.group(1)[:40]!r} — the marker is the card "
                          f"name (lowercase letters/digits/underscores, ≤39) "
                          f"or the repository URL's last path segment; "
                          f"configured: {_cards_with_slugs(repo_names, repo_urls)}")

    sticky = next((r.repo_ref for r in run_history if r.repo_ref), None)
    if sticky is not None:
        if sticky not in repo_names:
            return None, (f"repo '{sticky}' (used by this mission's previous "
                          f"runs) is no longer configured — restore it or "
                          f"have a human close out the mission")
        if marker is not None and marker != sticky:
            return None, (f"repo marker changed mid-mission ('{sticky}' → "
                          f"'{marker}') — resolution is sticky once a run "
                          f"exists; remove the marker or have a human close "
                          f"out the mission on '{sticky}'")
        # a changed instance DEFAULT never gates: sticky wins silently
        # (founder decision 2026-07-14 — see module docstring)
        return sticky, None

    allowed = list(instance.repos or [])
    if marker is not None:
        if marker not in repo_names:
            return None, (f"unknown repo '{marker}' — fix the "
                          f"`devcake-repo:` marker: the card name or the "
                          f"repository URL's last path segment; configured: "
                          f"{_cards_with_slugs(repo_names, repo_urls)}")
        if marker in (instance.reference_repos or []):
            # reference repos are read-only consultation material for every
            # stage (founder request 2026-07-15) — never a work target
            return None, (f"repo '{marker}' is a REFERENCE repo of this "
                          f"instance (read-only context) — work cannot route "
                          f"to it; fix the marker")
        if marker not in allowed:
            # the instance's repo SET is its allowed set (item 2): a marker
            # naming a configured-but-unlisted repo gates rather than
            # silently crossing the instance boundary. Empty set (`[]` =
            # per-mission internal only) lists nothing — every external
            # marker is unlisted; do NOT short-circuit on falsy `allowed`
            # (that made `[]` more permissive than a non-empty set).
            return None, (f"repo '{marker}' is not in this PMO instance's "
                          f"repo set {allowed} — add it to the instance's "
                          f"repositories or fix the marker")
        return marker, None
    if allowed:
        # unmarked missions route to the FIRST entry (the default); config
        # cross-validates set members against repos — belt-and-braces here
        if allowed[0] not in repo_names:
            return None, (f"instance default repo '{allowed[0]}' "
                          f"is not configured")
        return allowed[0], None
    return None, REASON_ZERO_REPO
