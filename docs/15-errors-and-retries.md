# 15 — Errors and Retries

> **Audience:** implementers. One taxonomy; every component maps its failures into it.
> **Depends on:** `04-orchestrator.md` (attempts, watchdog), `07-dev-runtime.md` (exit codes), `09-messaging.md` (poison messages).

## 1. Error classes

| Class | Examples / mapping | Nature |
|---|---|---|
| `PMO_TRANSIENT` | Linear 429/`RATELIMITED`, 5xx, network — adapters signal it by raising `PMOTransient` (`ports/pmo.py`) | retryable |
| `PMO_PERMANENT` | Linear auth failure, 404 on team | config/credential problem |
| `PMO_GONE` | *taxonomy residual* — no dedicated code path today (a mid-run delete surfaces as ordinary PMO read failure / EXTERNAL_TRANSITION at finalize, not a named `PMO_GONE` branch) | external, informational |
| `FORGE_TRANSIENT` | forge 429/5xx/network; probe-classified transient failures | retryable |
| `FORGE_PERMANENT` | auth failure, branch protection blocks merge | config problem |
| `DEV_CRASH` | exit 10 (harness crash), 20 (entrypoint); vanished container | counted attempt |
| `DEV_MCP_SETUP` | exit 14: an `mcp_setup_commands` entry failed or hit the 300 s per-command cap; `run.error` carries the command + stderr tail | counted attempt |
| `DEV_TIMEOUT` | app watchdog kill via Dagu stop → Run `timed_out` (not an entrypoint exit code) | counted attempt |
| `DEV_AUTH` | exit 12 — harness credential wording per the §4 marker contract | circuit breaker (§4) — **not** a counted attempt |
| `DEV_FORGE_AUTH` | exit 13 carrying the Dev's **structured** `DEV_FORGE_AUTH` classification (auth wording in the detail alone is `DEV_FORGE`) | **per-repo** forge circuit breaker (`repo:{name}`); that repo's missions stop dispatching until the token can push |
| `DEV_HARNESS_FAULT` | exit 15: the harness reported a failure in-band, or produced no output at all, whatever its exit status (ADR-0018) | counted attempt — UNLESS correlated across ≥2 missions (§4a) |
| `DEV_TURN_BUDGET` | exit 16: the harness stopped at its configured `--max-turns` cap | counted attempt; deterministic, so never correlated and never excused |
| `DEV_BAD_OUTPUT` | exit 11: `result.json` missing/invalid; app-side: structurally invalid payload behind a legal outcome (empty decomposition, bad `blocked_by`) | counted attempt |
| `ILLEGAL_OUTCOME` | outcome not in `LEGAL_OUTCOMES` for the run type (`03` §6) — includes forged outcomes (e.g. EXECUTE claiming `reviewed`) | park with `DEVCAKE-SKIP` + comment; audit `illegal_outcome`; never acted on, never retried |
| `LABEL_CONFLICT` | ≥2 stage labels (derivation row 6) | human-resolve |
| `EXTERNAL_TRANSITION` | human changed status/label mid-run (`04-orchestrator.md` §4) | **not an error** — first-class outcome |
| `CONFIG_INVALID` | bad config file / failed validation | blocks startup or rejects the write |

## 2. Retry matrix (normative)

