# 07 — Dev Runtime: The Container Contract

> **Audience:** implementers and harness-image authors. Anyone should be able to build a new Dev image from this document alone.
> **Depends on:** `02-domain-model.md` (Run, DevType), `03-mission-lifecycle.md` (`result.json`), `08-harness-templates.md` (per-harness specifics), `09-messaging.md` (Redis protocol).

A **Dev container** is an ephemeral Docker container that performs exactly one Mission Step and exits. It is spawned by Dagu as a sibling of the compose stack (`13-deployment.md`), named `dev-{run_id}`, and attached to the **`devcake_runtime`** network so it reaches `redis`, `otel-collector`, and (when used) internal Gitea by service name. **OpenObserve is not on the runtime network** — Devs export OTLP to the collector only (`12-observability.md`, `14` §10).

Devs are **pure functions from (workspace, prompt) to artifacts**: they never write to the PMO System or mutate any shared state other than pushing a git branch at the very end (INV-4, INV-6). All PMO effects are applied by the app when it consumes the run's artifacts.

## 1. Filesystem layout (normative)

```
/workspace/
  repo/                  # fresh `git clone` of the configured repository, directory named
                         #   exactly after the repository (standard clone output).
                         #   The ONLY place the harness may do its work (INV-6).
  activity/
    MISSION.md           # the brief: key/title/meta/labels, FULL description, mission
                         #   attachments (ADR-0014 — every playbook points here)
    ACTIVITY.md          # faithful mirror of the Mission's feed (format: §2)
    {attachment files}   # every attachment from the feed — including prior steps' full
                         #   session transcripts {seq}_{TYPE}.md — downloaded under its
                         #   original name; collisions get the suffix rule below (seeded
                         #   against ACTIVITY.md and MISSION.md). `.zip` attachments are
                         #   also extracted under `{stem}/…` (zip-slip hardened, size-capped)
  out/
    result.json          # REQUIRED structured outcome (schema: 03-mission-lifecycle.md §6)
    PLAN.md              # PLAN runs only: the produced plan
  .devcake/              # entrypoint scratch: credentials, MCP setup logs, relay socket info.
                         #   Excluded from transcripts and never uploaded.
```

There is **no** `/workspace/out/transcript/` directory. The entrypoint assembles the session transcript **in memory** (`assemble_transcript`) and ships it as `transcript_md` on the `run.artifacts` payload; the app posts it as `{seq}_{TYPE}.md` on the PMO feed.

The workspace is prepared entirely by the container **entrypoint** (not the app): stage-1 env from Dagu, stage-2 (secrets, repo, prompt, …) via Redis `runspec.get` (§3), then the entrypoint materializes the activity folder **clone-first** — a full-history `git clone` of the mission's `activity-*` repo (ADR-0014 D4; `git log -p ACTIVITY.md` works in-container) with the Redis request/reply channel (`activity.get`, `09-messaging.md` §4) as the degraded fallback — clones the repo using injected credentials, runs the Dev Type's MCP setup commands, and only then launches the harness.

## 2. `ACTIVITY.md` format

**Intent (confirmed decision, revised by ADR-0014):** the `activity/` folder is a mini **knowledge base the harness taps into as needed** — queryable, greppable reference material. It is *never* inlined into the prompt; the playbook prompt carries only the mission title/description and points here (`03-mission-lifecycle.md` §7). The folder is three parts: **`MISSION.md`** (the brief — key/title/meta/labels, the FULL description, and mission-level attachments: description-embedded assets + the vendor's native attachment list, files downloaded / links rendered as links), **`ACTIVITY.md`** (a **faithful mirror of the feed as seen in the PMO** — every post and reply inline with its full body, `↳ reply to` nesting, `[attachment: name]` markers at their feed positions; heavy content is naturally attachment-borne because DevCake's own long posts externalize at post time), and the **sibling files** (every attachment's bytes, including prior steps' full session transcripts). If the adapter's full-history hard stop ever trips, `ACTIVITY.md` opens with a loud `⚠ FEED TRUNCATED` banner — never silent.

