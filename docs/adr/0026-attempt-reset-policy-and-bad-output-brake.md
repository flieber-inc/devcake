# ADR-0026 — Attempt-reset policy and the opt-in bad-output brake

- **Status:** accepted (2026-08-04)
- **Context:** The 2026-08-04 critical evaluation confirmed an unbounded
  token-spend hole in attempt accounting: `attempt_number` treated **any**
  non-DevCake feed comment as a human intervention deserving fresh attempts
  (`dispatch.py`, sentinel check only). Any integration that posts ordinary
  comments — a Linear↔GitHub sync bot, a CI notifier, a Slack mirror —
  posting more often than a run takes to fail keeps the counter at 1
  forever: `max_attempts` never fires, `DEVCAKE-FAILED` never lands, and a
  permanently broken step retries at token cost indefinitely. The same
  evaluation re-confirmed ADR-0018's documented gap: exit 11
  (`DEV_BAD_OUTPUT`) is invisible to the backend brake, so a shared-backend
  garbage cascade (the 2026-07-24 incident: every container talks, none
  writes `result.json`) burns the whole board. Founder decisions
  (2026-08-04): the reset rule becomes an **operator policy with a strict
  default** — a homelab operator whose token cost is measured in watts may
  legitimately choose infinite retries; the brake widening is **opt-in with
  the current design as default**; and both knobs get first-class admin-page
  explanation, because this nuance is exactly what a typical operator does
  not know.

## Decision

### 1 — `attempt_reset` (global, Limits & Traffic)

What grants a step fresh attempts. Two anchors are policy-independent:
removing `DEVCAKE-FAILED` (the give-up watermark) and a later step
finishing. The policy governs comments:

| Policy | Comment behavior | Give-up |
|---|---|---|
| **`label-ops`** (default) | Only a comment containing the literal `DEVCAKE-RETRY` resets. | normal |
| **`any-comment`** | Any non-DevCake comment resets (pre-0026 rule). | normal |
| **`unlimited`** | Same comment rule as `label-ops`. | **never** — the app never applies `DEVCAKE-FAILED` |

`DEVCAKE-RETRY` exists because strict mode needs a pre-give-up human
gesture: before `DEVCAKE-FAILED` lands there is no label to remove, and
without the token an operator steering by comment (README's documented
workflow) could not grant fresh attempts at all. It is a literal token in a
comment body, not a label — deliberate enough that no integration emits it
by accident. DevCake's own sentinel-signed posts never reset, token or not.

`unlimited` deliberately builds the livelock `excusals_left`'s docstring
warns against (no give-up, ever-growing run store). It is therefore LOUD: a
feed warning with cumulative recorded cost (ADR-0021 effective-cost
semantics) posts every `review_loop_warning_every` consecutive failures,
deduplicated per (mission, step, failure-count) across poll cycles.
Breakers (`DEV_AUTH`, `DEV_FORGE_AUTH`, repo latches) still act, and
`DEVCAKE-SKIP` still stops everything — `unlimited` removes only the
attempt ceiling, not the safety machinery.

### 2 — `brake_on_bad_output` (global, Limits & Traffic, default off)

Off keeps ADR-0018 exactly: only `DEV_HARNESS_FAULT` (exit 15) is brake
evidence. On widens the evidence set to `{DEV_HARNESS_FAULT,
DEV_BAD_OUTPUT}` for every arm — correlation, solo-streak throttle, and the
degraded map — so a mixed cascade (some containers exit 15, others 11)
reads as one backend event. A correlated exit-11 failure is then excused
(`attempt_counted=False`) on its own per-step excusal ledger
(`error_class="DEV_BAD_OUTPUT"`), bounded by the same
`MAX_EXCUSALS_PER_STEP`.

Unlike exit 15 there is **no container-class precondition**: exit 11 has no
in-band structured class — the exit code *is* the classification, stamped
app-side — so a reconcile-synthesized orphan carries the same evidence
value as a live finalize. The skew-safety argument that gates exit-15
excusal (ADR-0018) does not transfer, and requiring a class here would make
the toggle dead code.

Default off, per founder decision: the continuation loop (ADR-0022) already
absorbs most solitary narrate-and-stop exit-11s, and a genuinely confused
model should burn its attempts honestly. The toggle exists for fleets on
experimental/self-hosted backends where the cascade shape is a live risk.

### 3 — Admin UX is part of the contract

Both knobs render as first-class `SettingRow`s (Limits & Traffic — the
section name is capitalized as of this ADR) with long-form `help` text
explaining the failure modes: the chatty-integration hole for
`any-comment`, what `unlimited` really forfeits, and the brake's two arms —
including that at a per-type concurrency of 1 the throttle arm is inert and
only attempt-excusal acts. The config-diff/profile views label both fields
(`configLabels.js`).

## Consequences

- **Deliberate behavior change:** existing deployments get the strict
  default on upgrade — boards where a bot comment used to grant fresh
  attempts now reach `DEVCAKE-FAILED` honestly. Release-noted; pre-v1
  fail-loudly doctrine, no compat shim.
- Both fields are additive at config schema v4 (defaults, no migration);
  settings bundles and profiles round-trip them.
- The brake's evidence set is now derived per call
  (`backend_health.fault_classes(cfg)`), not a module constant — callers
  (`poll`, `finalize`) thread the config. `FAULT_CLASS` remains for the
  exit-15 identity.
- Deferred (docs/16 Candidates): a cycle-skip backoff so the brake's
  throttle arm acts at concurrency 1 (founder kept the current design);
  distinguishing bot from human authors via PMO actor metadata (would let
  `any-comment` be safe, but no adapter surfaces reliable bot flags today).
