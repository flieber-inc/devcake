# ADR-0027 — The failure taxonomy as data

- **Status:** accepted (2026-08-04)
- **Context:** One run-failure taxonomy lived in three hand-synchronized
  encodings. The Dev container speaks numeric exit codes (docs/07 §4);
  `finalize.dev_failure_error` mapped them to `error_class` strings through
  a branch ladder with per-class carve-outs; `reconcile.py` recovered
  classes for orphans by regexing Dagu's stderr against its **own**
  hand-maintained code list; and `dispatch.UNCOUNTED_CLASSES`,
  `backend_health`'s brake-evidence sets, and `runs.KILL_CLASSES` each
  restated membership rules. Adding one exit code or class meant editing
  four-plus places whose agreement was enforced only by discipline — across
  the app/image version-skew boundary, where the two sides deploy as
  separate artifacts. The 2026-08 evaluation named this a structural debt
  that every future change quietly taxes. Crucially, the asymmetries
  between the arms are DELIBERATE — each has an incident behind it (the
  DEV_FORGE livelock, the exit-15 skew rule, reconcile's exit-12 refusal) —
  so a naive unification that flattens them would re-open closed incidents.

## Decision

### 1 — One table (`domain/failure_taxonomy.py`)

A frozen `FailureRow` per `DEV_*` class. The nuance becomes **fields**, not
prose: `counting` is four-valued (`always` / `never` / `excusable` /
`forge-bounded`) because DEV_FORGE's excusal bound and DEV_AUTH's breaker
exemption are different safety arguments; `excusal_requires_structured_class`
differs between exits 15 and 11 because skew tolerance points in opposite
directions for them (ADR-0026); `orphan_recoverable` is False for exit 12
because a stale orphan post-mortem must never trip a breaker from
reconcile; `structured_only` encodes the exit-13 bare/structured split that
keeps DEV_FORGE_AUTH stampable only on the container's own classification.
Every class-name literal in app code is now an import of the table's
constants.

Scope: the `DEV_*` family only — the classes stamped on Run records.
docs/15 §1's other rows (`PMO_*`, `FORGE_*`, `ILLEGAL_OUTCOME`, …) are flow
outcomes, not run-record classes; they stay prose.

### 2 — Consumers are derivations

- `finalize.dev_failure_error`: `failure_taxonomy.classify(exit_code,
  structured)` picks the row (including the 13 split); only genuinely
  behavioral arms remain, as small handlers keyed on the row. The exits
  15 and 11 arms collapsed into ONE `_correlated_excusal` handler — their
  differences were exactly the two table fields.
- `reconcile`: the post-mortem regex is built from
  `ORPHAN_RECOVERABLE_EXIT_CODES` — the hand-list is gone.
- `dispatch.UNCOUNTED_CLASSES` := rows with `counting == "never"`.
- `backend_health.fault_classes()` := `brake_evidence == "always"` ∪
  (`"opt-in"` under `brake_on_bad_output`).
- `runs.KILL_CLASSES` := the table's `kill_state` column (DEV_KILLED stays
  the catch-all at the `_kill_inner` chokepoint).

### 3 — The doctrine becomes executable (`test_failure_taxonomy.py`)

- Pairing invariant: every uncounted row latches a breaker (previously a
  dispatch.py comment).
- Derivation pins: each derived view is asserted equal to its
  pre-ADR-0027 literal value, so the tableization provably preserved
  membership (the reconcile 10/11/20-recovered / 12-refused behavior is
  additionally pinned end-to-end in `test_crash_recovery`).
- **App/image parity by declared manifest, not text-scraping:** the image
  package declares its own exit contract — `fault.PRODUCED`, the set of
  `(exit_code, error_class)` pairs a Dev can hand the app — and the parity
  test imports it through the images/common test mount and asserts equality
  with the table's container-produced surface. Real objects across the
  skew boundary; future HarnessDialect modules keep the manifest. An
  AST honesty check (constant `sys.exit` sites + class literals ⊆ manifest,
  with the two artifact-less bare exits declared in `BARE_EXIT_CODES`)
  keeps `PRODUCED` true.
- Docs parity: docs/15 §1's `DEV_*` set must equal the table's classes
  (regex extract — prose edits cannot false-fail it). This closed a real
  gap: `DEV_ORPHANED`, `DEV_KILLED`, `DEV_OPERATOR_STOP` existed in code
  and were documented nowhere.
- Structural scan: no bare `DEV_*` string constant outside the table
  module — a typo'd class used to silently never match; now it fails CI.

## Consequences

- Adding an exit code or class is one table row (+ a handler only if it has
  new behavior), and the invariants force the docs row and the image
  manifest to move in the same commit.
- The `RunFinalizer` port surface is unchanged (`dev_failure_error`'s
  signature and payload contract); reconcile still passes no `error_class`
  key, so the 15-excusal and 13-auth arms remain unreachable from
  reconcile **by construction**.
- The pytest runners (scripts/pytest_app.sh, ci.yml) now bind `docs/` into
  the test container read-only, following the existing structural-mount
  pattern (a missing mount fails, never skips).
- No behavior change: the ADR-0018/0026 pin suites pass unmodified, plus
  the two new stamp rows (exits 12/15) added to the parametrize.