```markdown
# {mission_key}: {title}
> Brief: MISSION.md (same folder) — description, labels, mission attachments.

## Activity (chronological mirror of the PMO feed)
Entries marked 🧑 HUMAN are instructions/steering from a person — they
are authoritative. Entries marked 🤖 DevCake are DevCake's own records.
### {timestamp} — {author} — {🧑 HUMAN | 🤖 DevCake} ({comment|status_change|attachment})
{↳ reply to {author} @ {timestamp} — when the entry is a threaded reply}
{full body, verbatim}
{[attachment: name] — one line per attachment, at its feed position}
...
```

**Provenance markers (added with adr/0007):** each entry is classified by the comment-provenance sentinel (`03-mission-lifecycle.md` §8a) — a body ending in `` `devcake:v1` `` is DevCake's; everything else is a human's. The classification is never based on `author`, which is unreliable when DevCake posts with the operator's own PMO credentials. Every playbook instructs the Dev to read 🧑 HUMAN entries before starting and to let the most recent human comment win on conflict.

The mirror is strictly chronological and complete — *all* the current activity of the Mission; nothing is omitted or previewed (`get_activity(full=True)` walks the entire history — `05-pmo-adapter.md` §3). Attachments (notably prior DevCake full-session transcripts `N_TYPE.md`, plans, and review reports) are downloaded into `activity/` under their original filenames (name-collision suffix `-2`, `-3`, … — implemented app-side in `activity_payload`, deduping downloaded attachments against each other and against `ACTIVITY.md`/`MISSION.md`). Every `.zip` attachment is kept as the zip **and** expanded under a same-named stem folder (`{name without .zip}/…`) so Devs can read deliverables without unpacking tools; path members are zip-slip hardened and extraction is size-capped (best-effort — corrupt or oversize members are skipped, the zip itself remains). The folder is always one valid file tree: a file and a directory never share a name — conflicting zip members are dropped, and an extraction folder that would collide with an existing flat attachment is remapped to `{stem}-2/…` (a later flat attachment named like an extraction folder takes the `-2` suffix instead).

## 3. Environment contract (normative)

Delivery happens in two stages, because Dagu trigger params are visible unmasked in its UI (verified — `13-deployment.md` §4):

- **Stage 1 — container env from the Dagu DAG:** `DEVCAKE_RUN_ID`, `TRACEPARENT`, `REDIS_URL`, and the per-run scoped Redis ACL credential (`REDIS_USER`/`REDIS_PASSWORD` — the one deliberate param-borne secret, `09-messaging.md` §1a).
- **Stage 2 — the run spec, fetched by the entrypoint** over Redis (`runspec.get` → keyed by run id, `09-messaging.md` §4): everything else below, including all secrets, scoped to exactly what this run's Dev Type needs. The entrypoint exports these as env vars (or writes credential files) **before** launching the harness — so from the harness's point of view the full table is simply its environment.

