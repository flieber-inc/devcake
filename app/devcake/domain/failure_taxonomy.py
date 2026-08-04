"""THE run-failure taxonomy, as data (ADR-0027).

One frozen row per `DEV_*` class stamped on Run records. Before this module
the taxonomy lived in three hand-synchronized encodings — the
`finalize.dev_failure_error` branch ladder, reconcile's stderr regex, and
the membership sets in dispatch / backend_health / runs — whose agreement
was enforced only by discipline, across the app/image version-skew boundary.
Adding one exit code meant editing four-plus places. Now the consumers are
DERIVED views of this table, and `test_failure_taxonomy.py` makes the
pairing doctrine, the app/image parity, and the docs/15 table executable
invariants.

The asymmetries between rows are DELIBERATE — each carries an incident.
They are fields here, not flattened: `counting` is four-valued because
DEV_FORGE's bound and DEV_AUTH's breaker exemption are different safety
arguments; `excusal_requires_structured_class` differs between exits 15 and
11 because skew tolerance points in opposite directions for them; exit 12
is not orphan-recoverable because a stale post-mortem must never trip a
breaker. Scope: the `DEV_*` family only — docs/15 §1's flow outcomes
(`PMO_*`, `FORGE_*`, `ILLEGAL_OUTCOME`, …) are not run-record classes and
stay prose.
"""

from __future__ import annotations

from dataclasses import dataclass

# The class-name constants. Everything in app/devcake that stamps or matches
# a class imports these — a bare "DEV_*" literal outside this module fails
# the structural scan, so a typo'd class can no longer silently never match.
DEV_CRASH = "DEV_CRASH"
DEV_BAD_OUTPUT = "DEV_BAD_OUTPUT"
DEV_AUTH = "DEV_AUTH"
DEV_FORGE = "DEV_FORGE"
DEV_FORGE_AUTH = "DEV_FORGE_AUTH"
DEV_MCP_SETUP = "DEV_MCP_SETUP"
DEV_HARNESS_FAULT = "DEV_HARNESS_FAULT"
DEV_TURN_BUDGET = "DEV_TURN_BUDGET"
DEV_TIMEOUT = "DEV_TIMEOUT"
DEV_ORPHANED = "DEV_ORPHANED"
DEV_KILLED = "DEV_KILLED"
DEV_OPERATOR_STOP = "DEV_OPERATOR_STOP"


@dataclass(frozen=True)
class FailureRow:
    error_class: str
    # Container-produced exit codes; () = app-side-only (kill/orphan paths,
    # which never leave a container exit behind).
    exit_codes: tuple[int, ...]
    # The class is stamped ONLY when the container's structured `error_class`
    # matches (DEV_FORGE_AUTH); the bare code falls to the sibling row.
    structured_only: bool
    # "always"        — every failure burns an attempt (deterministic codes).
    # "never"         — exempt with NO cap; safe only because the row also
    #                   latches a breaker (the dispatch.py pairing doctrine,
    #                   enforced as an invariant test).
    # "excusable"     — counted unless backend-correlated with excusals left
    #                   (ADR-0018 §4a; opt-in for exit 11 via ADR-0026's
    #                   brake_on_bad_output — see brake_evidence).
    # "forge-bounded" — uncounted while the step has excusals, counted once
    #                   spent: plain exit 13 latches no breaker, so the
    #                   unconditional exemption would re-dispatch forever on a
    #                   bad branch name / DNS failure / 500 (founder call
    #                   reversed in ADR-0018 — see dispatch.py's doctrine).
    counting: str
    # "dev_type" | "repo" | None. Every "never" row MUST have one — both
    # halt dispatch entirely, which is what makes "uncounted" livelock-free.
    breaker: str | None
    # Exit 15's skew rule: correlation — and therefore excusing an attempt —
    # requires the STRUCTURED class from the container; a reconcile-
    # synthesized orphan payload carries the numeric code only, so it earns
    # the label and contributes evidence but is never itself excused. Exit 11
    # is deliberately False (ADR-0026): it has no in-band structured class —
    # the exit code IS the classification (app-side), so an orphan carries
    # the same evidence value as a live finalize.
    excusal_requires_structured_class: bool
    # "always" | "opt-in" | "never" — feeds backend_health.fault_classes():
    # which rows count as brake evidence (window faults, correlation,
    # degradation). "opt-in" = only under cfg.brake_on_bad_output.
    brake_evidence: str
    # Membership in reconcile's post-mortem stderr regex. Exit 12 is
    # deliberately False: dev_failure_error latches the dev-type auth breaker
    # for it, and a stale orphan post-mortem must never trip a breaker from
    # reconcile.
    orphan_recoverable: bool
    # The kill state this row classifies at RunManager._kill_inner
    # (None = not a kill class, or passed explicitly by operator-stop
    # callers). DEV_KILLED doubles as the catch-all for unnamed states.
    kill_state: str | None = None
    # Fallback error detail when the artifact carries none (finalize's
    # per-arm messages). Empty for rows whose message is fully bespoke.
    default_detail: str = ""