| Class | Retryable | Counts toward `max_attempts` | Who retries | User-visible surface |
|---|---|---|---|---|
| `PMO_TRANSIENT` | yes — **no in-adapter retry ladder.** Adapter raises `PMOTransient`; the poll cycle **skips that PMO instance's segment** and continues with the others; the next poll tick (`poll_interval_seconds`, default 30) re-attempts the sick instance | no | app (next poll tick) | `poll.cycle` outcome / logs; not latched into `poll_degraded` (that field is for permanent per-instance failures) |
| `PMO_PERMANENT` | no | no | — | `poll_degraded` for that instance + SPA alert; other instances keep polling |
| `PMO_GONE` | n/a — residual class only | no | — | *not implemented as a dedicated path* — do not expect a WARN+cancel special case |
| `FORGE_TRANSIENT` | yes — **no exp-backoff matrix.** Poll re-probes forge health each cycle (latched breakers self-heal on a green probe); finalization re-enters via ingress reclaim; adapters may short-sleep only where code has them (e.g. Gitea merge "try again later" 405 retries, GitHub 409 race retries — `06-forge-adapter.md` §5/§7a). Dev clone/push is single-shot (exit 13 on failure) | no | app (poll / finalize resume) | health / breaker surfaces if a probe becomes definitive |
| `FORGE_PERMANENT` | no | no | — | PMO comment + health strip (e.g. merge blocked, `06-forge-adapter.md` §5) |
| `DEV_CRASH` | yes — by natural rescheduling (INV-3) | **yes** | scheduler (next cycle) | after cap: `DEVCAKE-FAILED` (§3) |
| `DEV_MCP_SETUP` | yes — same (a transient install/network failure deserves retries; the deterministic missing-secret case never dispatches at all, `14` §8) | **yes** | scheduler | same |
| `DEV_TIMEOUT` | yes — same | **yes** | scheduler | same |
| `DEV_BAD_OUTPUT` | yes — same | **yes** | scheduler | same |
| `DEV_HARNESS_FAULT` | yes — same | **yes**, unless correlated (§4a) | scheduler | `dev_backend_degraded` in `/health` + SPA warning while throttled |
| `DEV_TURN_BUDGET` | yes — but retrying the same cap cannot help; raise `--max-turns` or assign a stronger Dev Type | **yes** | scheduler | `run.error` names the cap and where to change it |
| `DEV_FORGE` | yes — a forge outage should not burn missions | **no** while the step has excusals (§4a), **yes** once spent | scheduler | after the cap: `DEVCAKE-FAILED` |
| `DEV_AUTH` | no — pointless until creds fixed | **no** | — | circuit breaker (§4) + SPA/health alert |
| `DEV_FORGE_AUTH` | no — pointless until repository access is fixed | **no** | — | per-repo forge breaker + actionable connection-test error |
| `LABEL_CONFLICT` | n/a — skipped until resolved | no | — | derivation unschedulable reason only (`gate_map` / `GET /api/v1/missions`); **no** PMO comment, **no** metric |
| `EXTERNAL_TRANSITION` | n/a | no | — | explanatory PMO comment; run's artifacts already posted |

Retries of Dev work are never in-place: a failed attempt ends the container; the Mission's label never advanced (INV-3), so the next poll cycle re-derives and re-dispatches with `attempt_of_step + 1`.

## 3. `DEVCAKE-FAILED` semantics

After `max_attempts` (default 3) counted failures of the **same step** (mission + type):

1. Add the `DEVCAKE-FAILED` label (one of the ten managed labels, `02-domain-model.md` §5).
2. Post a comment: last error class + message, attempt count, and the OpenObserve trace link for the final attempt.
3. Stop scheduling the Mission (derivation row 8).
4. **Recovery is human:** remove the label → the Mission derives normally again; the attempt counter restarts — implemented as a watermark: only failures newer than the mission's last `devcake_failed` audit event count toward the next give-up (advisory local state — `10-persistence.md` §5).

The counter is **seq-independent** (failed runs post transcripts and advance `seq`, so per-seq counting could retry forever) and resets at the newest of three anchors: the give-up watermark above, **any finished run for the mission** (a later step completing implies the failing step was resolved, possibly by hand), or **the latest human feed comment** (non-sentinel-signed — a human touching the mission is an intervention, and the step deserves fresh attempts).

## 3a. `DEVCAKE-NEEDS-HUMAN` semantics (not an error class)

The `human_needed` outcome (`03-mission-lifecycle.md` §4a) is a **successful run** — the Dev deliberately reported that only a human can clear an external obstacle. Consequences:

1. The run finishes in state `finished` and **never counts toward `max_attempts`** — no watermark or counter reset is needed.
2. The app adds `DEVCAKE-NEEDS-HUMAN` (derivation row 11 halts scheduling) and posts a baton-pass comment stating precisely what the human must do; an ONBOARD hand-off also restores the status to `backlog`.
3. **Recovery is human:** resolve the obstacle, remove the label → the Mission re-derives its stage next poll and resumes where it left off.

