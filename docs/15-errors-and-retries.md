# 15 — Errors and Retries

> **Audience:** implementers. One taxonomy; every component maps its failures into it.
> **Depends on:** `04-orchestrator.md` (attempts, watchdog), `07-dev-runtime.md` (exit codes), `09-messaging.md` (poison messages).

## 1. Error classes

| Class | Examples / mapping | Nature |
|---|---|---|
| `PMO_TRANSIENT` | Linear 429/`RATELIMITED`, 5xx, network — adapters signal it by raising `PMOTransient` (`ports/pmo.py`) | retryable |
| `PMO_PERMANENT` | Linear auth failure, 404 on team | config/credential problem |
| `PMO_GONE` | Mission deleted mid-run | external, informational |
| `FORGE_TRANSIENT` | forge 429/5xx/network | retryable |
| `FORGE_PERMANENT` | auth failure, branch protection blocks merge | config problem |
| `DEV_CRASH` | exit 10 (harness crash), 20 (entrypoint); vanished container | counted attempt |
| `DEV_MCP_SETUP` | exit 14: an `mcp_setup_commands` entry failed or hit the 300 s per-command cap; `run.error` carries the command + stderr tail | counted attempt |
| `DEV_TIMEOUT` | exit 124 / watchdog kill | counted attempt |
| `DEV_AUTH` | exit 12 — harness credential wording per the §4 marker contract | circuit breaker (§4) — **not** a counted attempt |
| `DEV_FORGE_AUTH` | exit 13 with GitHub/GitLab auth/permission evidence | global forge circuit breaker; no run dispatch until the configured token can push |
| `DEV_BAD_OUTPUT` | exit 11: `result.json` missing/invalid; app-side: structurally invalid payload behind a legal outcome (empty decomposition, bad `blocked_by`) | counted attempt |
| `ILLEGAL_OUTCOME` | outcome not in `LEGAL_OUTCOMES` for the run type (`03` §6) — includes forged outcomes (e.g. EXECUTE claiming `reviewed`) | park with `DEVCAKE-SKIP` + comment; audit `illegal_outcome`; never acted on, never retried |
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
| `DEV_MCP_SETUP` | yes — same (a transient install/network failure deserves retries; the deterministic missing-secret case never dispatches at all, `14` §8) | **yes** | scheduler | same |
| `DEV_TIMEOUT` | yes — same | **yes** | scheduler | same |
| `DEV_BAD_OUTPUT` | yes — same | **yes** | scheduler | same |
| `DEV_AUTH` | no — pointless until creds fixed | **no** | — | circuit breaker (§4) + health strip |
| `DEV_FORGE_AUTH` | no — pointless until repository access is fixed | **no** | — | global forge breaker + actionable connection-test error |
| `LABEL_CONFLICT` | n/a — skipped until resolved | no | — | one PMO comment (deduped via local state) asking a human to fix; metric |
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

**`out_of_pipeline_merge` (anomaly, not an error):** a mission's PR found merged while the mission is still mid-pipeline (EXECUTE/REVIEW). Detection tripwire only (docs/14 §2): comment + audit + health banner; a human decides — they may have merged early themselves.

**Blocked-on-a-dead-blocker deadlock:** a Mission whose blocker carries `DEVCAKE-FAILED`/`DEVCAKE-SKIP` stays parked indefinitely (the prerequisite will not complete autonomously). This is surfaced in `/api/v1/missions` reason strings (`04-orchestrator.md` §2); recovery is human — fix the blocker or delete the relation.

## 4. `DEV_AUTH` circuit breaker

Exit 12 trips a **per-Dev-Type breaker**: all scheduling for that Dev Type pauses (its Missions stay queued, unharmed), the health strip shows the tripped state, and a PMO comment is posted on the mission that tripped it. Half-open probe: the next config write touching that Dev Type's credentials (or a manual "retry" from the admin panel health strip) closes the breaker. Rationale: auth failures burn nothing but fail everything — retrying without new credentials is pure waste.

**Harness-auth marker contract** (the Dev entrypoint's `classify_harness_failure`, mirrored by `test_entrypoint_classify.py`): a nonzero harness exit maps to 12 only when the stderr tail carries credential wording — `authentication`, `unauthorized`, `log in`, plus the grok CLI's revoked-session phrases `not signed in`, `please sign in`, `signed out`, `grok login`. Markers are **precise phrases by design**: a false 12 pauses the entire Dev Type until a human re-uploads credentials, while a false 10 merely burns one attempt — when extending the list, err toward 10. (Bare `login`/`sign in` are deliberately absent.)

Exit 13 trips the **global forge breaker** only when the Dev's clone-failure classification is the structured `DEV_FORGE_AUTH` class, which itself requires git's credential wording ("returned error: 403/401", "Authentication failed", "could not read Username", …) — a bare "403" in stderr (rate limit, URL fragment) never halts dispatch. Probes latch the breaker only on **definitive** credential/permission failures (HTTP 401/403/404; a GitHub rate-limit 403 is exempt): 5xx/network/probe errors are marked *transient* and neither latch nor clear it. While the breaker is latched, the poll loop re-probes every cycle — a false latch or a restored token self-heals within one poll interval, while a genuinely revoked token stays latched (and alerted) until fixed. Startup and the Forge connection test run the same probe and require push permission, so a private repository omitted from a fine-grained PAT is rejected before another Dev starts.

## 5. Poison messages

Per `09-messaging.md` §4: an ingress entry failing 5 handling attempts moves to `devcake:dead` as a metadata-only record and is XACKed + XDEL'd, emitting an `ingress.poison` span (ERROR status — `12-observability.md` §2). Malformed entry bodies (non-JSON `m`) are dead-lettered the same way from raw fields — a poison pill can never loop through reclaim forever. Chunk groups are exempt while still receiving new chunks (they poison only after 300 s of stall, `09-messaging.md` §4). The affected run recovers through the normal failure machinery — watchdog timeout for active runs, or the finalize-stall backstop for a `finalizing` run whose poisoned entry can no longer resume it (`04-orchestrator.md` §5) — then reschedule; `devcake:dead` is capped at ~1000 records for inspection.

## 6. Alerting (v0)

OpenObserve scheduled alerts (`scripts/provision_oo.py`, needs
`OO_ALERT_WEBHOOK` in `.env`), each an SQL condition over the traces stream —
there is no metrics pipeline in v0 (`12-observability.md` §4):

1. any give-up — `mission.give_up` spans in a 5-min window;
1a. any needs-human hand-off — `audit.event` spans with `devcake.audit.action = devcake_needs_human` (the audit log is span-mirrored precisely so this alert can fire);
2. tripped breaker (DEV_AUTH or forge) — `breaker.trip` spans;
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
  falls back visibly, nor re-raises. There are none in the tree; the lint keeps
  it that way.
- Test code is exempt (`tests/*` per-file ignore) — tests legitimately catch
  broadly.
