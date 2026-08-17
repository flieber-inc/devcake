# 12 — Observability: OpenTelemetry Conventions and Cost Telemetry

> **Audience:** everyone writing instrumented code. ALL code must be appropriately spanned/traced and logged in OpenObserve (mission-doc requirement).
> **Depends on:** `07-dev-runtime.md` (TRACEPARENT injection), `13-deployment.md` (endpoints).

## 0. The principle (founder decision, 2026-07-11)

**Everything is traced — no exceptions.** Every method that changes state or
crosses a boundary must be observable in OpenObserve: every outbound HTTP call
(auto-instrumented via `HTTPXClientInstrumentor` — Linear, forge, Dagu, all of
it), every API endpoint (FastAPI auto-instrumentation), and an explicit span for
every internally-triggered action: dispatch, finalization, watchdog kills,
give-ups, sweeps, OAuth flows. The test when adding code: *if this line
misbehaved at 3am, would a trace show it?* If not, add a span. Failure paths
especially — a failure that leaves no trace is a design bug, not an
observability gap.

## 1. Pipeline

- The **app** exports OTLP HTTP directly to OpenObserve; **Dev entrypoints export to the inserted `otel-collector`** (`http://otel-collector:4318/v1/traces` on `devcake_runtime`, M8/ISSUES #13), **unauthenticated** — the collector alone holds the OO credentials and forwards to `http://openobserve:5080/api/{org}/v1/traces` (`otel/collector-config.yaml`). Devs carry no OO credentials at all; there is no `OTEL_EXPORTER_OTLP_BASIC` anywhere. The app deliberately does NOT route through the collector (a sick collector must never blind the control plane). **Residual (dedicated-host posture):** a Dev can forge or flood spans on this host — OO alerts are **ops** signals, not a security boundary (`14-security.md` §10).
- App auth is a Basic header built **in code**, never via `OTEL_*` env vars (no percent-encoding dance), derived from the `OO_INGEST_EMAIL`/`OO_INGEST_PASSWORD` service account (`telemetry/__init__.py`); root creds are used only for admin ops (stream deletion `api/clear.py`, boot-time ingest provision, and `scripts/provision_oo.py` for dashboard/alerts). **Boot auto-provisions** the ingest user (`telemetry/oo_provision.py`, role `service_account`) from `OO_INGEST_*` + root — fail-loud with retries so a filled `.env` + `compose up` is enough; no host-side script for connectivity.
- Container **stdout** of compose services that use the fluentd logging driver (incl. `dagu`, `redis`, **`gitea`**, and others wired in `docker-compose.yml`) is shipped to OpenObserve via fluent-bit (`13-deployment.md` §7) so non-instrumented services remain searchable.
- The **host baker** is not a container. It writes complete files under `/data/harness_outbox/`; the app poll cycle claims each file, ships it through **`push_oo_log("baker", …)`**, then deletes it (the same chokepoint as `run_failures`). A leftover growing `/data/harness_baker.jsonl` is still drained by cursor until it ages out. If the app is already down, the baker POSTs one dying word to OO on loopback with the ingest account — it does not go through the collector (a sick collector must never blind this either). `service.name` for baker transition spans is **`devcake-app`** (the app observes; the host process does not install its own tracer).
- `service.name`: the app OTLP resource is **`devcake-app`** (`telemetry/__init__.py`). Dev entrypoints (shared + hello) set **`devcake-dev`**. There is no `devcake-admin` service name in OTel (admin is nginx + static SPA). The run's `dev_type` is a span attribute, not the service name.

## 2. Span taxonomy (normative)

| Span | Parent | Emitted by | Content |
|---|---|---|---|
| `poll.cycle` | root | app | counts: missions seen/candidates/dispatched; `PMO_TRANSIENT`/`cycle_error` outcomes |
| `poll.instance` | `poll.cycle` | app | one child per configured PMO instance (`devcake.instance`); a per-instance failure marks THIS span, not the cycle |
| `mission.dispatch` | `poll.cycle` (steward runs: `steward.periodic` / `steward.discovery`) | app | `devcake.mission.*`, `devcake.run.id`, `devcake.dev_type`; covers the ACL-user creation, run persist, and Dagu trigger |
| `mission.give_up` | `poll.cycle` | app | ERROR status; covers the `DEVCAKE-FAILED` label write + feed post |
| `sweep.merge` | `poll.cycle` | app | emitted only when the sweep acts (`merged`/`closed`); covers the PMO writes |
| `sweep.merge_retry` | `poll.cycle` | app | one span per acting deferred-retry decision: `merged` / `conflict` / `conflict_handoff` / `merge_failed_transient` / `window_exhausted` (ERROR) |
| `sweep.tracking` | `poll.cycle` | app | emitted when a project auto-completes; covers the PMO writes |
| `steward.periodic` | `poll.cycle` | app | how a DUE periodic steward run resolved: `dispatched` / `already_active` / `concurrency_deferred` / `degraded_skip` (ERROR). Emitted on outcome **transitions** (and every dispatch), not per tick — a steward stuck degraded for hours yields one span, not thousands |
| `steward.discovery` | `poll.cycle` (or the harvest kick's task) | app | how a discovery-lane pass resolved: `dispatched` / `already_active` / `concurrency_deferred` / `degraded_skip` / `mirror_stale` / `secret_env_gate` / `dispatch_skipped` (ADR-0033). Same on-transition emission rule as `steward.periodic` |
| `dev.run` | *linked to `mission.dispatch` via TRACEPARENT env* | Dev entrypoint | full registry incl. `devcake.tokens.*`, `devcake.cost.usd`, `devcake.outcome` |
| `harness.exec` | `dev.run` | Dev entrypoint | `devcake.harness` |
| `ingress.handle` | `dev.run` (via the run's traceparent) | app | one span per handled ingress message (`devcake.kind`: `run.started`, `runspec.get`, `activity.get`, `oauth.result`, `run.artifacts`, OAuth-shaped `run.log`, …). Deliberately span-free: `run.heartbeat` (2/min/run) and streamed `run.log {lines}` batches (one every few seconds while a harness talks) — pure liveness/output noise that would drown the trace |
| `run.finalize` | `dev.run` (via the run's traceparent) | app | `devcake.run.id`, `devcake.outcome`, `devcake.tokens.*`, `devcake.cost.usd`, `devcake.verdict` (+ ERROR status on rejections) |
| `watchdog.kill` | `dev.run` (via the run's traceparent) | app | ERROR status, kill reason, resulting state |
| `baker.dead` / `baker.alive` | root (poll.cycle sibling) | app | Transition only. The host baker heartbeats on `/data`; the **poll cycle** observes it (same chokepoint as `run_failures`). `baker.dead` is ERROR — restart with `./up.sh`. Quiet ticks are span-free. |
| `baker.reconcile` | root | app (replay) | One claimed keep-set order. Children: `baker.compile`, `baker.probe.<row>` (`devcake.baker.cause` = aim/stub/dialect/auth), `baker.prune`. Host baker writes span records to the outbox; poll replays them. Quiet ticks emit nothing. |
| `ingress.forged_drop` | root | app | security event: a message that failed envelope auth was dropped (ERROR) |
| `ingress.poison` | root | app | reliability event: a message group dead-lettered after 5 deliveries (ERROR) |
| `audit.event` | current span (or root) | app | mirrors every audit-log write: `devcake.audit.action` (e.g. `devcake_needs_human`), `devcake.pmo.id` — the needs-human alert queries this (`15-errors-and-retries.md` §6) |
| `breaker.trip` | current span (or root) | app | ERROR status; `devcake.breaker` (dev type or `forge`), `devcake.reason` — breakers are otherwise in-memory only |
| `dev.backend_degraded` | poll cycle | app | ERROR status; `devcake.dev_type`, `devcake.reason` — emitted ONLY on transition into degradation (ADR-0018). Deliberately not `breaker.trip`: that alert means "a human must fix a credential", and this self-heals |
| `forge.probe_transient` | current span (or root) | app | a transient forge-probe failure that did NOT touch the breaker; the >15 min transient alert counts these |
| `oauth.start` / `oauth.result` | API request span | app | `devcake.run.id`, `devcake.dev_type` |
| `system.clear_runs` | API request span | app | deletion counts |
| *HTTP client spans* | caller | app (auto) | every outbound call — Linear GraphQL, GitHub/GitLab REST, Dagu, OpenObserve — via `HTTPXClientInstrumentor`; there are deliberately no hand-rolled `pmo.*`/`forge.*` spans |
| *API request spans* | root | app (auto) | every `/api/v1/*` request via `FastAPIInstrumentor` |

Deliberately span-free besides heartbeats: the watchdog's quiet 10 s scan (its
*actions* — kills and Dagu status probes — are all spanned or auto-instrumented).

**Trace continuity:** the app injects W3C `TRACEPARENT` into the Dev container env (`07-dev-runtime.md` §3), and every app-side consumer of a run message re-extracts it; `TRACEPARENT` rides params into BOTH of a run's containers (provision and harness, ADR-0025), so one trace spans dispatch → provision → harness execution → ingress handling → finalization (or kill). This is the primary debugging view: "show me everything about run X" is one trace ID.

## 3. Attribute registry (normative — spelled exactly)

```
devcake.mission.id          devcake.mission.key        devcake.mission.type
devcake.dev_type            devcake.harness
devcake.run.id              devcake.run.seq            devcake.run.attempt
devcake.tokens.input        devcake.tokens.output      devcake.tokens.total
devcake.tokens.cache_read   devcake.tokens.cache_write devcake.tokens.reasoning
devcake.cost.usd            devcake.cost.usd_estimated devcake.cost.rate_card
devcake.outcome             (result.json outcome | error class)
devcake.discoveries.harvested  (run.finalize; count only — discovery CONTENT
                                never leaves the board, ADR-0033 D8)
devcake.steward.duty           (mission.dispatch — "relations" | "discovery")
devcake.steward.edges_created  devcake.steward.edges_rejected   (run.finalize,
                                relations flavor)
devcake.steward.routes_delivered  devcake.steward.routes_rejected
                               (run.finalize, discovery flavor; counts only)
```

Every log line from app and Dev entrypoint carries `devcake.run.id` and `devcake.mission.key` for correlation.

## 4. Aggregation model: SQL over spans, not a metrics pipeline

v0 emits **no OTel metric instruments**. Every quantity worth aggregating is
already a span attribute (§3) or a log record, and OpenObserve queries them
directly with SQL over the `traces` stream — one pipeline, one place to look.
(A first-class metrics layer is on the roadmap, `16-roadmap.md`.)

Canonical queries (the shapes `scripts/provision_oo.py` installs):

| Question | Query over |
|---|---|
| Cost / tokens by dev type & mission type | `run.finalize` spans: `devcake.cost.usd`, `devcake.tokens.*`, grouped by `devcake_dev_type` |
| Runs by outcome | `run.finalize` spans: `devcake.outcome` (+ `devcake.verdict` for app-level rejections) |
| Failure signals | `watchdog.kill` + `mission.give_up` spans (ERROR status); the `run_failures` log stream (§6) for pre-telemetry deaths |
| Fleet / model-backend faults (ADR-0018) | `dev.backend_degraded` spans (transition into throttle — **not** `breaker.trip`); failed `run.finalize` / `dev.run` with `devcake.outcome` / `devcake.verdict` carrying harness error classes (`DEV_HARNESS_FAULT`, `DEV_TURN_BUDGET`, … — semantics in `15` §1 / §4a) |
| Poison / forgery pressure | `ingress.poison` / `ingress.forged_drop` spans |
| Poll health & queue depth | `poll.cycle` spans: duration + missions seen/candidates/dispatched |

Token/cost numbers are reported **twice by design**: human-facing in the
activity-feed report (INV-5) and machine-facing as `run.finalize` span
attributes — OpenObserve is the cost dashboard.

**`devcake.cost.usd` is a claude-code-only attribute.** It keeps its name over
the stored v1 key (`cost_usd_native`, `adr/0029`) and is set solely from a
natively reported figure; neither `codex` 0.147.0 nor `grok` 0.2.112 emits a
cost field of any kind (`08-harness-templates.md` §5) — no price table invents
one, and a missing cost is written as **null, never 0**, so a cost query returns
claude runs only rather than silently averaging in free-looking runs.

**`devcake.cost.usd_estimated` is the cross-harness spend proxy** (`adr/0021`):
the app-side rate-card estimate, emitted only when the full token split exists
and `config.cost_inputs.rates` maps the model, always accompanied by
`devcake.cost.rate_card` (the card vintage that priced it). It never coalesces
into `devcake.cost.usd` — dashboards that want "reported or estimated" must
say so in their own labels (the provisioned dashboard keeps two panels). The
cross-harness token quantity remains `devcake.tokens.*`: grok fills the full
split plus `total` from its `end` event, codex the split with no total.

## 5. Pre-provisioned dashboard + alerts (`scripts/provision_oo.py`, idempotent)

The **ingest service account** is created/resynced at **app boot** (not by
this script — see §1). The host-side script remains for the **DevCake**
dashboard and optional alerts: all panels SQL over the traces stream —
**Cost per hour (USD, by dev type)** · **Dev runs by outcome (daily)** ·
**Failure signals (kills, give-ups)**. With `OO_ALERT_WEBHOOK` set in `.env`,
the script also provisions the alert set of `15-errors-and-retries.md` §6
against the same stream. The admin panel's Consoles page deep-links here
(`11-admin-panel.md` §5). Safe to re-run; still ensures the ingest user if
you want a host-side check without restarting the app.

## 6. Run-failure log stream (`run_failures`) — the executor's dying words

Discovered live (2026-07-11, first real-world mission): when a Dev container
dies before it can emit telemetry — bad credentials, clone failure, crashed
entrypoint — **nothing reaches OpenObserve**. Fluent-bit ships compose services
that opt into the fluentd driver (`dagu`, `redis`, **`gitea`** — `13-deployment.md`
§7); Dev containers are spawned via docker.sock with the default logging driver
and removed on exit (`keep_container: false`). The only surviving post-mortem
for those Devs is the Dagu run record, whose per-step `error` field embeds a
stderr tail.

*Narrowed 2026-07-12 by the live output relay (`08-harness-templates.md` §1a):
the entrypoint now streams condensed harness output into the Dagu step log
(captured live, survives the container) and into `/data/state/runlogs/` via
`run.log`, so a mid-run death leaves everything printed up to that moment.
The blind spot shrinks to deaths before the harness starts — which is exactly
what this stream covers.*

So every kill (`watchdog.kill`: timeout, dead-before-start, stale heartbeat,
reconciliation-orphaned) ships one JSON record to the OO log stream
`run_failures` before finalizing:

| field | content |
|---|---|
| `run_id`, `mission_key`, `mission_type`, `dev_type`, `seq` | run identity |
| `outcome` | `failed` / `timed_out` / `orphaned` |
| `reason` | the watchdog's kill reason |
| `trace_id` | from the run's traceparent — joins with the trace in OO |
| `detail` | Dagu step errors + stderr tail, **redacted** (`14-security.md` §7) |

Shipping is best-effort (`push_oo_log` never raises): losing a log record must
not break the kill path. Dev-reported failures (`outcome: failure` in
`run.artifacts`) are NOT shipped here — they already post a full transcript to
the PMO and their spans reach OO normally.