| Variable | Stage | Meaning |
|---|---|---|
| `DEVCAKE_RUN_ID` | 1 | Human-readable run id (`02-domain-model.md` §7, e.g. `LINEAR-ENG-142-3-EXECUTE-9GX2TQ`); container is named `dev-{DEVCAKE_RUN_ID}`. |
| `TRACEPARENT` | 1 | W3C trace context — links the Dev's spans into the dispatch trace (`12-observability.md`). |
| `REDIS_URL` | 1 | `redis://redis:6379/0`. |
| `REDIS_USER` / `REDIS_PASSWORD` | 1 | Per-run scoped ACL credential (`09-messaging.md` §1a); doubles as the envelope `auth` token. |
| `DEVCAKE_MISSION_ID` | 2 | PMO `pmo_id`. |
| `DEVCAKE_MISSION_KEY` | 2 | e.g. `ENG-142`. Branch name is `devcake/{INSTANCE}-{mission_key}` via `mission_branch` (not derived from this env alone). |
| `DEVCAKE_MISSION_TYPE` | 2 | `ONBOARD \| PLAN \| EXECUTE \| REVIEW`. |
| `DEVCAKE_DEV_TYPE` | 2 | Dev Type name. |
| `DEVCAKE_HARNESS` | 2 | Harness id — image-baked `ENV` is the fallback; the runspec (from dispatch) is authoritative. Not a DAG stage-1 param. |
| `DEVCAKE_MODEL` | 2 | Per-Dev-Type model pin; empty = harness default. |
| `DEVCAKE_SEQ` | 2 | Step number (transcript naming `{seq}_{type}.md`). |
| `DEVCAKE_REPO_URL` | 2 | Clone URL (credential-free; auth via helper, §5). |
| `DEVCAKE_DEFAULT_BRANCH` | 2 | The resolved work repo's `default_branch` (from that `RepoInstance` — not a singular `config.repo`). Present in the runspec for playbooks/spec; the shared entrypoint itself may not read it. |
| `DEVCAKE_CLONE_USER` | 2 | Credential-in-URL user for the https clone (from the forge descriptor, `06-forge-adapter.md` §3a — `x-access-token` / `oauth2`). |
| `DEVCAKE_GIT_NAME` / `DEVCAKE_GIT_EMAIL` | 2 | The Dev's git identity (from the forge descriptor). |
| `DEVCAKE_FORGE_CLI_ENVS` | 2 | Comma-joined env-var names the entrypoint mirrors `DEVCAKE_FORGE_TOKEN` into for the forge CLI (from the descriptor's `cli_token_envs`, e.g. `GH_TOKEN`). |
| `DEVCAKE_EXTRA_ARGS` | 2 | Per-Mission-Type extra CLI args from `assignments` (`02-domain-model.md` §9), appended verbatim to the harness invocation (`08-harness-templates.md` §1). May be empty. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 2 | OTLP endpoint = the stack's `otel-collector` on `devcake_runtime`. **Unauthenticated**: Devs hold no OO credentials at all; the collector alone authenticates upstream (`12-observability.md` §1, ISSUES #13). |
| *harness credentials* | 2 | Per Dev Type: e.g. `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`, `XAI_API_KEY`, `CODEX_API_KEY` — or credential-file **content** in the run spec, written by the entrypoint to the harness-specific path, 0600 (`08-harness-templates.md` §4). |
| *forge credentials* | 2 | `DEVCAKE_FORGE_TOKEN` (the active work repo's token for this run). |
| *Dev-Type secret env* | 2 | Named vars from `DevType.secret_env` (`02-domain-model.md` §6), values GUI-stored under `/data/secrets/harness/` — mission-tooling credentials (e.g. a log-platform key) referenced as `$VAR` from `mcp_setup_commands` (`08-harness-templates.md` §7). They land in the container's environment either way, so this is also how a var the **harness CLI itself** reads is delivered — e.g. a local backend's base URL (`08-harness-templates.md` §8a). Missing value: **referenced** by a setup command ⇒ dispatch refuses (`14-security.md` §8); unreferenced ⇒ warn-and-proceed. |

Real secrets (harness and forge credentials) never appear in Dagu params, DAG YAML, its UI, or any bind mount; the sole param-borne credential is the per-run scoped, finalization-revoked Redis ACL pair (`14-security.md` §4, `09-messaging.md` §1a).

**Forge dialect (`forge_dialect()` in the shared entrypoint):** the descriptor-driven vars (`DEVCAKE_CLONE_USER`, `DEVCAKE_GIT_NAME`, `DEVCAKE_GIT_EMAIL`, `DEVCAKE_FORGE_CLI_ENVS`) are **required** — there are no fallbacks. App and images deploy in lockstep (`13-deployment.md` §8); a runspec missing a descriptor var means a mismatched build and crashes the clone bootstrap loudly (the v0 fallback shims were removed at crystallization, `adr/0008` addendum).

## 4. Exit codes (normative)

| Code | Meaning | Error class (`15-errors-and-retries.md`) |
|---|---|---|
| 0 | Success — `result.json` present and valid | — |
| 10 | Harness exited nonzero / crashed | `DEV_CRASH` |
| 11 | `result.json` missing or schema-invalid | `DEV_BAD_OUTPUT` |
| 12 | Credential/auth failure (harness) | `DEV_AUTH` |
| 13 | Clone or forge operation failed | `DEV_FORGE` / `DEV_FORGE_AUTH` (classified from git stderr — `15-errors-and-retries.md` §4) |
| 14 | MCP setup command failed or timed out (300 s per command) | `DEV_MCP_SETUP` (counted) |
| 15 | Harness reported a failure in-band, or produced no output at all, despite the process exit status | `DEV_HARNESS_FAULT` — counted unless the failure is *correlated* across ≥2 missions (`15-errors-and-retries.md` §4a) |
| 16 | Harness stopped at its configured turn cap (`--max-turns`) — **`claude-code` and `grok-build`; unreachable for `codex`**, see below | `DEV_TURN_BUDGET` (always counted; deterministic, so never correlated) |
| 20 | Entrypoint internal error | `DEV_CRASH` |

**The exit status alone is not the failure signal (ADR-0018).** Every harness CLI can terminate with status 0 while reporting failure in-band — a backend answering HTTP 200 with an empty completion is the measured case — and their stderr carries no failure information at all, so the entrypoint inspects the harness's own terminal event before deciding. Exits 10, 11, 15 and 16 all carry `error_class`, `error_detail` and a bounded workspace-forensics block.

**Which harnesses can reach exit 16** (`adr/0018-harness-fault-classification-and-backend-brake.md`, and `app/tests/fixtures/harness_streams/README.md` for the streams). Two of the three. `claude-code`: cap exhaustion surfaces as `terminal_reason: "max_turns"` / `subtype: "error_max_turns"`. `grok-build` 0.2.112: it emits a dedicated `{"type":"max_turns_reached"}` event **and** an `end` with `stopReason: "Cancelled"`, exiting 1 — and since ADR-0018's fix round the grok predicate fires its turn-budget arm on that **event type**, checked before every other arm exactly as claude's is, so a grok cap stop now reports exit 16 too (`grok_turn_budget`; asserted in `app/tests/test_harness_captures.py`). It reached `DEV_CRASH` (exit 10) before that arm existed. `codex` at 0.144.4 has **no `--max-turns` equivalent and no config key for one**, so exit 16 is unreachable for it and no value of the per-Mission-Type extra CLI args (`02-domain-model.md` §9) can bound it.

**A grok Dev can also stop early WITHOUT reaching 16 — and it reports exit 11, not 16.** grok 0.2.112 halts a run in which the model repeats the identical tool call, on its own, after ~16 model calls: `stopReason: "EndTurn"`, exit **0**, no `max_turns_reached` (`grok_loop_nocap`, `grok_loop_cap30`). Nothing was completed, so `result.json` is missing and the run lands on **exit 11 `DEV_BAD_OUTPUT`** with no indication that it was truncated. This is a non-progress halt and **not** a turn cap — `--max-turns` is unaffected by it and still fires wherever it is set, above 16 included (`grok_loop_varying_cap20` stops at 20). Mechanism and fixtures: `adr/0018-harness-fault-classification-and-backend-brake.md`; operator guidance: `15-errors-and-retries.md` §2b.

**A runaway `codex` Dev is bounded only by the run timeout.** Against a backend that answers every turn with a tool call, a codex run does not stop by itself — the capture campaign measured one at ~5,535 requests in ~7 minutes, still going when it was killed at the container level (a campaign note, not a committed fixture: that run produced no capture files). There is no turn-cap remedy to reach for. The only control left is `dev_timeout_minutes` (`02-domain-model.md` §9, default 120; enforced by the watchdog, `04-orchestrator.md` §5) — a **global** setting, so lowering it to fence one Dev Type shortens every run — and it arrives as a signal kill, so the run is reported `DEV_TIMEOUT`, never `DEV_TURN_BUDGET`, and burns its full timeout of wall-clock and backend capacity on every attempt.

App-side timeout is **not** an entrypoint exit code: the watchdog kills the run via Dagu stop (SIGTERM → SIGKILL), and the Run is marked `timed_out` (`DEV_TIMEOUT`). The container may exit on SIGTERM; it does **not** emit exit 124 from the entrypoint.

## 5. Lifecycle

```
entrypoint start
  │ 0. fetch run spec via `runspec.get` (req/reply; retries with backoff;
  │      `runspec.error` → exit 20; timeout → exit 20);
  │      export stage-2 env, write credential material (0600)
  │ 1. emit `run.started` on Redis  ──────────────►  app marks Run "running"
  │      (runspec.get first, then run.started — never the reverse)
  │ 2. clone the mission's activity-* repo into /workspace/activity (full history);
  │      fallback: `activity.get` (req/reply) → materialize MISSION.md + ACTIVITY.md + attachments
  │ 3. git clone → /workspace/repo (credential helper from run-spec token; token never in URL on disk)
  │ 4. install harness credentials (env passthrough or credential-file content → harness path)
  │ 4b. install skill-store skills from the runspec `skills` field → the
  │      harness's skills dir from runspec `skills_dir` (home-relative;
  │      default ~/.claude/skills — never into the repo clone, the Dev would
  │      commit them); path-traversal-safe, per-file failures non-fatal;
  │      consult is optional unless the prompt soft-forces Required skills
  │ 5. run mcp_setup_commands — stdin closed, own process group, 300 s cap per
  │      command; first failure/timeout → run.artifacts {exit_code: 14,
  │      DEV_MCP_SETUP, command + stderr tail} then exit 14
  │ 6. launch harness: identifying prompt + mission-type playbook prompt
  │      (+ optional required-skills soft-force block — 03 §7)
  │      • heartbeat sidecar emits `run.heartbeat` every 30 s throughout
  │      • the live log announces harness start and, while user-visible output is
  │        absent, emits one liveness notice per 60 s (hidden reasoning stays hidden)
  │ 7. harness finishes; entrypoint validates /workspace/out/result.json
  │ 8. assemble transcript (`assemble_transcript` in the shared entrypoint —
  │      header + session dump / agent report + outcome JSON) + extract TokenReport
  │      (`session_json` or `unavailable` — 08-harness-templates.md §5)
  │ 9. publish `run.artifacts` {result.json, transcript_md, token_report} on Redis
  └ 10. exit with code per §4  ──────────────────►  app finalizes (04-orchestrator.md §4)
```

Git pushes and PR interactions (EXECUTE, REVIEW approval checkout) happen inside step 6, driven by the playbook prompt, but **commits only at the very end of the work** (INV-6) — the playbook prompts state this explicitly and the transcript is evidence of compliance.

## 6. Mid-run PMO access (not shipped as a CLI)

**`devcake-relay` is not a shipped CLI.** Devs speak the Redis protocol of `09-messaging.md` **directly from the shared entrypoint** (`runspec.get`, `activity.get`, heartbeats, artifacts). There is no separate relay binary in the image.

There is **no write access** to the PMO mid-run (INV-4). Writes travel as end-of-run artifacts that the app applies. Mid-run re-fetch of the activity payload uses the same `activity.get` request/reply channel the entrypoint already speaks.

Other tooling (log-platform access and the like) arrives as **MCP plugins** — standalone servers living outside this repo, installed per Dev Type at run time via `mcp_setup_commands` (`08-harness-templates.md` §7, `tutorials/03-mcp-plugins.md`). The official log connector is <https://github.com/fidecastro/devcake-logs-mcp>.

## 7. Network and resources

DevCake is **not** a multi-tenant sandbox product (`14-security.md` §6). Isolation is intentional but limited:

- **Network:** full **outbound** internet (forge, packages, model APIs) plus membership in `devcake_runtime` for Redis, **otel-collector**, and optional internal Gitea. **OpenObserve is not on runtime.** The app/admin/Dagu control plane is not attached. Attachment mechanism in `13-deployment.md` §5.
- **No `docker.sock`:** Dev containers never receive the Docker socket (`14-security.md` §5).
- **User:** the entire entrypoint runs as a non-root user (uid 1000) — verified hard requirement at M3: Claude Code refuses `--dangerously-skip-permissions` under root.
- **MCP / extra CLI args:** admin-configured free-text commands run with `shell=True` / harness flags before/with the agent — **admin-equivalent ACE** inside the disposable container (`11-admin-panel.md`).
- **Resources:** Dagu 2.10.5 step `container:` does **not** support Docker HostConfig CPU/memory/PID fields (schema `additionalProperties: false`). The DAG sets best-effort process-level `resources.limits` (`cpu: "2"`, `memory: "4g"`) where the host enforces cgroups on the DAG run process — this is **not** a guaranteed limit on the sibling Dev container (**engineering debt**, `14` §11). Primary throttle is app concurrency (`concurrency.global_max` + per-Dev-Type caps).

## 8. Building a new Dev image (checklist)

1. Start from the harness template's base image (`08-harness-templates.md` §2).
2. Include: `git`, the harness CLI, the shared entrypoint (Redis protocol speaker), an OTel-emitting wrapper.
3. Honor every env var in §3; produce every artifact in §1; exit per §4.
4. Verify with the M1 hello-world DAG and the contract test battery (`16-roadmap.md`).