Contrast: `DEVCAKE-FAILED` = involuntary give-up after repeated errors; `DEVCAKE-SKIP` = human opt-out; `DEVCAKE-NEEDS-HUMAN` = clean hand-off.

**Loop guardrail (warnings only):** repeats on the same (mission, stage) escalate the baton-pass comment from the 2nd hand-off on ("Hand-off #N … add `DEVCAKE-SKIP` to stop DevCake"); DevCake never auto-parks — the human always decides (founder decision 2026-07-12). The prompts require evidence (quote the exact error) before any hand-off.

**Mapper degradation:** 3 consecutive dead MAPPER runs ⇒ the periodic service backs off (`mapper_degraded` in `/health` + the admin card); "Run now" remains available and a successful run clears it. Store-derived — restart-safe, no counters to reset.

**`out_of_pipeline_merge` (anomaly, not an error):** a mission's PR found merged while the mission is still mid-pipeline (EXECUTE/REVIEW). Detection tripwire only (docs/14 §2 zone C): comment + audit + health banner — **does not prevent or reverse the merge**. A human may have merged early, or a Dev with a write token may have merged if branch protection allowed it (`auto_merge` off only stops the **app**).

**Blocked-on-a-dead-blocker deadlock:** a Mission whose blocker carries `DEVCAKE-FAILED`/`DEVCAKE-SKIP` stays parked indefinitely (the prerequisite will not complete autonomously). This is surfaced in `/api/v1/missions` reason strings (`04-orchestrator.md` §2); recovery is human — fix the blocker or delete the relation.

## 4a. Model-backend degradation (ADR-0018) — NOT a circuit breaker

A shared model backend that degrades makes every Dev container fail at once and
identically. `DEV_HARNESS_FAULT` therefore has a brake, but a **different kind**
from §4's: store-derived, self-healing, and never latched.

**Detection** is derived from the run store in the `mapper_service.degraded()`
idiom — no counters, restart-safe. Two predicates, because throttling and
accounting are different questions:

| predicate | condition (recent terminal runs of a dev type) | effect |
|---|---|---|
| `backend_correlated` | ≥2 `DEV_HARNESS_FAULT` spanning ≥2 distinct missions, in the last **3 terminal runs** | may EXCUSE the attempt |
| `backend_degraded` | the above, **or** 3 consecutive faults on one mission among that type's last 3 **mission-bearing** runs (its own selection — a PMO-less Relations Mapper run interleaved in the window must not disarm the arm) | THROTTLES to one probe run |

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

**Harness-auth marker contract** (the Dev entrypoint's `classify_harness_failure` / `HARNESS_AUTH_MARKERS`, mirrored by `test_entrypoint_classify.py`): a nonzero harness exit maps to 12 only when the stderr tail matches a **word-boundary** regex for credential wording — currently `authentication`, `unauthorized`, `log in`, plus the grok distinctive phrases `not signed in` and `grok login`. **Deliberately dropped** (would false-trip on generic SSO/proxy stderr; those map to exit 10): `signed out`, `please sign in`. Bare `login` / `sign in` are also absent. A false 12 pauses the entire Dev Type until credentials are rewritten; a false 10 merely burns one attempt — when extending the list, err toward 10 and keep `\b` anchoring.

Exit 13 trips a **per-repo forge breaker** (`repo:{name}` in `circuit_breakers`) only when the Dev's clone-failure classification is the structured `DEV_FORGE_AUTH` class, which itself requires git's credential wording ("returned error: 403/401", "Authentication failed", "could not read Username", …) — a bare "403" in stderr (rate limit, URL fragment) never halts dispatch. A latched breaker on repo A never stops missions on repo B. Probes latch the breaker only on **definitive** credential/permission failures (HTTP 401/403/404; a GitHub rate-limit 403 is exempt): 5xx/network/probe errors are marked *transient* and neither latch nor clear it. While the breaker is latched, the poll loop re-probes every cycle — a false latch or a restored token self-heals within one poll interval, while a genuinely revoked token stays latched (and alerted) until fixed. Startup and the Forge connection test run the same probe and require push permission, so a private repository omitted from a fine-grained PAT is rejected before another Dev starts on that repo.

The app stamps the `DEV_FORGE_AUTH` **class** on exactly the same evidence as the latch: the Dev's structured `error_class`, never the wording of the failure detail. Auth wording without that class — a push rate-limited with "HTTP 403", or any pre-taxonomy image that sends no `error_class` at all — is plain `DEV_FORGE`, i.e. excusal-bounded (§4a) instead of exempt. The pairing is the whole safety argument: `DEV_FORGE_AUTH` is uncounted with **no** cap (§2), which is bounded only because it always latches the breaker, so stamping the class on marker evidence alone produced uncounted, breaker-less retries forever. A genuine credential failure that arrives without the structured class (an orphan enriched from Dagu's exit code, a lockstep skew) therefore degrades to a terminating path — `DEVCAKE-FAILED` once the excusals are spent — rather than a livelock.

