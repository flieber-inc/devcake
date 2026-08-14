# 07 — Dev Runtime: The Container Contract

> **Audience:** implementers and harness-image authors. This document plus `08-harness-templates.md` and `09-messaging.md` are the complete image contract — build against the three together (the 2026-08 truth sweep retired the older "this document alone" claim; the protocol payloads live in 09).
> **Depends on:** `02-domain-model.md` (Run, DevType), `03-mission-lifecycle.md` (`result.json`), `08-harness-templates.md` (per-harness specifics), `09-messaging.md` (Redis protocol).

A **Dev container** is an ephemeral Docker container that performs exactly one Mission Step and exits. It is spawned by Dagu as a sibling of the compose stack (`13-deployment.md`), named `dev-{run_id}`, and attached to the **`devcake_runtime`** network so it reaches `redis`, `otel-collector`, and (when used) internal Gitea by service name. **OpenObserve is not on the runtime network** — Devs export OTLP to the collector only (`12-observability.md`, `14` §10).

Devs are **pure functions from (workspace, prompt) to artifacts**: they never write to the PMO System or mutate any shared state other than pushing a git branch at the very end (INV-4, INV-6). All PMO effects are applied by the app when it consumes the run's artifacts.

## 1. Filesystem layout (normative)

```
/workspace/
  repo/                  # fresh `git clone` of the configured repository, directory named
                         #   exactly after the repository (standard clone output).
                         #   The ONLY place the harness may do its work (INV-6).
    {slug}/              # ZERO OR MORE extra clones (ADR-0017/0024): the routing
                         #   set's other repos (ONBOARD), the instance's reference
                         #   repos (every stage), and done blockers' work repos —
                         #   cloned by provision from `extra_repos` (mirror file://
                         #   or RO-token https). Read-only BY TOKEN + prompt
                         #   contract, not by filesystem mode; extra-clone
                         #   failures are non-fatal (noted, run proceeds).
  memory/
    {card}/              # ZERO OR MORE consumer memory notebooks (PLAN_MEMORY):
                         #   cloned by provision from `memory_repos` to
                         #   /workspace/memory/<card>/ (card name, not URL slug).
                         #   Sibling of repo/ and activity/ — never
                         #   repo/memory/. Read-only by token + prompt.
                         #   Curator runs do NOT remount their own notebook
                         #   here (it is the primary work clone).
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
                         #   Excluded from transcripts and never uploaded. Also holds two
                         #   ADR-0025 control files: `created-by-app` (the app writes the
                         #   run id at pre-create — provision verifies it) and `provisioned`
                         #   (the provision step writes the run id last — the harness step
                         #   verifies it before doing anything).
```

**One run, two containers, one workspace (ADR-0025).** `/workspace` is a
host-bind directory (`$DEVCAKE_WS_HOST/<run_id>` on the daemon host) the app
pre-creates at dispatch and deletes at run end. It is mounted rw into BOTH of
a run's containers: the **provision** container (`prov-<run_id>`,
`DEVCAKE_PHASE=provision`) materializes everything above with the source
mirrors mounted RO at `/mirrors`, then exits; the **harness** container
(`dev-<run_id>`, `DEVCAKE_PHASE=harness`) mounts ONLY this workspace —
`/mirrors` does not exist in it — verifies the `provisioned` marker, and runs
the agent. The agent therefore sees exactly `repo/`, `activity/`, `out/` and
`.devcake/` — never the mirror, never another repo's bytes. There is no
single-container fallback: the DAG always sets `DEVCAKE_PHASE`, and the
entrypoint exits 20 loudly on any other value (a mismatched build /
hand-run container — rollback compat is not a pre-v1 concern).

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

