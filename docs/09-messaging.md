# 09 — Messaging: The Redis Protocol Between Devs and the App

> **Audience:** implementers of both sides (app ingress consumer; `devcake-relay` + Dev entrypoint).
> **Depends on:** `07-dev-runtime.md` (lifecycle), `04-orchestrator.md` (finalization).
> **Decision record:** `adr/0001-redis-streams-for-dev-callback.md`.

Redis Streams mediate **all** Dev↔app traffic. Redis is a **transport buffer, never a source of truth**: a lost message is recoverable because artifacts also exist in Dagu run logs, and the Mission's label was never advanced (INV-3) — the step simply re-runs.

## 1. Topology

| Stream | Direction | Consumers |
|---|---|---|
| `devcake:ingress` | all Devs → app | consumer group `app` (single logical consumer; group enables reclaim) |
| `devcake:reply:{run_id}` | app → one Dev | the Dev blocks on `XREAD` for request/reply; stream deleted at finalization |

## 1a. Per-run authentication (Redis ACLs)

Dev containers execute LLM-driven work influenced by untrusted mission text — they are treated as potentially adversarial peers on the network. Redis therefore runs with a password-protected default user (app-only) and **per-run ACL users** the app manages:

- At dispatch, the app runs `ACL SETUSER dev-{run_id} on >{random-password} ~devcake:ingress ~devcake:reply:{run_id} +xadd +xread +xlen` — the Dev can append to the shared ingress and read *only its own* reply stream. It cannot read other runs' `runspec.result` (credential theft) or any other key.
- The credentials reach the Dev as the `REDIS_USER`/`REDIS_PASSWORD` container env, interpolated from Dagu params. This is deliberate: params are visible only through the Dagu UI/API, which is itself authenticated (`13-deployment.md` §4) and unreachable with Dev-held credentials — so run A's password is invisible to Dev B. Residual exposure (operators reading Dagu run history) is accepted: the user is deleted at finalization, so the password is dead by the time anyone reads it.
- **Forgery guard:** every Dev→app envelope carries `auth: <its redis password>`. The app verifies `(run_id, auth)` against the run's issued credential before processing and strips the field — a Dev can physically `XADD` a message claiming another `run_id` (ACLs can't inspect payloads), but it cannot forge the token, so spoofed `run.artifacts` (e.g. a fabricated REVIEW approval) are rejected and alarmed (`devcake.errors.total{class="forged_message"}`).
- The app deletes the ACL user in finalization (idempotent step) and on watchdog kill; startup reconciliation sweeps `ACL LIST` for `dev-*` users with no live Run.

## 2. Envelope (all messages)

```jsonc
{
  "v": 1,                 // envelope schema version
  "run_id": "ENG-142-3-EXECUTE-9GX2TQ",   // human-readable run id (02-domain-model.md §7)
  "auth": "…",            // the run's Redis password — identity proof, verified & stripped by the app (§1a)
  "kind": "run.started",  // §3
  "ts": "2026-07-11T12:00:00Z",
  "payload": { … }
}
```

Encoded as a single JSON string in the stream entry field `m`.

## 3. Message kinds

| Kind | Direction | Payload | Semantics |
|---|---|---|---|
| `run.started` | Dev→app | `{container_hostname}` | Entrypoint's first act; app marks Run `running`, stamps `started_at`. |
| `run.heartbeat` | Dev→app | `{phase}` | Every 60 s; watchdog liveness input (`04-orchestrator.md` §5). |
| `run.log` | Dev→app | `{lines: […]}` — condensed live harness output, batched ≤ 50 lines / ~2 s, entrypoint-truncated to 2000 chars/line (`08-harness-templates.md` §1a). OAuth helper mode sends `{oauth_url, code}` / `{oauth_error}`; legacy shape `{level, message}` | Live output relay: `lines` payloads are **redacted** (`14-security.md` §5) and appended to `/data/state/runlogs/{run_id}.log`, which feeds the admin panel's run terminal over SSE (`11-admin-panel.md` §3). Non-`lines` shapes go to the app log and the OAuth manager. Advisory telemetry (INV-1): capped at 20k lines Dev-side / 10 MB app-side; a lost batch is harmless. |
| `runspec.get` | Dev→app | `{}` | Request/reply: the entrypoint's **first** message. App responds with `runspec.result` `{env: {…}, credential_files: [{path_hint, content, mode}]}` — the stage-2 environment and credential material for this run, scoped to its Dev Type (`07-dev-runtime.md` §3). **Secrets in transit:** the app `XDEL`s the reply entry as soon as the Dev acknowledges (follow-up `runspec.ack`), and the reply stream carries a short TTL (§5); this channel exists because Dagu params are UI-visible (`14-security.md` §3). |
| `activity.get` | Dev→app | `{}` | Request/reply: app responds on `devcake:reply:{run_id}` with `activity.result` `{activity_md, attachments: [{filename, url_or_b64}]}`. |
| `run.artifacts` | Dev→app | `{result, transcript_md, token_report, plan_md?}` — `result` = parsed `result.json`; `plan_md` present when the run produced `/workspace/out/PLAN.md` (PLAN runs, and ONBOARD runs that attached an opportunistic plan — `03-mission-lifecycle.md` §1.2) | The final message; triggers finalization. Payloads > **512 KB** are chunked: `{chunk: i, of: n, data}` entries with the same `run_id`+`kind`, reassembled by the consumer. |

## 4. Delivery semantics

- **At-least-once.** The Dev-side publisher retries with exponential backoff (1 s → 30 s) while Redis is unreachable; the entrypoint does not exit until `run.artifacts` is XADD-acknowledged.
- **Idempotent consumption.** The app keys handling on `(run_id, kind)`; a redelivered `run.artifacts` re-enters finalization, where each side effect's own idempotency key (`04-orchestrator.md` §4) makes the replay harmless.
- **XACK after durable handling** — for `run.artifacts`, only after the Run file records the completed `finalized_steps`.
- **Reclaim:** on startup and every 5 min, `XAUTOCLAIM` pending entries idle > 60 s (an app crash mid-finalization leaves the entry pending; reclaim resumes it — `04-orchestrator.md` §6.4).
- **Poison messages:** an entry that fails handling 5 times is copied to `devcake:dead` (inspected manually; `15-errors-and-retries.md`), XACKed, and alarmed via a metric.

## 5. Retention and persistence

- `devcake:ingress` trimmed with `XTRIM MAXLEN ~ 10000` (≈ retains the recent-runs history; sizing note: one run ≈ 4–6 entries + chunks).
- Reply streams deleted at finalization; orphaned reply streams expire via `EXPIRE 24h` set at creation — except `runspec.result` entries, which are `XDEL`ed immediately on `runspec.ack` and whose stream carries `EXPIRE 15m` until then (secret material must not linger).
- Redis runs with `appendonly yes`, `appendfsync everysec` (`13-deployment.md`) — messages survive a Redis restart; the acceptable loss window is ≤ 1 s of acknowledged entries, covered by the re-run recovery property above.