## 5. Poison messages

Per `09-messaging.md` §4: an ingress entry failing 5 handling attempts moves to `devcake:dead` as a metadata-only record and is XACKed + XDEL'd, emitting an `ingress.poison` span (ERROR status — `12-observability.md` §2). Malformed entry bodies (non-JSON `m`) are dead-lettered the same way from raw fields — a poison pill can never loop through reclaim forever. Chunk groups are exempt while still receiving new chunks (they poison only after 300 s of stall, `09-messaging.md` §4). The affected run recovers through the normal failure machinery — watchdog timeout for active runs, or the finalize-stall backstop for a `finalizing` run whose poisoned entry can no longer resume it (`04-orchestrator.md` §5) — then reschedule; `devcake:dead` is capped at ~1000 records for inspection.

## 6. Alerting (v0)

OpenObserve scheduled alerts (`scripts/provision_oo.py`, needs
`OO_ALERT_WEBHOOK` in `.env`), each an SQL condition over the traces stream —
there is no metrics pipeline in v0 (`12-observability.md` §4):

1. any give-up — `mission.give_up` spans in a 5-min window;
1a. any needs-human hand-off — `audit.event` spans with `devcake.audit.action = devcake_needs_human` (the audit log is span-mirrored precisely so this alert can fire);
2. tripped breaker (DEV_AUTH or forge) — and, separately, `dev.backend_degraded` spans for model-backend throttling (§4a, NOT a breaker) — `breaker.trip` spans;
3. `PMO_TRANSIENT`/`FORGE_TRANSIENT` persistent > 15 min — `poll.cycle` outcome attribute plus `forge.probe_transient` spans;
4. poison message — `ingress.poison` spans;
5. daily cost threshold — SUM of `devcake.cost.usd` over `run.finalize` spans (`OO_DAILY_COST_ALERT_USD`, default 50).

## 7. Blanket-exception policy

Silent partial failure — a swallowed exception in a loop that keeps looking
healthy — is this system's most dangerous failure mode: poll cycles, sweeps,
and teardown paths all deliberately outlive individual errors. The policy that
keeps that deliberate without becoming sloppy:

- **Lint-enforced** (`app/ruff.toml`, rule `BLE001`; runs in CI and
  `scripts/ci_suite.sh`): every production `except Exception` is either
  **narrowed** to the types the code can actually handle, or carries
  `# noqa: BLE001 — <one-line justification>` naming the contract that makes a
  blanket catch correct. The justification lives **inline at the site** — this
  section defines the vocabulary, never a site inventory (it would drift).
- **Sanctioned contracts** (the justification should name one):
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
  and the test tree. `images/common/dev_entrypoint.py` runs INSIDE every Dev
  container and is **not** under this gate; its blanket handlers follow the
  same contracts by convention but are not lint-enforced (a standing follow-up
  is to extend the gate to `images/`).
- Test code is exempt (`tests/*` per-file ignore) — tests legitimately catch
  broadly.
