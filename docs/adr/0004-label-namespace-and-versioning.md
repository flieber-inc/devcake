# ADR-0004 — Label Namespace and Versioning

**Status:** accepted (v0). **Amended:** managed set is the eleven names in
`02-domain-model.md` §5 / `ALL_LABELS`, including `DEVCAKE-DISCOVERY`
(ADR-0033).

## Context

The mission state machine is driven by labels living in the PMO System, visible to and editable by humans. They must be unmistakable, stable, and centrally defined.

## Decision

A flat uppercase namespace whose managed set is defined once in
`02-domain-model.md` §5 and mirrored in `ALL_LABELS`: `DEVCAKE` (opt-in
adoption signal), three stage labels (`-PLAN`, `-EXECUTE`, `-REVIEW`), plus
`-MERGE` (awaiting merge), `-CREATED` (provenance), `-FAILED` (attention),
`-SKIP` (opt-out), `-TRACKING` (decomposed projects), `-NEEDS-HUMAN`
(deliberate hand-off — added post-v0 with `adr/0007`; the set was nine
through v0 and ten after NEEDS-HUMAN), and `-DISCOVERY` (sweep-gate for
harvested discoveries — ADR-0033; not a `derive()` / schedule row). No
version suffixes in names. The app ensures every `ALL_LABELS` member exists
in the configured team at startup. Renaming is a documented migration:
create new → copy on touched missions → retire old.

## Alternatives considered

- **Versioned labels** (`DEVCAKE-PLAN-V1`) — pollutes the human-visible label space to solve a migration that may never happen.
- **Encoding state in Linear custom fields or workflow states** — less portable across PMO Systems; labels are the lowest common denominator every candidate backend supports.
- **Prefix configurability** — deferred; a hardcoded prefix keeps the derivation table and docs unambiguous.

## Consequences

Humans can drive DevCake entirely from Linear (add `DEVCAKE-SKIP`, remove `DEVCAKE-FAILED`). Two stage labels at once is a detectable conflict (INV-2) rather than undefined behavior. Adapters must support label ensure/create (`PMOPort.ensure_labels`).