The mirror is strictly chronological and complete — *all* the current activity of the Mission; nothing is omitted or previewed (`get_activity(full=True)` walks the entire history — `05-pmo-adapter.md` §3). **Project-kind missions** (project-fidelity fix): the mirrored feed is the project-native update feed — updates (authored `"Name (project update)"`) and their comments, with the same provenance markers — and the folder additionally carries each project Document as `docs/<title>.md` (indexed under `## Project documents` in `MISSION.md`; a document exceeding the attachment cap is skipped with an honest `[document too large to mirror: …]` line). External links and native project attachments ride the mission-attachment lines exactly like issues. Attachments (notably prior DevCake full-session transcripts `N_TYPE.md`, plans, and review reports) are downloaded into `activity/` under their original filenames (name-collision suffix `-2`, `-3`, … — implemented app-side in `activity_payload`, deduping downloaded attachments against each other and against `ACTIVITY.md`/`MISSION.md`). Every `.zip` attachment is kept as the zip **and** expanded under a same-named stem folder (`{name without .zip}/…`) so Devs can read deliverables without unpacking tools; path members are zip-slip hardened and extraction is size-capped (best-effort — corrupt or oversize members are skipped, the zip itself remains). The folder is always one valid file tree: a file and a directory never share a name — conflicting zip members are dropped, and an extraction folder that would collide with an existing flat attachment is remapped to `{stem}-2/…` (a later flat attachment named like an extraction folder takes the `-2` suffix instead).

## 3. Environment contract (normative)

Delivery happens in two stages, because Dagu trigger params are visible unmasked in its UI (verified — `13-deployment.md` §4):

- **Stage 1 — container env from the Dagu DAG:** `DEVCAKE_RUN_ID`, `TRACEPARENT`, `REDIS_URL`, and the per-run scoped Redis ACL credential (`REDIS_USER`/`REDIS_PASSWORD` — the one deliberate param-borne secret, `09-messaging.md` §1a).
- **Stage 2 — the run spec, fetched by the entrypoint** over Redis (`runspec.get` → keyed by run id, `09-messaging.md` §4): everything else below, including all secrets, scoped to exactly what this run's Dev Type needs. The entrypoint exports these as env vars (or writes credential files) **before** launching the harness — so from the harness's point of view the full table is simply its environment. Under ADR-0025 the `runspec.get` payload carries the caller's `phase`: the **provision** step receives a REDUCED spec (no credential files, no harness/model or Dev-Type secret env; the forge token only for direct-clone internal repos) — it runs no agent and needs only what cloning needs; the **harness** step receives the full table below (`09-dev-protocol.md` §4).

