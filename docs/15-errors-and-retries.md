# 15 — Errors and Retries

> **Audience:** implementers. One taxonomy; every component maps its failures into it.
> **Depends on:** `04-orchestrator.md` (attempts, watchdog), `07-dev-runtime.md` (exit codes),
> `09-messaging.md` (poison messages),
> `adr/0018-harness-fault-classification-and-backend-brake.md` (exits 15/16, auth
> precedence, backend brake).
> **Trust language:** out-of-pipeline merge and related residuals are
> **detection / tripwire**, not prevention — product contract
> [`14-security.md`](14-security.md) §2 zone C · §8.

## 1. Error classes

The `DEV_*` family below is **authoritative in code**: one row per class in
`domain/failure_taxonomy.py` (ADR-0027), from which the consumers (finalize
ladder, reconcile's post-mortem regex, the counting/brake/kill membership
sets) are derived — and `test_failure_taxonomy.py` pins this section's class
set against that table, so adding a class means a table row AND a row here.
The non-`DEV_*` rows are flow outcomes, not Run-record classes; they stay
prose.

| Class | Examples / mapping | Nature |
|---|---|---|
| `PMO_TRANSIENT` | Linear 429/`RATELIMITED`, 5xx, network — adapters signal it by raising `PMOTransient` (`ports/pmo.py`) | retryable |
| `PMO_PERMANENT` | Linear auth failure, 404 on team | config/credential problem |
| `PMO_GONE` | *taxonomy residual* — no dedicated code path today (a mid-run delete surfaces as ordinary PMO read failure / EXTERNAL_TRANSITION at finalize, not a named `PMO_GONE` branch) | external, informational |
| `FORGE_TRANSIENT` | forge 429/5xx/network; probe-classified transient failures | retryable |
| `FORGE_PERMANENT` | auth failure, branch protection blocks merge | config problem |
| `DEV_CRASH` | exit 10 (harness crash), 20 (entrypoint — incl. the ADR-0025 sentinel/marker family: provision found the wrong bind dir, or the harness step found no/mismatched `provisioned` marker — the artifact carries owner/mode/listing forensics); orphan post-mortem enrichment of those codes | counted attempt |
| `DEV_MCP_SETUP` | exit 14: additive **entrypoint** setup line failed or hit the 300 s per-command cap (`run.error` names the command + stderr/timeout tail), or **override** script aborted (`set -e` / non-zero; hangs are `DEV_TIMEOUT` via the run watchdog, not this class) | counted attempt |
| `DEV_TIMEOUT` | app watchdog kill via Dagu stop → Run `timed_out` (not an entrypoint exit code) | counted attempt |
| `DEV_ORPHANED` | reconciliation found the Dagu run dead while the app was away → Run `orphaned` (post-mortem enrichment may then upgrade `run.error` to a classified exit — §2 note); also stamped by the multi-instance router on a run whose PMO instance is no longer configured (state `failed`, deliberately — the condition is a genuine orphan) | counted attempt |
| `DEV_KILLED` | the kill-chokepoint **catch-all** (`_kill_inner`): any kill path that names no more specific state/class lands here, so a future kill site cannot produce an unclassified run | counted attempt |
| `DEV_OPERATOR_STOP` | operator-initiated stop (admin UI stop run / stop all, clear-runs drain) — passed explicitly by those callers, never a default | counted attempt — a stopped attempt burns like a failed one; pause or re-label the mission if that is not what you want |
| `DEV_AUTH` | exit 12 — harness credential failure per the §4 precedence contract (stream 401/403 and/or distinctive stderr markers; generic markers only when no in-band fault already explains the run) | circuit breaker (§4) — **not** a counted attempt |
| `DEV_FORGE` | exit 13 without the structured `DEV_FORGE_AUTH` class (transient forge/clone/push failure, or auth-ish wording without the structured class). Includes a **strict memory-notebook clone failure** in the provision step (ADR-0035: `context_sourcing_strict` on ⇒ the run must not start memoryless) | counted only after per-step excusals are spent (§4a) — **not** a latched breaker |
| `DEV_FORGE_AUTH` | exit 13 carrying the Dev's **structured** `DEV_FORGE_AUTH` classification (auth wording in the detail alone is `DEV_FORGE`) | **per-repo** forge circuit breaker (`repo:{name}`); that repo's missions stop dispatching until the token can push. NOTE (ADR-0035): a strict memory mount's revoked credential classifies here too — a NOTEBOOK card's breaker then freezes dispatch for every mission that binds it, by design (fail closed beats silently memoryless). Distinct case: a memory/skill-source card that fails the MIRROR gate never reaches a container at all — it defers dispatch as an unschedulable gate reason (blocked-reasons row), no attempt, no class |
| `DEV_HARNESS_FAULT` | exit 15: the harness reported a failure in-band, or produced no output at all, whatever its exit status (ADR-0018) | counted attempt — UNLESS correlated across ≥2 missions (§4a) |
| `DEV_TURN_BUDGET` | exit 16: the harness stopped at a configured run-budget cap — reachable for **`claude-code`** / **`grok-build`** (`--max-turns`) and **`qwen-code`** (`--max-session-turns` / `--max-wall-time` / `--max-tool-calls`, CLI exits 53/55); unreachable for **codex** 0.147.0, **pi**, and **opencode**, which have no turn/session cap that maps here (`07-dev-runtime.md` §4, §2a below) | counted attempt; deterministic, so never correlated and never excused |
| `DEV_BAD_OUTPUT` | exit 11: `result.json` missing/invalid **after the in-container continuation budget is spent, when the loop is enabled** (ADR-0022; `07-dev-runtime.md` §5a); app-side: structurally invalid payload behind a legal outcome (empty decomposition, bad `blocked_by`) | counted attempt — when many Devs share one backend, exit 11 can still land fleet-wide (model invents tools as prose — `08` §8; grok silent non-progress halt — §2b); §4a's brake keys on exit 15 by default, with `brake_on_bad_output` (ADR-0026, default off) widening it to cover exactly this cascade |
| `ILLEGAL_OUTCOME` | outcome not in `LEGAL_OUTCOMES` for the run type (`03` §6) — includes forged outcomes (e.g. EXECUTE claiming `reviewed`) | park with `DEVCAKE-SKIP` + comment; audit `illegal_outcome`; never acted on, never retried |
| `LABEL_CONFLICT` | ≥2 stage labels (derivation row 6) | human-resolve |
| `EXTERNAL_TRANSITION` | human changed status/label mid-run (`04-orchestrator.md` §4) | **not an error** — first-class outcome |
| `CONFIG_INVALID` | bad config file / failed validation | blocks startup or rejects the write |