TABLE: tuple[FailureRow, ...] = (
    FailureRow(DEV_CRASH, (10, 20), False, "always", None, False, "never",
               True),
    FailureRow(DEV_BAD_OUTPUT, (11,), False, "excusable", None, False,
               "opt-in", True,
               default_detail="result.json missing or invalid"),
    FailureRow(DEV_AUTH, (12,), False, "never", "dev_type", False, "never",
               False),
    FailureRow(DEV_FORGE, (13,), False, "forge-bounded", None, False, "never",
               True, default_detail="clone/push setup failed"),
    FailureRow(DEV_FORGE_AUTH, (13,), True, "never", "repo", False, "never",
               False, default_detail="repository credential rejected"),
    FailureRow(DEV_MCP_SETUP, (14,), False, "always", None, False, "never",
               True, default_detail="MCP setup command failed"),
    FailureRow(DEV_HARNESS_FAULT, (15,), False, "excusable", None, True,
               "always", True, default_detail="model backend unavailable"),
    FailureRow(DEV_TURN_BUDGET, (16,), False, "always", None, False, "never",
               True,
               default_detail="harness stopped at its configured turn cap"),
    FailureRow(DEV_TIMEOUT, (), False, "always", None, False, "never", False,
               kill_state="timed_out"),
    FailureRow(DEV_ORPHANED, (), False, "always", None, False, "never", False,
               kill_state="orphaned"),
    FailureRow(DEV_KILLED, (), False, "always", None, False, "never", False,
               kill_state="failed"),
    FailureRow(DEV_OPERATOR_STOP, (), False, "always", None, False, "never",
               False),
)

BY_CLASS: dict[str, FailureRow] = {r.error_class: r for r in TABLE}

# Exit-code lookup for the BARE (no structured class) case: structured_only
# rows never match a bare code, so 13 resolves to DEV_FORGE here.
BY_EXIT_CODE: dict[int, FailureRow] = {
    code: r for r in TABLE if not r.structured_only for code in r.exit_codes}


def classify(exit_code, structured_class: str) -> FailureRow | None:
    """The row a Dev failure artifact resolves to, or None (unknown code —
    finalize's fallback arm). A structured_only row is selected only when the
    container's own classification names it AND the exit code agrees — auth
    *wording* alone falls through to the bare row (see DEV_FORGE_AUTH's
    docstring trail in finalize.py)."""
    row = BY_CLASS.get(structured_class)
    if row is not None and row.structured_only and exit_code in row.exit_codes:
        return row
    return BY_EXIT_CODE.get(exit_code)


# ── Derived views (the old hand-synchronized encodings) ──────────────────────

# dispatch.counts_toward_attempts: classes whose failures NEVER burn attempts.
UNCOUNTED_CLASSES: frozenset[str] = frozenset(
    r.error_class for r in TABLE if r.counting == "never")

# backend_health.fault_classes(): brake evidence, unconditional vs opt-in.
BRAKE_ALWAYS_CLASSES: frozenset[str] = frozenset(
    r.error_class for r in TABLE if r.brake_evidence == "always")
BRAKE_OPT_IN_CLASSES: frozenset[str] = frozenset(
    r.error_class for r in TABLE if r.brake_evidence == "opt-in")

# reconcile's post-mortem enrichment: the codes recoverable from Dagu's
# node-error string. Sorted so the regex is deterministic.
ORPHAN_RECOVERABLE_EXIT_CODES: tuple[int, ...] = tuple(sorted(
    {code for r in TABLE if r.orphan_recoverable for code in r.exit_codes}))

# runs._kill_inner: error class per kill target state (DEV_KILLED is the
# catch-all for any state not named — applied at the chokepoint).
KILL_CLASSES: dict[str, str] = {
    r.kill_state: r.error_class for r in TABLE if r.kill_state}

# The container-produced (exit_code, error_class) surface — compared verbatim
# against the image package's self-declared `fault.PRODUCED` manifest by the
# parity test, bridging the two packages without coupling them.
CONTAINER_PRODUCED: frozenset[tuple[int, str]] = frozenset(
    (code, r.error_class) for r in TABLE for code in r.exit_codes)
