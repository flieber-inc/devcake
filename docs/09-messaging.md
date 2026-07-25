# 09 — Messaging: The Redis Protocol Between Devs and the App

> **Audience:** implementers of both sides (app ingress consumer; shared Dev entrypoint).
> **Depends on:** `07-dev-runtime.md` (lifecycle), `04-orchestrator.md` (finalization).
> **Decision record:** `adr/0001-redis-streams-for-dev-callback.md`.

Redis Streams mediate **all** Dev↔app traffic. Redis is a **transport buffer, never a source of truth**: a lost message is recoverable because artifacts also exist in Dagu run logs, and the Mission's label was never advanced (INV-3) — the step simply re-runs.

The app's domain programs against **`MessagingPort`** (`ports/messaging.py`); the production adapter is `adapters/redis/messaging.py`. Per-run ACL creation is also the first step of `RunBootstrap.launch` (`04-orchestrator.md` §3.1).

## 1. Topology

| Stream | Direction | Consumers |
|---|---|---|
| `devcake:ingress` | all Devs → app | consumer group `app` (single logical consumer; group enables reclaim) |
| `devcake:reply:{run_id}` | app → one Dev | the Dev blocks on `XREAD` for request/reply; stream deleted at finalization |

## 1a. Per-run authentication (Redis ACLs)

Dev containers execute LLM-driven work influenced by mission text (trusted by
design as product input — `14-security.md` §3) — they are still treated as
**potentially adversarial peers on the network** for Redis isolation. Redis
therefore runs with a password-protected default user (app-only) and **per-run
ACL users** the app manages:

- At dispatch, the app runs Redis 7 key selectors:
  `ACL SETUSER dev-{run_id} on >{random-password} %W~devcake:ingress %RW~devcake:reply:{run_id} +xadd +xread +xlen +ping +client|setinfo`
  — the Dev can **write** (XADD) to the shared ingress and **read/write** only its own reply stream. It **cannot XREAD ingress** (ingress envelopes carry plaintext `auth` of concurrent runs). It cannot read other runs' reply streams or any other key.