## 2. Retry matrix (normative)

| Class | Retryable | Counts toward `max_attempts` | Who retries | User-visible surface |
|---|---|---|---|---|
| `PMO_TRANSIENT` | yes — **no in-adapter retry ladder.** Adapter raises `PMOTransient`; the poll cycle **skips that PMO instance's segment** and continues with the others; the next poll tick (`poll_interval_seconds`, default 30) re-attempts the sick instance | no | app (next poll tick) | `poll.instance` outcome / logs; not latched into `poll_degraded` (that field is for permanent per-instance failures) |
| `PMO_PERMANENT` | no | no | — | `poll_degraded` for that instance + SPA alert; other instances keep polling |
| `PMO_GONE` | n/a — residual class only | no | — | *not implemented as a dedicated path* — do not expect a WARN+cancel special case |
| `FORGE_TRANSIENT` | yes — **no exp-backoff matrix.** Poll re-probes forge health each cycle (latched breakers self-heal on a green probe; the re-probe sweep is bounded-parallel — `ForgeRuntime.refresh_all`, at most 8 in flight); finalization re-enters via ingress reclaim; adapters may short-sleep only where code has them (e.g. Gitea merge "try again later" 405 retries, GitHub 409 race retries — `06-forge-adapter.md` §5/§7a; the `.claims/` writer replays its checkout+commit+push cycle ONCE on a push race — ADR-0035). Dev clone/push is single-shot (exit 13 on failure) | no | app (poll / finalize resume) | health / breaker surfaces if a probe becomes definitive |
| `FORGE_PERMANENT` | no | no | — | PMO comment + health strip (e.g. merge blocked, `06-forge-adapter.md` §5) |
| `DEV_CRASH` | yes — by natural rescheduling (INV-3) | **yes** | scheduler (next cycle) | after cap: `DEVCAKE-FAILED` (§3) |
| `DEV_MCP_SETUP` | yes — same (a transient install/network failure deserves retries; the deterministic missing-secret case never dispatches at all, `14` §8) | **yes** | scheduler | same |
| `DEV_TIMEOUT` | yes — same | **yes** | scheduler | same |
| `DEV_ORPHANED` | yes — same (the mission's label never advanced) | **yes** | scheduler | same |
| `DEV_KILLED` | yes — same | **yes** | scheduler | same |
| `DEV_OPERATOR_STOP` | yes — the mission stays schedulable; stop the *mission*, not just the run, to prevent re-dispatch | **yes** | scheduler | Runs page names the operator stop |
| `DEV_BAD_OUTPUT` | yes — same | **yes** | scheduler | same |
| `DEV_HARNESS_FAULT` | yes — same | **yes**, unless correlated (§4a) | scheduler | `dev_backend_degraded` in `/health` + SPA warning while throttled |
| `DEV_TURN_BUDGET` | yes — but retrying the same cap cannot help; raise the harness's budget flag (claude-code / grok-build: `--max-turns`; qwen-code: `--max-session-turns` / wall-time / tool-calls; **codex / pi / opencode have none** — §2a) or assign a stronger Dev Type | **yes** | scheduler | `run.error` names the cap and where to change it |
| `DEV_FORGE` | yes — a forge outage should not burn missions | **no** while the step has excusals (§4a), **yes** once spent | scheduler | after the cap: `DEVCAKE-FAILED` |
| `DEV_AUTH` | no — pointless until creds fixed | **no** | — | circuit breaker (§4) + SPA/health alert |
| `DEV_FORGE_AUTH` | no — pointless until repository access is fixed | **no** | — | per-repo forge breaker + actionable connection-test error |
| `LABEL_CONFLICT` | n/a — skipped until resolved | no | — | derivation unschedulable reason only (`gate_map` / `GET /api/v1/missions`); **no** PMO comment, **no** metric |
| `EXTERNAL_TRANSITION` | n/a | no | — | explanatory PMO comment; run's artifacts already posted |

Retries of Dev work are never in-place: a failed attempt ends the container; the Mission's label never advanced (INV-3), so the next poll cycle re-derives and re-dispatches with `attempt_of_step + 1`. **A second not-a-retry carve-out (ADR-0031):** a *freshness re-review* is a fresh REVIEW dispatch after a **successful** run whose done-transition was withheld because material feed entries arrived after its dispatch watermark. The run being re-run did not fail — its *context* did. It ends `state="finished"`, which is itself an attempt-reset anchor, so the re-dispatch counts as attempt 1 and nothing feeds the ADR-0026 brakes; the loop is bounded by `` `devcake:freshness-rereview:N` `` feed markers and `AppConfig.budgets.freshness_rereviews` (default 5, `0` = unlimited — not a fixed-2 constant; conflict-resolve stays at `MAX_CONFLICT_RESOLVES = 2`), not by attempt machinery. **One deliberate carve-out (ADR-0022):** an in-container *continuation* is in-place BY DESIGN — but it is not a retry of a failed attempt. It happens *before* the attempt fails, only on a clean exit with no fault (the row-9 landing), inside one Run, bounded by `cfg.max_continuations` and the watchdog. A crashed, faulted, or auth-failed container still dies exactly as this section describes, and attempt counting never sees continuations. **ADR-0024 adds a pre-run gate in the same spirit:** "repository mirror not fresh — dispatch deferred" is an unschedulable reason (missions row + `/health.blocked_reasons`), NOT an error class — no run exists, nothing counts, the next poll retries. Auth-classed sync failures latch the per-repo forge breaker (§4). Deliberate hardening to know about: a reference/blocker repo whose sync fails now gates whole missions that previously ran with that context silently omitted.

## 2a. "Raise `--max-turns`" is not universal advice

The turn-cap remedy above assumes a harness that has a turn cap. Measured
against each CLI (`adr/0018-harness-fault-classification-and-backend-brake.md`, `07-dev-runtime.md` §4):

| Harness | Cap flag | Reaches exit 16? |
|---|---|---|
| `claude-code` | `--max-turns <N>` | **yes** — on `terminal_reason:"max_turns"` / `subtype:"error_max_turns"` |
| `grok-build` (0.2.112) | `--max-turns <N>` | **yes** — it emits a dedicated `{"type":"max_turns_reached"}` event **and** `end` `stopReason:"Cancelled"`, exits 1, and the predicate fires on that event type (`grok_turn_budget`). It landed on `DEV_CRASH` (exit 10) until the ADR-0018 fix round added the arm |
| `qwen-code` | `--max-session-turns` / `--max-wall-time` / `--max-tool-calls` | **yes** — CLI exits 53/55 and budget subtypes map through `qwen_run_fault` → `FAULT_TURN_BUDGET` → exit 16 (`08-harness-templates.md` §1) |
| `codex` (0.147.0) | **none** | **no** — no `--max-turns` equivalent and no config key for one, so the class is unreachable |
| `pi` | **none** | **no** — no turn/session budget flag maps to `FAULT_TURN_BUDGET` |
| `opencode` | **none** | **no** — no turn/session budget flag maps to `FAULT_TURN_BUDGET` |

Consequences for the operator. On a **claude-code** or **grok-build** Dev, raising
`--max-turns` in that Mission Type's extra CLI args (`11-admin-panel.md` §3) is
the literal remedy the `run.error` names, and both report the stop as
`DEV_TURN_BUDGET`. On a **qwen-code** Dev the levers are the three budget flags
above (same class, different CLI surface). On a **codex**, **pi**, or **opencode**
Dev there is nothing to raise: an unbounded run is stopped only by
`dev_timeout_minutes` (`AppConfig.dev_timeout_minutes`, default 120 — a
**global** setting, so lowering it to fence one Dev Type shortens every run) and
it arrives as a signal kill reported `DEV_TIMEOUT`, never `DEV_TURN_BUDGET`. The
levers there are a smaller task, a different Dev Type, or accepting the timeout as
the bound. Do not go looking for a turn flag those three CLIs do not expose.

**grok's cap has no default**, so nothing sits above the value you set: it stops
exactly where it is told (`grok_loop_varying_cap20` at 20; `grok_turn_budget` at 2),
`--max-turns <N>` is documented with no default, and `config.toml` has no
`max_turns` key. The 16 in §2b is a different stop path entirely — do not mistake
it for a ceiling on this flag.

## 2b. grok's silent non-progress halt — a `DEV_BAD_OUTPUT` with no diagnosis

**The hazard.** A grok Dev whose model keeps issuing the **same** tool call is
stopped by grok itself after ~16 model calls. The run ends `stopReason: "EndTurn"`
with **exit 0** — byte-identical in shape to a clean success, no
`max_turns_reached`, nothing on stderr. Nothing was accomplished, so no
`result.json` was written, so DevCake reports **exit 11 `DEV_BAD_OUTPUT`**
(`07-dev-runtime.md` §4) and the operator sees a Dev that "produced bad output".
There is no signal anywhere that the run was truncated.

**Why it matters.** A weak or overloaded model that loops on one command can
hit this path on many missions at once. `DEV_BAD_OUTPUT` has no brake by
default: every attempt counts, nothing is excused, nothing is throttled (§4a
keys on exit 15 unless `brake_on_bad_output` is on — ADR-0026).

**What it is not.** It is **not** a turn cap and `--max-turns` is not the lever:
a run with `--max-turns 30` halts at the same 16 because the cap is never reached
(`grok_loop_cap30`), while the same lane with *varying* tool calls runs past 16
and honours a cap of 20 (`grok_loop_varying_cap20`). Raising the cap changes
nothing about this failure; §2a's advice is for real cap stops (exit 16).

**The lever that does exist (ADR-0022).** This landing is exactly the
continuation loop's trigger: with `cfg.max_continuations > 0` the run is
nudged — session-resume first, then a fresh session in the same workspace —
before it is allowed to fail as exit 11, and the exit-11 artifact now names
the terminal event (`evidence.terminal`: `stopReason`, `num_turns`) so a
truncated-looking run is distinguishable from a clean-but-early stop.

**How to recognise it.** In this order, because only the first two are visible in
DevCake:

| where | what you see |
|---|---|
| run report | exit 11 `DEV_BAD_OUTPUT`, `result.json` missing — **and the run ended early and cheaply** (low token count, short duration for the step) |
| activity / transcript | the same tool invocation repeated, then nothing; `grok export` lists the repeats and no conclusion |
| grok's own diagnosis | `You appear to be running empty commands to stay active while waiting for background work. End your turn` — grok injects this into the **tool result** it sends its backend. It never reaches stdout, stderr or the transcript, so DevCake cannot surface it. |

**What to do.** The remedies are the model's, not the cap's: assign a stronger Dev
Type / harness for that Mission Type (`11-admin-panel.md` §3), or decompose the
mission so each step is small enough that the model does not stall (ADR-0012
decomposition depth, `03-mission-lifecycle.md`). If many missions fail this way at once, check the model/backend
(`08-harness-templates.md` §8) — the brake does not cover exit 11.

Scenario captures: `grok_loop_*` under `app/tests/fixtures/harness_streams/`
(grok-build 0.2.112).

## 3. `DEVCAKE-FAILED` semantics

After `max_attempts` (default 3) counted failures of the **same step** (mission + type):

1. Add the `DEVCAKE-FAILED` label (one of the managed labels in `ALL_LABELS`, `02-domain-model.md` §5 / `domain/model.py`).
2. Post a comment: last error class + message, attempt count, and the OpenObserve trace link for the final attempt.
3. Stop scheduling the Mission (derivation row 8).
4. **Recovery is human:** remove the label → the Mission derives normally again; the attempt counter restarts — implemented as a watermark: only failures newer than the mission's last `devcake_failed` audit event **for that PMO instance + `pmo_id`** count toward the next give-up (advisory local state — `10-persistence.md` §5 / §6; bare ids collide across instances).

The counter is **seq-independent** (failed runs post transcripts and advance `seq`, so per-seq counting could retry forever) and resets at the newest of its anchors. Two are policy-independent: the give-up watermark above, and **any finished run for the mission** (a later step completing implies the failing step was resolved, possibly by hand). What comments do is the operator's `attempt_reset` policy (ADR-0026, Policies):

- **`label-ops` (default, strict):** only a non-DevCake comment containing the literal `DEVCAKE-RETRY` resets — the deliberate human gesture. Ordinary comments, human or bot, do not: the pre-0026 rule let any chatty integration (a Linear↔GitHub sync bot, a CI notifier) keep the counter at 1 forever, defeating `max_attempts` entirely.
- **`any-comment`:** the pre-0026 rule — any non-sentinel-signed comment is an intervention and grants fresh attempts. For boards with no integration traffic.
- **`unlimited`:** the app never applies `DEVCAKE-FAILED` at all (comment rule as `label-ops`). An explicit homelab choice for operators whose token cost is measured in watts; a cumulative-cost warning posts to the feed every `review_loop_warning_every` consecutive failures so the mode stays loud. Breakers and `DEVCAKE-SKIP` still act.

## 3a. `DEVCAKE-NEEDS-HUMAN` semantics (not an error class)

The `human_needed` outcome (`03-mission-lifecycle.md` §4a) is a **successful run** — the Dev deliberately reported that only a human can clear an external obstacle. Consequences:

1. The run finishes in state `finished` and **never counts toward `max_attempts`** — no watermark or counter reset is needed.
2. The app adds `DEVCAKE-NEEDS-HUMAN` (derivation row 11 halts scheduling) and posts a baton-pass comment stating precisely what the human must do; an ONBOARD hand-off also restores the status to `backlog`.
3. **Recovery is human:** resolve the obstacle, remove the label → the Mission re-derives its stage next poll and resumes where it left off.

Contrast: `DEVCAKE-FAILED` = involuntary give-up after repeated errors; `DEVCAKE-SKIP` = human opt-out; `DEVCAKE-NEEDS-HUMAN` = clean hand-off.

The label has one other, routine source: per-board **plan approval** (`03-mission-lifecycle.md` §2a) parks every fresh plan, and every decomposition child, under it until a person approves. Same label, same recovery (remove it / **Resume**), but not a hand-off — the run is a plain success and it never enters the loop guardrail count below.

**Loop guardrail (warnings only):** repeats on the same (mission, stage) escalate the baton-pass comment from the 2nd hand-off on ("Hand-off #N … add `DEVCAKE-SKIP` to stop DevCake"); DevCake never auto-parks — the human always decides. The prompts require evidence (quote the exact error) before any hand-off.

**Steward degradation:** 3 consecutive dead STEWARD runs ⇒ the periodic service backs off (`steward_degraded` in `/health` + the admin card); "Run now" remains available and a successful run clears it. Store-derived — restart-safe, no counters to reset.

**`out_of_pipeline_merge` (anomaly, not an error):** a mission's PR found merged while the mission is still mid-pipeline (EXECUTE/REVIEW). Detection tripwire only (docs/14 §2 zone C): comment + audit + health banner — **does not prevent or reverse the merge**. A human may have merged early, or a Dev with a write token may have merged if branch protection allowed it (`auto_merge` off only stops the **app**).

**Blocked-on-a-dead-blocker deadlock:** a Mission whose blocker carries `DEVCAKE-FAILED`/`DEVCAKE-SKIP` stays parked indefinitely (the prerequisite will not complete autonomously). This is surfaced in `/api/v1/missions` reason strings (`04-orchestrator.md` §2); recovery is human — fix the blocker or delete the relation.

## 4a. Model-backend degradation (ADR-0018) — NOT a circuit breaker

When many Devs share one model backend, harness-level faults (`DEV_HARNESS_FAULT`)
often land together. That class therefore has a brake, of a **different kind**
from §4's: store-derived, self-healing, and never latched.

**Detection** is derived from the run store in the `steward_service.degraded()`
idiom — no counters, restart-safe. Two predicates, because throttling and
accounting are different questions:

| predicate | condition (recent terminal runs of a dev type) | effect |
|---|---|---|
| `backend_correlated` | ≥2 `DEV_HARNESS_FAULT` spanning ≥2 distinct missions, in the last **3 terminal runs** | may EXCUSE the attempt |
| `backend_degraded` | the above, **or** 3 consecutive faults on one mission among that type's last 3 **mission-bearing** runs (its own selection — a PMO-less Relations Steward run interleaved in the window must not disarm the arm) | THROTTLES to one probe run |

**Throttled, not stopped.** A degraded Dev Type dispatches at most one run at a
time. That probe *is* the half-open: it is what lets the condition clear itself,
which is why there is no timer and no operator "retry" control. Successes are
included in the detection window on purpose — evicting fault evidence is the
clearing mechanism, and two green runs clear it.

**Escape hatches (both required).** The evidence *is* the faults, so an armed
detector would otherwise excuse every later fault, re-arm the window, and retry a
permanently bad model id forever with no give-up:

1. A given (mission, mission type) step may be excused at most **3** times; after
   that its failures count and it reaches `DEVCAKE-FAILED` normally. The same
   bound covers `DEV_FORGE`, which latches no breaker and would otherwise
   re-dispatch forever on a bad branch name or a DNS failure.
2. `DEV_TURN_BUDGET` is a separate class precisely so it can never be correlated:
   turn exhaustion is deterministic, and a fleet that all hit the same cap must
   not be excused into an unbounded retry against a wall that will never move.

**Surface:** `dev_backend_degraded` in `/health` (dev_type → reason), shaped like
`poll_degraded` and deliberately OUTSIDE `circuit_breakers` — the SPA renders
that map as one alert whose remediation says "refresh the credential", which is
actively wrong here. The SPA alert is a **warning**, not critical, and says
explicitly that no credential change is needed. Span: `dev.backend_degraded`, on
transition into degradation only (never `breaker.trip`, which alerts mean "a
human must fix a credential").

**What it covers is now an operator choice (ADR-0026).** By default both
predicates key on `error_class == "DEV_HARNESS_FAULT"` — failures that still
produce text or tools but no `result.json` land on `DEV_BAD_OUTPUT` (exit 11):
counted, unexcused, unthrottled. Operator-visible cases: codex inventing tool
syntax as prose (`08-harness-templates.md` §8); grok silent non-progress halt
(§2b). `brake_on_bad_output` (Policies, default **off**) widens the
evidence set of every arm to `{DEV_HARNESS_FAULT, DEV_BAD_OUTPUT}`, so a
fleet-wide bad-output cascade — including MIXED evidence, some containers
exiting 15 and others 11 — reads as one backend event: correlated exit-11
attempts are excused on their own per-step ledger (same 3-excusal bound) and
the Dev Type throttles to the probe. Unlike exit 15 there is no
container-class precondition — exit 11 has no in-band structured class (the
exit code IS the classification, stamped app-side), so orphan-enriched runs
carry the same evidence value. Off by default because the continuation loop
(ADR-0022) already absorbs most solitary narrate-and-stop exit-11s.

**Who this protects.** Strongest for multi-mission fleets. A deployment running
one mission per Dev Type can never satisfy the ≥2-mission rule, so it gets
throttling only — its failures still count. Dev Types already at
`max_concurrency: 1` see no throttling effect at all. The brake is per-Dev-Type
while the fault is per-backend; DevCake has no first-class backend concept.

**Clear-runs resets it**, unlike §4's auth breakers: this condition *is* run
history, so "start fresh" legitimately forgets it, and it re-derives within two
correlated failures. Each cycle's recomputation also **intersects the map with
the live Dev Type registry**: renaming or deleting a Dev Type would otherwise
leave a permanent entry, since no future run can supply the two greens that
clear it (and the renamed Type would inherit no evidence at all).

## 4. `DEV_AUTH` circuit breaker

Exit 12 trips a **per-Dev-Type breaker** (`circuit_breakers` in `/health`, SPA alert): all scheduling for that Dev Type pauses (its Missions stay queued, unharmed). **Clear by rewriting credentials** — a credential/secret write for that Dev Type (or a successful OAuth completion) removes the breaker entry. There is no interactive "retry" control on the health strip, and half-open is not a manual probe: the write itself is the reset. Rationale: auth failures burn nothing but fail everything — retrying without new credentials is pure waste.

### Harness exit classification (ADR-0018) — when a nonzero harness exit is 12 vs 15 vs 10 vs 16

The entrypoint does **not** map exit 12 from stderr alone. After the harness
process ends, it (1) runs the per-harness fault predicate on **stdout**
(`harness_fault` — empty completion, terminal error, turn budget, …), (2) reads
a structured HTTP status from the **stream** when the CLI reports one
(`harness_api_error_status` — never from model-controlled assistant prose), and
(3) classifies with `classify_nonzero_exit` (`images/common/devcake_dev/domain/fault.py`,
mirrored by `test_entrypoint_classify.py` / `test_harness_captures.py`):

| Order | Condition | Exit | Class |
|---|---|---|---|
| 1 | fault reason is turn budget | **16** | `DEV_TURN_BUDGET` |
| 2 | **distinctive auth** evidence (below) | **12** | `DEV_AUTH` |
| 3 | any other harness fault | **15** | `DEV_HARNESS_FAULT` |
| 4 | no fault, but **generic** stderr auth markers | **12** | `DEV_AUTH` |
| 5 | else | **10** | `DEV_CRASH` |

**Why step 2 outranks step 3.** A false 12 pauses every mission on that Dev Type
until credentials are rewritten. A false 10 or 15 burns attempts (15 may also
throttle the fleet under §4a). So when the stream already looks like a harness
fault, only **unambiguous** credential evidence may override it to 12. Ordinary
OpenAI-compatible gateways often print `authentication` / `unauthorized` on
non-auth rejections — those words alone must **not** win over an in-band fault.

**Distinctive auth** (`auth_evidence_is_distinctive`) is true when either:

1. **Stream HTTP status is 401 or 403** — preferred path. Sources (CLI error /
   result events only, not assistant text):
   - `claude-code`: `api_error_status` on the final `result` event
   - `codex`: transport wording in error / `turn.failed` messages
     (`unexpected status NNN`, `last status: NNN`)
   - `grok-build`: `Unauthorized (NNN)` / `(status NNN` in error events
   - `pi` / `opencode` / `qwen-code`: dialect `api_error_status` extractors
     over error / terminal event text via `HARNESS_STATUS_PATTERNS` (plus
     qwen's `api_error_status` / `[API Error: …]` bodies when present)
2. **Or** stderr matches a **distinctive** marker (word-boundary): currently
   only the grok session phrases `not signed in` and `grok login`.

**Generic auth** (step 4 only — used when there is **no** fault already):
stderr matches `HARNESS_AUTH_MARKERS` (word-boundary): `authentication`,
`unauthorized`, `log in`, plus the same two grok distinctive phrases.
**Deliberately dropped** (false-trip on generic SSO/proxy stderr → exit 10
instead): `signed out`, `please sign in`. Bare `login` / `sign in` are also
absent. When extending either list, err toward not-12 and keep `\b` anchoring.

**Missed 401 falls through to 15**, not 10: the run still counts, still surfaces
as a harness fault, and may correlate under §4a — it just does not latch the
auth breaker. Precision over recall on exit 12 is intentional.

### Forge breakers (exit 13)

Exit 13 trips a **per-repo forge breaker** (`repo:{name}` in `circuit_breakers`) only when the Dev's clone-failure classification is the structured `DEV_FORGE_AUTH` class, which itself requires git's credential wording ("returned error: 403/401", "Authentication failed", "could not read Username", …) — a bare "403" in stderr (rate limit, URL fragment) never halts dispatch. A latched breaker on repo A never stops missions on repo B. Probes latch the breaker only on **definitive** credential/permission failures (HTTP 401/403/404; a GitHub rate-limit 403 is exempt): 5xx/network/probe errors are marked *transient* and neither latch nor clear it. While the breaker is latched, the poll loop re-probes every cycle — a false latch or a restored token self-heals within one poll interval when the probe authenticates with the same credential field the latch recorded (`token` vs `token_ro`), while a genuinely revoked token stays latched (and alerted) until fixed. An **internal** mission repo row stores no such secret (its Dev pair is minted by the internal forge), so its latch is never field-keyed: the per-cycle re-probe runs on the registered service-token adapter, any ok clears it, and the next dispatch remints the pair. On a dual-token notebook card, a healthy write token must not clear a latch tripped by a revoked read token (memory/notebook clones and mirror sync are read-preferred). A `token_ro`-keyed latch self-heals on a successful **read** probe (`can_read`; push is not required); a `token`-keyed latch still requires a writable probe. Startup and the Forge connection test run the same probe and require push permission, so a private repository omitted from a fine-grained PAT is rejected before another Dev starts on that repo.

The app stamps the `DEV_FORGE_AUTH` **class** on exactly the same evidence as the latch: the Dev's structured `error_class`, never the wording of the failure detail. Auth wording without that class — a push rate-limited with "HTTP 403", or any pre-taxonomy image that sends no `error_class` at all — is plain `DEV_FORGE`, i.e. excusal-bounded (§4a) instead of exempt. The pairing is the whole safety argument: `DEV_FORGE_AUTH` is uncounted with **no** cap (§2), which is bounded only because it always latches the breaker, so stamping the class on marker evidence alone produced uncounted, breaker-less retries forever. A genuine credential failure that arrives without the structured class (an orphan enriched from Dagu's exit code, a lockstep skew) therefore degrades to a terminating path — `DEVCAKE-FAILED` once the excusals are spent — rather than a livelock.

## 5. Poison messages

Per `09-messaging.md` §4: an ingress entry failing 5 handling attempts moves to `devcake:dead` as a metadata-only record and is XACKed + XDEL'd, emitting an `ingress.poison` span (ERROR status — `12-observability.md` §2). Malformed entry bodies (non-JSON `m`) are dead-lettered the same way from raw fields — a poison pill can never loop through reclaim forever. Chunk groups are exempt while still receiving new chunks (they poison only after 300 s of stall, `09-messaging.md` §4). The affected run recovers through the normal failure machinery — watchdog timeout for active runs, or the finalize-stall backstop for a `finalizing` run whose poisoned entry can no longer resume it (`04-orchestrator.md` §5) — then reschedule; `devcake:dead` is capped at ~1000 records for inspection.

## 6. Alerting (v0)

OpenObserve scheduled alerts (`scripts/provision_oo.py`, needs
`OO_ALERT_WEBHOOK` in `.env`), each an SQL condition over the traces stream
`default` — there is no metrics pipeline in v0 (`12-observability.md` §4).
Destination name: **`devcake-webhook`**. Alert names and periods below match
the script (period is minutes; threshold is the SQL result `>=` count):

| Alert name | Condition (SQL over traces) | Period (min) | Threshold |
|---|---|---|---|
| `devcake-give-up` | `operation_name = 'mission.give_up'` | 10 | 1 |
| `devcake-kills` | `operation_name = 'watchdog.kill'` | 10 | 1 |
| `devcake-needs-human` | `audit.event` with `devcake_audit_action = 'devcake_needs_human'` (audit log is span-mirrored so this can fire) | 15 | 1 |
| `devcake-dev-auth-breaker` | `operation_name = 'breaker.trip'` (DEV_AUTH or forge). Separately, `dev.backend_degraded` spans signal model-backend throttling (§4a) — **not** this alert | 15 | 1 |
| `devcake-pmo-forge-transient` | `poll.instance` with `devcake_outcome = 'PMO_TRANSIENT'` **or** `forge.probe_transient` | 15 | 3 |
| `devcake-poison` | `operation_name = 'ingress.poison'` | 10 | 1 |
| `devcake-daily-cost` | `SUM(devcake_cost_usd)` on `run.finalize` where not null | 1440 (24 h) | `OO_DAILY_COST_ALERT_USD` (default **50**) |

Without `OO_ALERT_WEBHOOK`, the script skips alerts (exit 0). Re-post is
idempotent: an “already exists” body is success; other errors fail loud
(`12-observability.md` §5).

## 7. Blanket-exception policy

Silent partial failure — a swallowed exception in a loop that keeps looking
healthy — is this system's most dangerous failure mode: poll cycles, sweeps,
and teardown paths all deliberately outlive individual errors. The policy that
keeps that deliberate without becoming sloppy:

- **Lint-enforced** (`app/ruff.toml`, rule `BLE001`; runs in CI and
  `scripts/ci_suite.sh`): every production `except Exception` satisfies one of
  three arms — (1) **narrowed** to the types the code can actually handle, or
  (2) carries `# noqa: BLE001 — <one-line justification>` naming the contract
  that makes a blanket catch correct, or (3) is a handler that **logs**
  (`log.exception` / equivalent) under a sanctioned contract below. Ruff's
  BLE001 already accepts logged handlers without a noqa; the noqa form is
  still preferred at long-lived seams (teardown, routing degrade) so the
  contract is readable at the site. Justifications live **inline** — this
  section defines the vocabulary, never a site inventory (it would drift).
- **Sanctioned contracts** (the justification — or the log line of arm 3 —
  should name one):
  *loop guard* (a poll/sweep/consumer cycle must survive any single item's
  failure — the failure is logged and, where one exists, surfaced via
  `poll_degraded`/`blocked_reasons`/a breaker); *probe* (health and connection
  probes map any failure to `ok: False` — `/health` must never 500);
  *best-effort teardown/rollback* (cleanup continues past individual failures,
  each logged; the original error, not the cleanup's, surfaces); *degrade with
  record* (fall back to a default and record it — bundled skill copy, warnings
  list, quarantine).
- **Never sanctioned:** a blanket catch whose handler neither logs, records,
  falls back visibly, nor re-raises. None remain under the linted scope, and
  the lint keeps it that way.
- **Scope (be honest about it):** the gate runs `ruff check devcake tests`
  (`scripts/ci_suite.sh`, `.github/workflows/ci.yml`) — it covers `app/devcake`
  and the test tree. Everything under `images/` (including
  `images/common/dev_entrypoint.py` and the `devcake_dev/` package) runs INSIDE
  every Dev container and is **not** under this gate; its blanket handlers
  follow the same contracts by convention but are not lint-enforced (a standing
  follow-up is to extend the gate to `images/`).
- Test code is exempt (`tests/*` per-file ignore) — tests legitimately catch
  broadly.

Two ADR-0035 lanes are sanctioned degrade-with-record instances of this
policy, by contract: a **`.claims/` write failure** is audited
(`claims_append_failed` / `claims_list_failed` / `claims_skip_nowrite`)
and never fails the discovering run or withholds feed memorialization;
a **scheduled-task fire failure** is recorded as `failed` in the
`state/cron_outcomes.json` ledger — three consecutive automatic
failures set `cron_degraded` (`/health` + SPA alert) and pause only the
schedule; Run-now keeps working and one success re-arms.
