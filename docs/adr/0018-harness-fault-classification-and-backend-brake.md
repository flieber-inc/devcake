# ADR-0018 — Harness fault classification and the model-backend brake

- **Status:** accepted (2026-07-24; codex/grok arms settled 2026-07-25)
- **Context:** Operators run DevCake against varied models and local /
  OpenAI-compatible backends. Harness CLIs often exit 0 (or leave stderr empty)
  while the run produced nothing useful, and pre-ADR classification treated that
  as ordinary `DEV_BAD_OUTPUT` (exit 11). Experimentation then burns
  `max_attempts` without a truthful class, without workspace forensics, and —
  when many missions share one sick backend — without any fleet throttle.
  Exit status alone is not a failure signal; in-band stream events are.

## Decision

### 1 — The entrypoint asks whether the harness worked

`harness_fault(harness, out, harness_exit, *, dump, last_message, prompt)`
returns `{"reason", "detail"}` or `None`, via per-harness predicates. Reasons:
`turn_budget`, `terminal_error`, `empty_completion`, `no_terminal_event`.

**Conservatism:** a model refusal and a tool-only run must never be faults
(false 15 excuses attempts and can throttle a Dev Type). `empty_completion` uses
**structural** activity (non-blank assistant text and/or tool items), never a
token threshold — scenario captures show `output_tokens == 0` for both empty and
tool-only Claude runs.

**Per-harness settled shapes** (scenario captures under
`app/tests/fixtures/harness_streams/`):

| Harness | Empty / nothing useful | Auth (401) | Turn cap |
|---|---|---|---|
| claude-code | structural empty on success result | `api_error_status` field | `max_turns` / `error_max_turns` → 16 |
| codex | empty only if no agent message and no tool items; `item.type=="error"` is **not** tool activity | transport wording in error events (`unexpected status NNN`, `last status: NNN`) | unreachable (no cap) |
| grok-build | export activity after prompt echo; or terminal `error` event | `Unauthorized (NNN)` / `(status NNN` in error events | `max_turns_reached` event → 16 |

HTTP status patterns are read **only** from CLI error events, never from
model-controlled assistant text. Precision over recall: a false 12 pauses a
whole Dev Type; a missed 401 falls through to 15 (still counted, still visible).

### 2 — Exit codes 15 and 16

- **15 `DEV_HARNESS_FAULT`** — correlation-eligible.
- **16 `DEV_TURN_BUDGET`** — always counted, never correlation-eligible
  (retrying the same cap cannot help).

### 3 — Precedence

**Nonzero exit** (result.json not read): turn budget → distinctive auth →
predicate → generic auth → crash.  
**Zero exit:** valid result.json → success; else turn budget / predicate → 15|16;
optional misplaced-result recovery; else 11.

Success on a **nonzero** exit is forbidden (would turn today’s failures into
PMO transitions).

### 4 — Evidence on failure artifacts

Exits 10/11/15/16 carry `error_class`, `error_detail`, and bounded
`workspace_forensics` (also rendered into the transcript for lockstep skew).

### 5 — Misplaced `result.json`

Fixed candidate list, `mtime >= harness start`, no symlinks. Diagnosis always;
recovery behind `recover_misplaced_result` (default on). Prompt binding rules
must not contradict `/workspace/out/result.json`.

### 6 — Store-derived backend brake (not a circuit breaker)

`domain/backend_health.py`, same idiom as mapper degradation: no counters,
restart-safe, cleared by success. Rejected: latched breaker (no credential to
write), timers, hard dispatch block.

| Predicate | Effect |
|---|---|
| `backend_correlated` | ≥2 `DEV_HARNESS_FAULT` across ≥2 missions in last 3 terminal runs of a dev type → may **excuse** attempt |
| `backend_degraded` | that, **or** 3 consecutive mission-bearing faults on one mission → **throttle** to one probe |

Solo deployments get throttle without free retries. `excusals_left` (3 per step)
bounds livelock; the same bound covers plain `DEV_FORGE` (latches no breaker).
`DEV_FORGE_AUTH` stays uncounted only with the structured class + repo latch.

`refresh_degraded` intersects run-derived keys with the live Dev Type registry
so renames cannot leave permanent unclearable entries. Surface:
`dev_backend_degraded` on `/health` (outside `circuit_breakers`).

### 7 — Structured attempt accounting

`Run.error_class` / `Run.attempt_counted` stamped at failure. Attempt counting
matches the structured class (not a substring of `run.error` — injectable via
Dev-authored `blocked_by`). Kill paths stamp via `_kill_inner` chokepoint.

## Consequences

- Strongest protection for multi-mission fleets; solo deployments get throttle only.
- Brake is per Dev Type while the fault is often per shared backend (no first-class backend entity).
- Clear-runs resets the degraded map (it **is** run history), unlike auth breakers.
- `dev_failure_error` mutates the run; fakes must stamp `error_class`.
- Failure-path store scan is O(runs); acceptable until measured otherwise.

## Known gaps (exit 11, unbraked)

The brake keys on `DEV_HARNESS_FAULT` only. Failures that produce real text or
tool activity but no `result.json` still land as **exit 11** `DEV_BAD_OUTPUT`:

- Models that invent tool syntax as prose (especially codex + large optional
  tool schemas) — operator notes in `docs/08-harness-templates.md` §8.
- Grok silent non-progress halt on a repeated tool call — `docs/15` §2b.

Fault arms must not key on model-controlled string content to “catch” these.

## Related

- Implement: `images/common/dev_entrypoint.py`, `domain/backend_health.py`,
  finalize / dispatch / schedule / poll, messaging chunk admission.
- Evidence: scenario captures in `app/tests/fixtures/harness_streams/` +
  `test_harness_captures.py`, `test_entrypoint_fault.py`, `test_backend_brake.py`.
- Operator: `docs/07` §4, `docs/15` §1/§2/§2a/§2b/§4a, `docs/08` §8, `docs/11`/`12`.
