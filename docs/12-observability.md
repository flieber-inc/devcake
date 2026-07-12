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

- All Python services and Dev entrypoints export **OTLP HTTP directly to OpenObserve** — no collector in v0 (a collector is the documented future insertion point for sampling/routing).
- Endpoints (org segment required): `http://openobserve:5080/api/default/v1/traces`, `…/v1/logs`, `…/v1/metrics`; auth via `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(email:password)>"` (or a v0.91+ org ingestion token).
- Container **stdout** of every compose service (incl. `dagu`, `redis`) is also shipped to OpenObserve (fluent-forward or filelog shipper, `13-deployment.md` §7) so non-instrumented services remain searchable.
- `service.name`: `devcake-app`, `devcake-admin`, `devcake-dev` (Dev entrypoints; the run's `dev_type` is an attribute, not the service name).

## 2. Span taxonomy (normative)

| Span | Parent | Emitted by | Required attributes |
|---|---|---|---|
| `poll.cycle` | root | app | counts: missions seen/candidates/dispatched |
| `mission.evaluate` | `poll.cycle` | app | `devcake.mission.*` |
| `mission.dispatch` | `poll.cycle` | app | `devcake.mission.*`, `devcake.run.id`, `devcake.dev_type` |
| `dev.run` | *linked to `mission.dispatch` via TRACEPARENT env* | Dev entrypoint | full registry incl. `devcake.tokens.*`, `devcake.cost.usd`, `devcake.outcome` |
| `harness.exec` | `dev.run` | Dev entrypoint | `devcake.harness` |
| `pmo.{op}` | caller | app | op = `list/get/comment/upload/set_status/swap_labels/create/cancel` |
| `forge.{op}` | caller | app | op = `ensure_pr/comment/approve/merge` |
| `redis.publish` / `redis.consume` | caller | both | `devcake.run.id`, message kind |
| `run.finalize` | `redis.consume` | app | `devcake.run.id`, `devcake.outcome`, finalized steps |

**Trace continuity:** the app injects W3C `TRACEPARENT` into the Dev container env (`07-dev-runtime.md` §3); one trace therefore spans dispatch → container execution → finalization. This is the primary debugging view: "show me everything about run X" is one trace ID.

## 3. Attribute registry (normative — spelled exactly)

```
devcake.mission.id          devcake.mission.key        devcake.mission.type
devcake.dev_type            devcake.harness            devcake.pmo.system   (= "linear")
devcake.run.id              devcake.run.seq            devcake.run.attempt
devcake.tokens.input        devcake.tokens.output
devcake.tokens.cache_read   devcake.tokens.cache_write
devcake.cost.usd            devcake.outcome            (result.json outcome | error class)
```

Every log line from app and Dev entrypoint carries `devcake.run.id` and `devcake.mission.key` for correlation.

## 4. Metrics

| Metric | Type | Labels |
|---|---|---|
| `devcake.runs.active` | gauge | `dev_type` |
| `devcake.runs.total` | counter | `outcome`, `dev_type`, `mission_type` |
| `devcake.tokens.total` | counter | `dev_type`, `mission_type`, `direction` (input/output) |
| `devcake.cost.usd.total` | counter | `dev_type`, `mission_type` |
| `devcake.review_loops` | counter | `mission_key` |
| `devcake.poll.duration` | histogram | — |
| `devcake.errors.total` | counter | `class` (per `15-errors-and-retries.md`) |

Token/cost numbers are emitted **twice by design**: human-facing in the activity-feed report (INV-5) and machine-facing here — OpenObserve is the cost dashboard.

## 5. Pre-provisioned dashboards (created at M0/M3, `16-roadmap.md`)

1. **Cost** — `devcake.cost.usd.total` and `devcake.tokens.total` by dev_type/mission_type; top-10 missions by cost.
2. **Reliability** — runs by outcome, error classes over time, `DEVCAKE-FAILED` events, review-loop counts.
3. **Throughput** — active runs vs caps, poll duration, queue depth (candidates not dispatched).

The admin panel's Logs tab deep-links to these (`11-admin-panel.md` §5).

## 6. Run-failure log stream (`run_failures`) — the executor's dying words

Discovered live (2026-07-11, first real-world mission): when a Dev container
dies before it can emit telemetry — bad credentials, clone failure, crashed
entrypoint — **nothing reaches OpenObserve**. Fluent-bit ships only the
dagu/redis containers' own stdout (`13-deployment.md` §7); Dev containers are
spawned via docker.sock with the default logging driver and removed on exit
(`keep_container: false`). The only surviving post-mortem is the Dagu run
record, whose per-step `error` field embeds a stderr tail.

So every kill (`watchdog.kill`: timeout, dead-before-start, stale heartbeat,
reconciliation-orphaned) ships one JSON record to the OO log stream
`run_failures` before finalizing:

| field | content |
|---|---|
| `run_id`, `mission_key`, `mission_type`, `dev_type`, `seq` | run identity |
| `outcome` | `failed` / `timed_out` / `orphaned` |
| `reason` | the watchdog's kill reason |
| `trace_id` | from the run's traceparent — joins with the trace in OO |
| `detail` | Dagu step errors + stderr tail, **redacted** (`14-security.md` §5) |

Shipping is best-effort (`push_oo_log` never raises): losing a log record must
not break the kill path. Dev-reported failures (`outcome: failure` in
`run.artifacts`) are NOT shipped here — they already post a full transcript to
the PMO and their spans reach OO normally.