| Variable | Stage | Meaning |
|---|---|---|
| `DEVCAKE_RUN_ID` | 1 | Human-readable run id (`02-domain-model.md` §7, e.g. `LINEAR-ENG-142-3-EXECUTE-9GX2TQ`); the run's two containers are named `prov-{DEVCAKE_RUN_ID}` and `dev-{DEVCAKE_RUN_ID}` (ADR-0025). |
| `DEVCAKE_PHASE` | 1 | `provision \| harness` (ADR-0025) — which step of the two-step `dev-run` DAG this container is; the DAG always sets it, and the entrypoint exits 20 on any other value (no single-container fallback). Selects the reduced vs full runspec and gates credential-file writes to the harness phase. |
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
| `DEVCAKE_RECOVER_MISPLACED_RESULT` | 2 | Misplaced-result recovery flag (ADR-0018, `cfg.recover_misplaced_result`). Wire format `"1"`/`""` — the entrypoint reads it with bare truthiness, so `str(bool)` would be unswitchable. |
| `DEVCAKE_CONTINUATION_POLICY` | 2 | Continuation policy string (ADR-0022, `cfg.continuation_policy`): `auto \| resume-only \| fresh-only \| off`. Parsed defensively in-container (unknown → `auto`). |
| `DEVCAKE_MAX_CONTINUATIONS` | 2 | Continuation budget as `str(int)` (ADR-0022, `cfg.max_continuations`); `"0"`/absent/garbage → loop off. |
| `DEVCAKE_MIRROR_PATH` | 2 | The work repo's source mirror inside the RO `/mirrors` mount (ADR-0024), e.g. `/mirrors/<name>.git`; `""` = direct clone (internal repos only). The entrypoint clones `file://<path>` then rewrites origin to `DEVCAKE_REPO_URL`. |
| `DEVCAKE_LFS` | 2 | `"1"`/`""` (ADR-0022 flag convention): full LFS smudge (real content from the mirror's LFS store) vs `--skip-smudge` (pointer files — the pre-ADR-0024 behavior). From `cfg.repo_mirror.lfs`. |
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
| 11 | `result.json` missing or schema-invalid — reached only after the in-container continuation budget is spent, when the loop is enabled (ADR-0022, §5a) | `DEV_BAD_OUTPUT` |
| 12 | Credential/auth failure (harness) | `DEV_AUTH` |
| 13 | Clone or forge operation failed | `DEV_FORGE` / `DEV_FORGE_AUTH` (classified from git stderr — `15-errors-and-retries.md` §4) |
| 14 | MCP setup command failed or timed out (300 s per command) | `DEV_MCP_SETUP` (counted) |
| 15 | Harness reported a failure in-band, or produced no output at all, despite the process exit status | `DEV_HARNESS_FAULT` — counted unless the failure is *correlated* across ≥2 missions (`15-errors-and-retries.md` §4a) |
| 16 | Harness stopped at its configured turn cap (`--max-turns`) — **`claude-code` and `grok-build`; unreachable for `codex`**, see below | `DEV_TURN_BUDGET` (always counted; deterministic, so never correlated) |
| 20 | Entrypoint internal error | `DEV_CRASH` |

**The exit status alone is not the failure signal (ADR-0018).** Harness CLIs can
exit 0 with an empty or failed in-band terminal event, and stderr often carries
no failure information — the entrypoint inspects the stream before deciding.
Exits 10, 11, 15 and 16 carry `error_class`, `error_detail` and bounded
workspace forensics. On exit 11 the forensics additionally name the stream's
terminal event under `terminal` (`continuation.terminal_evidence`): a clean
early stop (grok `end`/`stopReason EndTurn` with `num_turns`, claude `result`,
codex `turn.completed`) versus `null` for a stream that just stopped — the
narrate-and-stop diagnosis at fleet scale. Scenario captures:
`app/tests/fixtures/harness_streams/`.

**Which harnesses can reach exit 16.** `claude-code` (`max_turns` /
`error_max_turns`) and `grok-build` (`max_turns_reached` event) — see
`test_harness_captures.py` and ADR-0018. `codex` 0.147.0 has no turn cap, so
exit 16 is unreachable; extra CLI args cannot invent one (`02` §9).

**Grok non-progress halt → exit 11, not 16.** A repeated identical tool call can
end with `EndTurn` and exit 0 after ~16 model calls (no `max_turns_reached`).
Missing `result.json` → exit 11. Not a turn cap — see `15` §2b and `grok_loop_*`
captures. This is exactly the landing the ADR-0022 continuation loop (§5a)
nudges before the run is allowed to fail.

**A runaway `codex` Dev is bounded only by the run timeout**
(`dev_timeout_minutes`, default 120 — global; watchdog → `DEV_TIMEOUT`, never
`DEV_TURN_BUDGET`).

App-side timeout is **not** an entrypoint exit code: the watchdog kills the run via Dagu stop (SIGTERM → SIGKILL), and the Run is marked `timed_out` (`DEV_TIMEOUT`). The container may exit on SIGTERM; it does **not** emit exit 124 from the entrypoint.

## 5. Lifecycle

The DAG is two dependent container steps (ADR-0025). **Provision**
(`prov-<run_id>`, `DEVCAKE_PHASE=provision`) runs steps 0-3 with `/mirrors`
mounted RO, verifies the app-written `created-by-app` sentinel first, writes
the `provisioned` marker last, and exits. **Harness** (`dev-<run_id>`,
`DEVCAKE_PHASE=harness`, no `/mirrors`) verifies the marker, then runs steps
4-10. `run.started` is sent by provision only; the harness step starts its
heartbeat before fetching the runspec. There is no single-container flow —
a missing/unknown `DEVCAKE_PHASE` exits 20 loudly.

```
PROVISION container (prov-<run_id>, DEVCAKE_PHASE=provision, /mirrors RO)
  │ 0. fetch run spec via `runspec.get` {phase: provision} → REDUCED spec
  │      (no harness/model secrets, no credential files); export stage-2 env
  │ 0a. verify `.devcake/created-by-app` names THIS run (else exit 20 +
  │      forensics — wrong bind dir: daemon autocreate or WS_HOST drift)
  │ 1. emit `run.started` on Redis  ──────────────►  app marks Run "running"
  │      (the SOLE run.started sender; heartbeat sidecar starts here too)
  │ 2. clone the mission's activity-* repo into /workspace/activity (full history);
  │      fallback: `activity.get` (req/reply) → materialize MISSION.md + ACTIVITY.md + attachments
  │ 3. git clone → /workspace/repo — from the RO source mirror
  │      (file://$DEVCAKE_MIRROR_PATH, ADR-0024 §5b below; credential-stripped,
  │      `-c lfs.url` pinned to the own mirror — ADR-0025 §5) for configured
  │      repos, then `git remote set-url origin <real URL>`; direct from the
  │      forge only for internal repos (credential helper from run-spec
  │      token; token never in URL on disk)
  │ 3z. write `.devcake/provisioned` (this run id) → exit 0
  ▼
HARNESS container (dev-<run_id>, DEVCAKE_PHASE=harness, workspace ONLY)
  │ 3a. heartbeat sidecar starts, THEN fetch run spec `{phase: harness}` →
  │      full spec
  │ 4. install harness credentials (env passthrough or credential-file
  │      content → harness path). NOTE the order (2026-08 truth sweep):
  │      credentials land BEFORE the workspace identity check below —
  │      they go to $HOME, not the workspace, so a wrong-bind exit never
  │      leaves secrets in the disputed tree
  │ 4a. verify sentinel + `provisioned` marker (else exit 20 WITH artifacts —
  │      forensic owner/mode/listing); re-adopt the askpass + git
  │      identity/LFS posture (fresh HOME)
  │ 4b. install skill-store skills from the runspec `skills` field → the
  │      harness's skills dir from runspec `skills_dir` (home-relative;
  │      default ~/.claude/skills — never into the repo clone, the Dev would
  │      commit them); path-traversal-safe, per-file failures non-fatal;
  │      consult is optional unless the prompt soft-forces Required skills
  │      (external `<card>/<skill>` skills arrive in the SAME field with
  │      basename-flattened paths — the container cannot tell the source;
  │      their card joins the fail-closed mirror gate app-side, ADR-0016)
  │ 5. run mcp_setup_commands — stdin closed, own process group, 300 s cap per
  │      command; first failure/timeout → run.artifacts {exit_code: 14,
  │      DEV_MCP_SETUP, command + stderr tail} then exit 14
  │ 6. launch harness: identifying prompt + mission-type playbook prompt
  │      (+ optional required-skills soft-force block — 03 §7)
  │      • heartbeat sidecar emits `run.heartbeat` every 30 s throughout
  │      • the live log announces harness start and, while user-visible output is
  │        absent, emits one liveness notice per 60 s (hidden reasoning stays hidden)
  │ 7. harness finishes; entrypoint validates /workspace/out/result.json
  │ 7a. CONTINUATION LOOP (ADR-0022, §5a below): if the harness exited 0 with
  │      no fault but no usable result.json (and no recoverable stray), and
  │      the continuation budget allows, relaunch the harness in THIS
  │      container with a contract-reminder nudge — resuming the session
  │      (verified harnesses) or starting a fresh one in the same workspace —
  │      then re-run step 7. Every relaunch is announced on the live log.
  │ 8. assemble transcript (`assemble_transcript` in the shared entrypoint —
  │      header + session dump / agent report + outcome JSON) + extract TokenReport
  │      (`session_json` or `unavailable` — 08-harness-templates.md §5;
  │      merged across invocations when the loop fired)
  │ 9. publish `run.artifacts` {result.json, transcript_md, token_report,
  │      continuations_used} on Redis
  └ 10. exit with code per §4  ──────────────────►  app finalizes (04-orchestrator.md §4)
```

Git pushes and PR interactions (EXECUTE, REVIEW approval checkout) happen inside step 6, driven by the playbook prompt, but **commits only at the very end of the work** (INV-6) — the playbook prompts state this explicitly and the transcript is evidence of compliance.

### 5a. The continuation loop (ADR-0022)

Trigger: exactly the "row 9" landing — exit 0, fault predicate None, no valid
canonical `result.json`, no recoverable stray. Every other terminal path
classifies as before, including on a continuation invocation (the fault
machinery outranks the loop). Policy `cfg.continuation_policy` — `auto`
(resume the session where `RESUME_SPECS` has a capture-verified entry;
escalate permanently to a fresh session after a zero-progress continuation) ·
`resume-only` (stops, as today, when resume is unavailable) · `fresh-only` ·
`off`. `cfg.max_continuations` is the ONLY terminator (stalls escalate,
never stop). Plan mode never continues. The nudge restates the canonical
path + legal outcomes and points back to the playbook's Required output
section for the exact shape; fresh mode embeds the original prompt verbatim.
The misplaced-result freshness gate stays pinned to the FIRST launch, so a
stray written by an earlier invocation stays adoptable. Each relaunch resets
the CLI's own `--max-turns` — effective turn budget is
(continuations + 1) × max-turns. The watchdog (`dev_timeout_minutes`) is
unchanged and bounds the whole loop.

## 6. Mid-run PMO access (not shipped as a CLI)

**`devcake-relay` is not a shipped CLI.** Devs speak the Redis protocol of `09-messaging.md` **directly from the shared entrypoint** (`runspec.get`, `activity.get`, heartbeats, artifacts). There is no separate relay binary in the image.

**Entrypoint layout.** The image `ENTRYPOINT` is still `/dev_entrypoint.py` (composition façade). Dev-side code lives under `images/common/devcake_dev/` (copied to `/devcake_dev`) — a Zone-B package separate from `app/devcake`:

| Package path | Role |
|---|---|
| `domain.fault` | ADR-0018 harness fault classification (pure) |
| `harness.continuation` | ADR-0022 continuation policy, nudges, session chains, token-report merge, terminal evidence (pure) |
| `harness.render` / `tokens` / `argv` | Live relay, stream dumps, CLI argv (+ resume dialects, `RESUME_SPECS`) |
| `workspace.*` | Clone, skills/MCP, forensics, activity, transcript |
| `adapters.bus` | Redis Streams send / request-reply / artifacts |

There is **no write access** to the PMO mid-run (INV-4). Writes travel as end-of-run artifacts that the app applies. Mid-run re-fetch of the activity payload uses the same `activity.get` request/reply channel the entrypoint already speaks.

Other tooling (log-platform access and the like) arrives as **MCP plugins** — standalone servers living outside this repo, installed per Dev Type at run time via `mcp_setup_commands` (`08-harness-templates.md` §7, `tutorials/03-mcp-plugins.md`). The official log connector is <https://github.com/fidecastro/devcake-logs-mcp>.

## 7. Network and resources

DevCake is **not** a multi-tenant sandbox product (`14-security.md` §6). Isolation is intentional but limited:

- **Network:** full **outbound** internet (forge, packages, model APIs) plus membership in `devcake_runtime` for Redis, **otel-collector**, and optional internal Gitea. **OpenObserve is not on runtime.** The app/admin/Dagu control plane is not attached. Attachment mechanism in `13-deployment.md` §5.
- **No `docker.sock`:** Dev containers never receive the Docker socket (`14-security.md` §5).
- **User:** the entire entrypoint runs as a non-root user (uid 1000) — verified hard requirement at M3: Claude Code refuses `--dangerously-skip-permissions` under root. PID 1 is `tini` (ADR-0023 fix round): the entrypoint reaps no orphans, and browser process trees would otherwise accumulate zombies over multi-hour runs; Dagu cannot pass `--init` (see **Resources** below).
- **MCP / extra CLI args:** admin-configured free-text commands run with `shell=True` / harness flags before/with the agent — **admin-equivalent ACE** inside the disposable container (`11-admin-panel.md`).
- **Resources (DELIVERED 2026-08-13 — closes the old ISSUES #20 deferral):** every Dev container (both steps) runs under **kernel-enforced cgroup limits** — `AppConfig.container_limits` (admin Limits page: memory MB / CPUs / PIDs, 0 = unlimited; defaults 4096 MB / 2.0 / off) → dev-run DAG params → Docker HostConfig via Dagu's docker-executor form (`host:` block; the values ride the NESTED `resources:` key at the pinned 2.13.0 — its decode lacks mapstructure Squash, upstream fix `dagucloud/dagu#2557`; measured live: `docker inspect` shows `Memory`/`NanoCpus` on real run containers). A Dev over the memory cap is OOM-killed (a normal counted failure); CPU throttles, never kills. The old step `container:` shorthand had no HostConfig path at all (measured 2.10.5/2.11.3) — that era's best-effort DAG `resources.limits` block is deleted. Fleet-level throttle remains app concurrency (`concurrency.global_max` + per-Dev-Type caps).

### 7a. Toolchain floor (ADR-0023, normative — identical across harness images)

The base image bakes **capability floors, not tool inventories** — classified
by who is able to provide the capability, since the mission space is
open-ended and runtime `apt` rightly does not exist (no root):

- **Class A (root-only — baked or impossible):** `build-essential` (native
  pip/npm builds), and the browser stack: playwright's pinned headless
  Chromium shell + every system library it loads, at
  `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` — **dev-owned**, because the
  env var points every install there and a read-only dir would brick any
  browser the base did not bake (measured; ADR-0023 fix round). Writable is
  safe: the container is disposable. The `playwright` CLI is on PATH at the
  same pin (`playwright --version` tells a Dev which version finds the baked
  browser; a mismatched install downloads its own build alongside —
  degraded, never broken).
- **Class B (self-provisioning enablers):** Node/npm/npx (shared base — every
  harness, so a JS repo's dev server always starts), pip/venv, `uv`, and a
  PATH that honors user installs: `~/.local/bin` and `~/.npm-global/bin`
  (dev-owned `.npmrc` prefix — runtime `npm i -g` needs no root).
- **Class C (high-frequency conveniences — turn economics, ADR-0022):** jq,
  ripgrep, unzip/zip, less, procps, file, sqlite3, and the
  document/spreadsheet floor: pandoc, poppler-utils, pandas + openpyxl in
  the system Python.

Deliberately absent: sudo, databases/services, cloud/vendor CLIs, media
tooling (`adr/0023` records the rationale for each). The container engine
JOINED the floor 2026-08-13 (`adr/0023` addendum): **rootless podman**
(`docker` = compat symlink) runs nested containers inside the Dev's own
namespaces — no `docker.sock`, no privilege. The sandbox boundary is
unchanged in KIND, at a stated cost: the dev-run DAG's custom seccomp
profile (default + one 15-syscall allow rule, never unconfined) plus
/dev/fuse + /dev/net/tun widen the kernel surface for EVERY container the
DAG launches, hello included — the accepted-risk row lives in `14` §6.
Nested storage lives under $HOME → per-run ephemeral; nested writes onto
the /workspace BIND persist past the run as foreign-uid files, so the DAG's
exit handler re-chowns the workspace to uid 1000 at run end (success,
failure, and stop) and the app then reclaims it normally. The container's
cgroup limits bound the nested engine too. The long
tail is per-Dev-Type via `mcp_setup_commands` or runtime user-space
installs. The base build ends with a smoke RUN **as uid 1000** proving the
floor (headless shell launches, imports resolve, binaries exist) — CI's
image bake asserts it on every PR.

### 7b. Source mirror (ADR-0024, normative)

Every configured repo clones from the app-maintained bare mirror on the
read-only `devcake_mirrors` volume (`/mirrors`) — MANDATORY, no toggle. A
successful app-side sync is a fail-closed DISPATCH precondition: a mission
whose mirrors cannot be freshened (per `cfg.repo_mirror.sync_max_age_seconds`,
default 0 = every dispatch) does not dispatch that cycle — no container, no
attempt burned, reason on the missions row + `/health` — and retries next
poll. **`/mirrors` is mounted ONLY in the provision container (ADR-0025)** —
the harness container the agent runs in has no `/mirrors` at all, so there is
no bare-pack duplicate of the work repo and no visibility of any other repo's
source. Origin is rewritten to the real forge, so push/PR/mid-run fetch
behave exactly as a direct clone. A mirror clone failure is exit 13
`DEV_FORGE` (never `DEV_FORGE_AUTH`); mirror clones run credential-stripped
with `-c lfs.url` pinned to the own mirror (ADR-0025 §5). Exceptions (by
design, not configuration): internal-forge synthesized repos and activity
repos stay direct — their isolation is per-mission token scope (`14` §2
Zone B) and must not ride a deployment-shared volume. LFS: pointer files by
default; `cfg.repo_mirror.lfs` upgrades to real content served from the
mirror's own LFS store (probe-verified standalone `file://` transfer) — the
posture is installed in BOTH containers, since checkout smudges in each.

## 8. Building a new Dev image (checklist)

1. Start from the harness template's base image (`08-harness-templates.md` §2).
2. Include: `git`, the harness CLI, the shared entrypoint (Redis protocol speaker), an OTel-emitting wrapper — and the §7a toolchain floor (build from the shared `base` stage and it is inherited).
3. Honor every env var in §3; produce every artifact in §1; exit per §4.
4. Verify with the M1 hello-world DAG and the contract test battery (`16-roadmap.md`).
