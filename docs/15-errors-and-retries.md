# 15 — Errors and Retries

> **Audience:** implementers. One taxonomy; every component maps its failures into it.
> **Depends on:** `04-orchestrator.md` (attempts, watchdog), `07-dev-runtime.md` (exit codes), `09-messaging.md` (poison messages).

## 1. Error classes

| Class | Examples / mapping | Nature |
|---|---|---|
| `PMO_TRANSIENT` | Linear 429/`RATELIMITED`, 5xx, network | retryable |
| `PMO_PERMANENT` | Linear auth failure, 404 on team | config/credential problem |
| `PMO_GONE` | Mission deleted mid-run | external, informational |
| `FORGE_TRANSIENT` | forge 429/5xx/network | retryable |
| `FORGE_PERMANENT` | auth failure, branch protection blocks merge | config problem |
| `DEV_CRASH` | exit 10 (harness crash), 14 (MCP setup), 20 (entrypoint); vanished container | counted attempt |
| `DEV_TIMEOUT` | exit 124 / watchdog kill | counted attempt |
| `DEV_AUTH` | exit 12 | circuit breaker (§4) — **not** a counted attempt |
| `DEV_BAD_OUTPUT` | exit 11: `result.json` missing/invalid/illegal outcome | counted attempt |
| `LABEL_CONFLICT` | ≥2 stage labels (derivation row 6) | human-resolve |
| `EXTERNAL_TRANSITION` | human changed status/label mid-run (`04-orchestrator.md` §4) | **not an error** — first-class outcome |
| `CONFIG_INVALID` | bad config file / failed validation | blocks startup or rejects the write |

## 2. Retry matrix (normative)

| Class | Retryable | Counts toward `max_attempts` | Who retries | User-visible surface |
|---|---|---|---|---|
| `PMO_TRANSIENT` | yes — exp backoff 1s→60s, jitter, max 5, then skip the poll cycle / requeue finalization | no | app | health strip if persistent |
| `PMO_PERMANENT` | no | no | — | health strip error; poll paused |
| `PMO_GONE` | no | no | — | WARN log; run canceled; artifacts kept locally |
| `FORGE_TRANSIENT` | yes — same backoff | no | app (finalization side effects) / Dev (clone/push, 3 in-run tries) | health strip if persistent |
| `FORGE_PERMANENT` | no | no | — | PMO comment + health strip (e.g. merge blocked, `06-forge-adapter.md` §5) |
| `DEV_CRASH` | yes — by natural rescheduling (INV-3) | **yes** | scheduler (next cycle) | after cap: `DEVCAKE-FAILED` (§3) |
| `DEV_TIMEOUT` | yes — same | **yes** | scheduler | same |
| `DEV_BAD_OUTPUT` | yes — same | **yes** | scheduler | same |
| `DEV_AUTH` | no — pointless until creds fixed | **no** | — | circuit breaker (§4) + health strip |
| `LABEL_CONFLICT` | n/a — skipped until resolved | no | — | one PMO comment (deduped via local state) asking a human to fix; metric |
| `EXTERNAL_TRANSITION` | n/a | no | — | explanatory PMO comment; run's artifacts already posted |

Retries of Dev work are never in-place: a failed attempt ends the container; the Mission's label never advanced (INV-3), so the next poll cycle re-derives and re-dispatches with `attempt_of_step + 1`.

## 3. `DEVCAKE-FAILED` semantics

After `max_attempts` (default 3) counted failures of the **same step** (mission + type):

1. Add the `DEVCAKE-FAILED` label (one of the nine managed labels, `02-domain-model.md` §5).
2. Post a comment: last error class + message, attempt count, and the OpenObserve trace link for the final attempt.
3. Stop scheduling the Mission (derivation row 8).
4. **Recovery is human:** remove the label → the Mission derives normally again; the attempt counter restarts — implemented as a watermark: only failures newer than the mission's last `devcake_failed` audit event count toward the next give-up (advisory local state — `10-persistence.md` §5).

## 4. `DEV_AUTH` circuit breaker

Exit 12 trips a **per-Dev-Type breaker**: all scheduling for that Dev Type pauses (its Missions stay queued, unharmed), the health strip shows the tripped state, and a PMO comment is posted on the mission that tripped it. Half-open probe: the next config write touching that Dev Type's credentials (or a manual "retry" from the admin panel health strip) closes the breaker. Rationale: auth failures burn nothing but fail everything — retrying without new credentials is pure waste.

## 5. Poison messages

Per `09-messaging.md` §4: an ingress entry failing 5 handling attempts moves to `devcake:dead`, is XACKed, and increments `devcake.errors.total{class="poison"}`. The affected run is marked `failed` (`DEV_CRASH` rules) so its Mission recovers by rescheduling; the dead entry is kept 7 days for inspection.

## 6. Alerting (v0)

OpenObserve scheduled alerts, provisioned at M7:

1. any `DEVCAKE-FAILED` event (`devcake.runs.total{outcome="devcake_failed"}` > 0, 5-min window);
2. tripped `DEV_AUTH` breaker;
3. `PMO_TRANSIENT`/`FORGE_TRANSIENT` persistent > 15 min;
4. poison message;
5. daily cost threshold on `devcake.cost.usd.total` (operator-configured).