- The credentials reach the Dev as the `REDIS_USER`/`REDIS_PASSWORD` container env, interpolated from Dagu params. Params are visible only through the Dagu UI/API (authenticated, loopback, dedicated host — `13-deployment.md` §4, `14` §0). Residual exposure (operators reading Dagu run history) is accepted under that posture: the user is deleted at finalization.
- **Forgery guard:** every Dev→app envelope carries `auth: <its redis password>`. The Run record stores only its SHA-256 verifier; the raw credential is never persisted in `/data`. The app verifies `(run_id, auth)` before processing — a Dev can physically `XADD` a message claiming another `run_id` (ACLs can't inspect payloads), but it cannot forge the token, so spoofed artifacts are rejected. Without write-only ingress, a concurrent Dev could XREAD other passwords and forge finalizations; the selectors close that path.
- The app deletes the ACL user in finalization (idempotent step) and on watchdog kill; startup reconciliation sweeps `ACL LIST` for `dev-*` users with no live Run.

## 2. Envelope (all messages)

```jsonc
{
  "v": 1,                 // envelope schema version
  "run_id": "LINEAR-ENG-142-3-EXECUTE-9GX2TQ",   // human-readable run id (02-domain-model.md §7)
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
| `runspec.get` | Dev→app | `{}` | Request/reply: the entrypoint's **first** message (before `run.started`). App responds with `runspec.result` `{env: {…}, credential_files: [{path_hint, content, mode}], mcp_setup_commands: […], skills: [{name, files: [{path, content_b64}]}], skills_dir, extra_repos: […], activity_repo: {url, clone_user, token} (null when absent), prompt}` (`activity_repo` — ADR-0014 D4: the shared-RO clone spec for the mission's activity repo; absent/null ⇒ the entrypoint materializes the folder from `activity.get` instead) — the stage-2 environment, credential material, MCP plugin setup commands, and skill-store files for this run, scoped to its Dev Type (`07-dev-runtime.md` §3, §5 steps 4b/5). **Secrets built on request:** the secret half of the payload is derived from current config the moment an authenticated *active* run asks (nothing secret is at rest between dispatch and `runspec.get`, and no TTL can expire it under a slow container start or Redis restart). **Secrets in transit:** the app `XDEL`s the reply entry as soon as the Dev acknowledges (`runspec.ack` below), and the reply stream carries a short TTL (§5); this channel exists because Dagu params are UI-visible (`14-security.md` §4). |
| `runspec.ack` | Dev→app | `{}` | Dev confirms it has the runspec; app `XDEL`s the secret-bearing `runspec.result` reply entry so credentials do not linger on the reply stream. |
| `runspec.error` | app→Dev | `{error}` | App cannot build a runspec for this run; entrypoint prints the error and exits 20. |
| `run.started` | Dev→app | `{container_hostname}` | After a successful `runspec.get`; app marks Run `running`, stamps `started_at`. |
| `run.heartbeat` | Dev→app | `{phase}` | Every 30 s (immediate first beat); watchdog liveness input (`04-orchestrator.md` §5). Deliberately span-free (`12-observability.md` §2). |
| `run.log` | Dev→app | `{lines: […]}` — condensed live harness output, batched ≤ 50 lines / ~2 s, entrypoint-truncated to 2000 chars/line (`08-harness-templates.md` §1a). The entrypoint announces harness start and emits a liveness line every 60 s while no user-visible model output arrives; it does not expose hidden reasoning. OAuth helper mode sends `{oauth_url, code}` / `{oauth_error}` | Live output relay: `lines` payloads are **redacted** (`14-security.md` §7) and appended to `/data/state/runlogs/{run_id}.log`, which feeds the admin panel's run terminal over SSE (`11-admin-panel.md` §4). Non-`lines` shapes go to the app log and the OAuth manager. Advisory telemetry (INV-1): capped at 20k lines Dev-side / 10 MB app-side; a lost batch is harmless. |
| `activity.get` | Dev→app | `{}` | Request/reply: app responds on `devcake:reply:{run_id}` with `activity.result` `{mission_md, activity_md, attachments: [{filename, content_b64}]}` (ADR-0014: `mission_md` = the brief; the clone-first entrypoint uses this channel as the degraded fallback). |
| `oauth.result` | Dev→app | `{content}` | OAuth helper mode: the harness auth-file contents after device-code login. |
| `run.artifacts` | Dev→app | `{result, transcript_md, last_message_md?, token_report, plan_md?, exit_code, error_class?, error_detail?, evidence?, bad_output_reason?, recovered_result_path?}` — on FAILURE `result` is null and `exit_code` carries the entrypoint's code; `error_class`/`error_detail` are the structured classification (exits 10/11/13/14/15/16) and `evidence` the bounded workspace forensics (ADR-0018) — `last_message_md` (ADR-0014): the Dev's final message for the inline step comment; shrinkable alongside `transcript_md`/`plan_md` — `result` = parsed `result.json`; `plan_md` present when the run produced `/workspace/out/PLAN.md` (PLAN runs, and ONBOARD runs that attached an opportunistic plan — `03-mission-lifecycle.md` §1.2) | The final message; triggers finalization. Payloads > **512 KB** are chunked as `{chunk, of, chunk_id, sha256, data}`. A group is capped at 128 chunks / 50 MiB; the process caps all active groups at 16 / 100 MiB, and verifies the full-payload hash before JSON parsing. An oversized payload is shrunk Dev-side before sending — `transcript_md`/`plan_md` are deterministically halved with an explicit truncation notice until the blob fits — so `result`/`token_report` always ship and a finished run is never lost to its own transcript size. |

## 4. Delivery semantics

- **At-least-once.** The Dev-side publisher retries each XADD four times with exponential backoff (0.25 s → 1 s); final artifact publication failure fails the entrypoint.
- **Safe redelivery.** A redelivered `run.artifacts` re-enters finalization; already-applied side effects are skipped via `run.finalized_steps` (`04-orchestrator.md` §4). Terminal-state guards refuse to re-open finished/failed/orphaned runs — there is no blanket global `(run_id, kind)` dedupe table.
- **XACK + XDEL after durable handling.** Non-chunk messages are removed only after the handler succeeds. Every chunk remains pending until the complete, verified group has finalized successfully; the whole group is then acknowledged and deleted together.
- **Reclaim:** on startup and every 60 seconds, `XAUTOCLAIM` retries pending entries idle > 60 seconds (an app crash mid-finalization leaves the entry pending; reclaim resumes it — `04-orchestrator.md` §6.4).
- **Poison messages:** after 5 deliveries, a single metadata-only dead-letter record identifies the failed message or chunk group without copying its auth or payload; all group members are XACKed + XDEL'd. A malformed (non-JSON) entry body is dead-lettered the same way from its raw fields. A chunk group that received a new chunk within the last 300 s (5× the reclaim interval) is never poisoned — buffered chunks stay pending by design, so a slow multi-chunk upload accumulates deliveries without being dead; only a stalled group is destroyed. `devcake:dead` is capped at approximately 1000 records.

## 5. Retention and persistence

- Successfully handled ingress entries are immediately XACKed + XDEL'd; Redis retains only work that is pending/retrying plus the bounded dead-letter stream. Clear-runs also XTRIMs the ingress stream as part of the operator wipe (`11-admin-panel.md` §4).
- Reply streams are deleted at finalization and expire after 15 minutes. Run-spec secret material is never at rest in Redis: it is built from config at `runspec.get` time, exists only inside the short-TTL reply entry, and that entry is XDEL'd on `runspec.ack`.
- Redis runs with `appendonly yes`, `appendfsync everysec` (`13-deployment.md`) — messages survive a Redis restart; the acceptable loss window is ≤ 1 s of acknowledged entries, covered by the re-run recovery property above.
