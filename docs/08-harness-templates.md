# 08 — Harness Templates

> **Audience:** implementers. This document centralizes the runtime contract for
> harness CLI churn. Model assignments normally change on a Dev Type; CLI,
> image, credential, or artifact-shape changes also require the implementation
> locations named in §9.
> **Depends on:** `07-dev-runtime.md` (container contract), `02-domain-model.md` (DevType, TokenReport).

A **harness template** describes how one CLI runs inside a Dev container:
image, credentials, invocation, artifact parsing, OAuth, and skills delivery.
It does **not** own a fixed model. Exactly three templates exist as entries in
the harness registry (`app/devcake/harness.py`, `HARNESSES` — §2). Adding a
fourth crosses the registry, config schema, image/Bake, entrypoint, tests, and
this document (§8); changing a Dev Type's model does not add a template.

| Template id | Harness CLI | Empty `DevType.model` resolves to | Seeded Dev Types |
|---|---|---|---|
| `claude-code` | Claude Code (`claude`) | CLI default | `judgment` pins `claude-fable-5`; `steward` pins `claude-haiku-4-5` |
| `grok-build` | Grok Build (`grok`) | Registry default `grok-4.5` | `implementer` leaves the model empty and receives that registry default |
| `codex` | Codex CLI (`codex`) | CLI default | *(none seeded)* |

Each template defines: base image, invocation pattern, plan-mode mapping, credential modes, MCP registration syntax, transcript source, and token-extraction strategy.

> **Version-currency doctrine (founder, 2026-08-04).** DevCake follows the
> CUTTING-EDGE versions of the harness CLIs it uses: the thesis is that these
> tools are continuously improving, and riding those improvements in/close to
> real time currently outweighs the cost of re-verifying each bump. The house
> discipline is unchanged by the doctrine — pins stay exact (never `:latest`
> npm; `images/Dockerfile`), every bump re-runs the capture rig
> (`scripts/harness_capture/`) so the recorded shapes below keep describing
> what production runs, and each claim carries the version it was last
> verified at. The 0.146.0 bump is the doctrine's first exhibit: the rig
> caught codex's new `cache_write_input_tokens` counter, which TokenReport v1
> (`adr/0029`) absorbed as one extractor line. Infra services (Gitea, OO,
> Dagu, redis) are NOT covered — those bump on a risk-managed cadence.

> Verification is capability- and version-specific, not blanket. Invocation,
> headless output, plan flags, MCP syntax and token extraction below record live
> evidence at the version each statement names: **Grok Build 0.2.112**, the pinned
> **Codex CLI 0.146.0** and **Claude Code 2.1.221**, except the grok skills read-set
> (**0.2.103**, §7a) and the grok plan and MCP claims (**0.2.93**, §3/§7), which are
> unverified at 0.2.112. Where a 0.2.112 observation supersedes the older 0.2.93
> record it says so. The Grok image installs latest rather than a pinned artifact,
> so every rebuild can invalidate the recorded shapes. Grok PLAN is flag-verified
> but not exercised end-to-end (§3). §8's local-backend recipes are operator
> guidance, not a guarantee for every model. Treat each statement's own version
> and caveat as its verification boundary.
>
> Scenario captures of CLI stream shapes: `app/tests/fixtures/harness_streams/`
> (README: how they are taken).

## 1. Invocation patterns

The entrypoint composes a single prompt: the Dev Type's **identifying prompt** + the Mission Type **playbook prompt** (`03-mission-lifecycle.md` §7), then invokes the harness command below **plus the Dev Type's model pin** (`$DEVCAKE_MODEL` → `claude --model` / `codex -m` / `grok --model`; empty = harness default — added 2026-07-12 after Claude Code's unpinned default silently resolved to Sonnet instead of Fable) **plus `$DEVCAKE_EXTRA_ARGS`** — the per-Mission-Type extra CLI args from `assignments` (`02-domain-model.md` §9), delivered in the run spec. Extra args come last, so they can override the pin per Mission Type. Rule (confirmed decision): **Mission-Type-specific flags are never hardcoded** — they are admin-set config, because assignments between Mission Types and Dev Types (and therefore harnesses) can change at any time.

### `claude-code`
```bash
claude -p "$PROMPT" \
  --output-format stream-json --verbose \
  --dangerously-skip-permissions        # autonomous coding by design; Dev is NOT a multi-tenant sandbox (14 §6)
```
- Do **not** use the `--bare` flag when running on subscription OAuth: `--bare` skips OAuth/keychain reads and requires `ANTHROPIC_API_KEY`. Since DevCake prefers subscription auth (§4), the template omits `--bare`.
- `--output-format stream-json` emits realtime JSONL events (`system/init`, `assistant`/`user` message events, noise events like `system/thinking_tokens`) ending in one `{type:"result"}` event that carries **the exact fields of the old `json` blob**: `result`, `session_id`, `num_turns`, `duration_ms`, `usage`, `modelUsage`, `total_cost_usd` — token extraction stays first-class, the entrypoint just reads the last `result` event instead of the whole stdout (verified live 2026-07-12). **`--verbose` is mandatory** with `-p` + `stream-json` (the CLI errors out without it).

### Live output relay (§1a)

The entrypoint pumps the harness's stdout line-by-line instead of buffering it (`dev_entrypoint.py`): each event is rendered to a **condensed one-liner** (tool name + truncated args, assistant text snippet, result summary — thinking/noise events skipped) which is (a) printed to the entrypoint's own stdout, where Dagu's container executor captures it **live into the step log** (Dagu UI), and (b) batched into `run.log {lines: […]}` envelopes over Redis (≤ 50 lines / ~2 s, 2000 chars/line, 20k-line flood cap) feeding the admin panel's run terminal (`11-admin-panel.md` §4). The raw lines are still accumulated in memory, so end-of-run parsing (result, tokens) is unchanged. Relaying is best-effort — a send failure drops the batch, never the run.

### `grok-build`
```bash
grok -p "$PROMPT" --output-format streaming-json --always-approve
```
- **Verified on an installed CLI (v0.2.93, 2026-07):** binary is `grok` ("Grok Build TUI"); `-p/--single` is the headless mode; `--always-approve` auto-approves all tool executions (also available: `--permission-mode bypassPermissions|dontAsk|acceptEdits`, `--sandbox <PROFILE>` / `GROK_SANDBOX`, `--max-turns <N>`, `--json-schema` for schema-constrained output). Of these only `--max-turns` has been exercised at 0.2.112; the rest are unverified there.
- **Headless resume verified at 0.2.117 (ADR-0022, `grok_resume_nudge_*` captures):** `grok -p "$NUDGE" -r <sessionId> --output-format streaming-json --always-approve` composes; the resumed `end` event carries the SAME `sessionId` (no fork), the stream carries only the new turn's events (no history replay), and `usage`/`num_turns` are per-invocation, not cumulative. Same capture also recorded stream drift vs 0.2.112: `stopReason` is now `"end_turn"` (was `"EndTurn"`) and new `available_commands`/`usage` event types appear — nothing branches on either (the stopReason enum is annotate-only by design; unknown event types are skipped), but it is this section's unpinned-CLI caveat made real.
- The image is NOT pinned — xAI ships `install.sh` only (no versioned artifact), so the Dockerfile installs latest; re-verify the shapes below after rebuilds (ISSUES #29 residual). There is no `LABEL devcake.grok_cli_verified` in the image.
- **What the entrypoint parses**, at 0.2.112: `text` deltas (concatenated into the result text, coalesced for the relay §1a) and the terminal `end` event, which carries `sessionId`, `stopReason`, `num_turns` and token `usage` (§5). A failing run emits `{"type":"error"}` with **no `sessionId`**, so it cannot be located on disk afterwards. The stream carries **no tool-call events at all**, which is why the transcript comes from `grok export` (§6) and not from stdout.
- **`--output-format json` cannot be reached through `$DEVCAKE_EXTRA_ARGS`.** The invocation already passes `streaming-json`; a duplicate flag makes grok exit **2** with an empty stdout. The 0.2.93 blob shape `{text, stopReason, sessionId, requestId, thought}` is therefore unverified at 0.2.112 and unreachable from DevCake's own argv.
- Sessions persist under `~/.grok/sessions/{urlencoded-cwd}/{session_id}/` (verified at 0.2.93); `signals.json` there carries token **totals** only. At 0.2.112 it is written only for cleanly-ended sessions, which is why §5 reads the `end` event first and falls back to the file. The TUI `/usage` command is interactive-only and not used.

### `codex`
```bash
codex exec "$PROMPT" --json -o /workspace/out/last_message.txt \
  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
```
- **Verified on the installed CLI pinned by the image (`@openai/codex@0.146.0`, capture-rig re-verified 2026-08).** `--json` emits a JSONL event stream; **`-o/--output-last-message FILE` writes the final agent message to a file** — the cleanest result-text source, no JSONL parsing needed (`item.completed` with `item.type: agent_message` is the in-stream equivalent). A run that produced no agent message leaves that file **empty** rather than absent.
- **What the entrypoint parses**, at 0.146.0 (event stream unchanged from 0.144.4): `thread.started` (`thread_id`, which locates the rollout file below), `item.completed` items, and the terminal `turn.completed` carrying `usage` (§5). A failed turn ends in `turn.failed`, always preceded by a plain `{"type":"error"}` carrying the same text. **stderr says nothing about a failure** — every nonzero exit leaves the same 39 bytes of boilerplate — which is why failure is classified from the stream and never from stderr (`adr/0018-harness-fault-classification-and-backend-brake.md`).
- **A pinned `-m` costs a benign error item.** When `-m` names a model codex has no metadata for — which is every local or OpenAI-compatible backend — an `item.completed` whose `item.type` is `error` precedes `turn.started` on every run. It is not a failure; anything counting `item.completed` as work must exclude it. Without `-m` the item is absent.
- **codex has no turn cap** at 0.146.0 (re-probed at the bump) — no `--max-turns` equivalent and no config key for one — so the per-Mission-Type extra CLI args cannot bound a codex Dev the way they bound a claude or grok one. The only bound is DevCake's own run timeout (`07-dev-runtime.md`), which arrives as a signal kill.
- Sandboxing: `--dangerously-bypass-approvals-and-sandbox` is the container invocation — its own help text says "intended solely for running in environments that are externally sandboxed", which is exactly the Dev container. The plan-substitute run uses `--sandbox read-only` instead. `--ephemeral` (no session files) and `--ignore-user-config` exist for hermetic runs.
- Token usage **verified live**: the final `turn.completed` event carries `usage = {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}` — those four keys and **no others**, so a total must be summed, never read. Present even when the turn produced nothing. On `codex exec resume` these are **cumulative — now measured, not just documented** (`codex_resume_nudge_*` captures: the resumed `turn.completed` reports both invocations' tokens), which is why `RESUME_SPECS["codex"].usage_cumulative` makes the ADR-0022 token merge last-wins within a codex resume chain instead of summing. Headless resume composes as `codex exec resume <thread_id> "$NUDGE" --json -o …`; the resumed stream keeps the same `thread_id` and replays no history. No cost field in the stream → `cost_usd` is omitted (never guessed).
- Secondary source **verified live**: rollout files at `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` contain `token_count` events with `total_token_usage` (incl. `total_tokens`) and `last_token_usage`; the `thread_id` from `thread.started` locates the file.

## 2. Base images

**The harness registry (`app/devcake/harness.py`) is authoritative** (2026-07-12
rework): which image a Dev runs, which credential env vars pass through, which
secret files are delivered, and whether an OAuth device flow exists all derive
from `harness_template` via `HARNESSES`. Dev Types store no image or credential
config; the admin panel's harness combobox therefore controls what actually
runs. Dispatch also sends `DEVCAKE_HARNESS` in the run spec, which overrides
the image-baked `ENV` (kept as a fallback).

Each template is a target in the multi-stage `images/Dockerfile` (shared `base` stage for git, forge CLIs, Python relay deps, non-root user):

| Template | Build target | Installs |
|---|---|---|
| `claude-code` | `claude-code` | Node 22 + `@anthropic-ai/claude-code`, git, shared entrypoint |
| `grok-build` | `grok-build` | Grok Build via official installer, git, shared entrypoint |
| `codex` | `codex` | Node 22 + Codex CLI, git, shared entrypoint |

Images are built only by Bake (`docker-bake.hcl` — `docker buildx bake images` or `bake all`; `13-deployment.md` §6) and referenced by **tag** (`devcake/dev-*:latest`) in the run spec. Digest pinning is not implemented; rebuild Dev images lockstep with app upgrades (Dagu's `pull_policy: missing` keeps stale local tags otherwise). Compose never builds them.

## 3. Plan-mode mapping (the "/plan function")

The PLAN playbook requires the harness's native planning capability where one exists (mission-doc requirement); the mapping is explicit:

| Template | Plan invocation | Notes |
|---|---|---|
| `claude-code` | `claude -p --permission-mode plan "$PROMPT" --output-format stream-json --verbose` | Read-only plan mode; the plan text is the `result` field of the final `result` event. Entrypoint writes it to `/workspace/out/PLAN.md`. |
| `grok-build` | `grok -p "$PROMPT" --permission-mode plan --output-format streaming-json` | **Verified (CLI v0.2.93):** `--permission-mode plan` is a first-class headless mode — same convention as Claude Code. Plan text = the concatenated `text` deltas; entrypoint writes it to `/workspace/out/PLAN.md`. |
| `codex` | Plan-only prompt substitute (`codex exec` with a read-only sandbox: `--sandbox read-only`). | No documented headless plan artifact. |

In all three cases the deliverable is the same: `/workspace/out/PLAN.md`, uploaded by the app to the activity feed (`03-mission-lifecycle.md` §3).

**The materialization contract (why this is harness-agnostic):** plan modes are read-only, so the agent cannot write `PLAN.md`/`result.json` itself. The playbook states "your final message IS the plan"; the shared entrypoint then writes the harness's returned final text to `PLAN.md` and synthesizes `result.json` (`outcome: planned`). The only per-harness requirement is *some* way to run headless + read-only + return final text — the flag lives in the template, the materialization is universal. Final text comes from the **documented stdout JSON** (never session-folder internals, which are undocumented and version-churned; session files are used only where data exists nowhere else, e.g. Grok token totals). A final text under 200 chars is treated as `DEV_BAD_OUTPUT` — an empty plan fails the attempt rather than advancing the mission. Verified end-to-end for `claude-code` (M4, DEV-18→PR#3); `grok-build`'s plan flag is CLI-verified but unexercised (re-verify before assigning PLAN to a grok Dev Type); `codex` uses the documented read-only-sandbox substitute.

## 4. Credential modes

Credential requirements are **registry-driven** (`HARNESSES` in `app/devcake/harness.py` — `credential_env` / `credential_files` / `oauth`); Dev Types store no credential-kind field. **OAuth/subscription is preferred** (mission-doc requirement — the goal is to run Dev work on subscriptions):

| Template | `credential_env` (registry) | File / OAuth (registry `credential_files` / `oauth`) |
|---|---|---|
| `claude-code` | `CLAUDE_CODE_OAUTH_TOKEN` **or** `ANTHROPIC_API_KEY` (any one stored is enough) | **No** `credential_files` entry. Preferred: `claude setup-token` once → paste the OAuth token into the admin panel as `CLAUDE_CODE_OAUTH_TOKEN`. |
| `grok-build` | `XAI_API_KEY` | Device-code OAuth → secret file **`grok-auth.json`** → `~/.grok/auth.json` in-container. |
| `codex` | `CODEX_API_KEY` | Device-code OAuth → secret file **`codex-auth.json`** → `~/.codex/auth.json` in-container. |

Uploaded credential files (when the harness registry declares them) live at `/data/secrets/{dev_type}/{secret_file}` (0600); their content is delivered to the Dev in the run spec (`runspec.get`, `09-messaging.md` §3) and the entrypoint writes it to the harness-expected path (0600). Auth failure at harness launch ⇒ exit 12 ⇒ the per-Dev-Type circuit breaker (`15-errors-and-retries.md`, `DEV_AUTH`).

## 5. Token extraction (INV-5 — a report is ALWAYS posted)

Every extractor emits **TokenReport v1** (`adr/0029`, built by
`devcake_dev/harness/tokens.token_report_v1`): one CLOSED shape whose keys are
always present (`None` = unknown, never an absent key) — `schema`, `model`, the
five token counts, `reasoning_tokens` (first-class; pre-v1 it hid in a
regex-parsed `notes` string), `num_turns`, `duration_ms`, `cost_usd_native`,
`cost_usd_estimated` (always `None` image-side — the app-side ADR-0021 stamp
fills it), `source`, and `raw` (the vendor usage payload, untouched). Provenance
is the `source` field, never key-presence folklore; silence is never acceptable
(INV-5). The `source` values:

1. **`session_json`** — the harness's own structured output, named for the shape it reads:
   - `claude-code`: the final `stream-json` `result` event's `usage` object + `total_cost_usd` (authoritative, includes per-model breakdown) → `cost_usd_native`.
   - `codex`: the final `turn.completed` event's `usage` in the `--json` stream — mapping: `input_tokens→input`, `cached_input_tokens→cache_read`, `cache_write_input_tokens→cache_write` (**new at 0.146.0** — 0.144.4 had no write counter, so pre-bump streams read None, never a fabricated 0), `output_tokens→output`, `reasoning_output_tokens→reasoning_tokens`. Capture-verified at 0.146.0: those five keys, **no `total_tokens`**, present on every `turn.completed` including zero-output turns. No native cost field → `cost_usd_native` null.
2. **`end_event`** — `grok-build` **primary** at 0.2.112: the terminal `end` event's `usage` (`input_tokens` / `cache_read_input_tokens` / `output_tokens` / `total_tokens` / `reasoning_tokens`) plus `num_turns`, with `model` taken as the dominant key of `modelUsage` — whose inner keys are **camelCase** (`outputTokens`), unlike claude's `usage` (§1). Read from the stdout stream the entrypoint already parses: no session id, no filesystem access, and it works for a **failed** run, which carries a full `end` event where `signals.json` is absent. Still absent at 0.2.112: any cost field, so `cost_usd_native` is `None` — never `0`, which would read as "this run was free" in the feed report and aggregate as real spend on `devcake.cost.usd`. Standing caveat: grok is installed unpinned, so a rebuild can withdraw these fields again — whereupon extraction falls through to `signals` and then `unavailable`. Grok's **native** cost stays blank forever, but the full split feeds the app-side rate-card estimate (`adr/0021`) — the feed shows a labeled `cost (estimated, …)` line and spend aggregates on `devcake.cost.usd_estimated`, so grok is no longer a blind spot of cost visibility, only of *billed* cost.
3. **`signals`** — `grok-build` **fallback** (implemented against verified v0.2.93 shapes; pre-v1 it masqueraded as `session_json`): `signals.json` in the session directory located via the headless output's `sessionId` (`~/.grok/sessions/{urlencoded-cwd}/{session_id}/`) — carries token **totals** only (`contextTokensUsed`/`totalTokens`, plus `modelsUsed`, turn counts), so `input`/`output` stay null and no honest price computation exists. Reached only when `end`-event extraction finds no `usage` — which at 0.2.112 means a run that did not end cleanly (§1).
4. **`cumulative` / `mixed`** — merge provenance (ADR-0022 continuation chains): `cumulative` marks a resume chain whose harness reports cumulative counters (codex — the last report IS the chain total, summing would double-count); `mixed` marks a multi-chain merge whose inputs disagree on source.
5. **`unavailable`** — post an explicit report when structured extraction fails, in the same full shape. Every method above reads a **documented structured field**: there is no heuristic scraping of prose output, and **no price table anywhere in the harness layer** — `cost_usd_native` is only ever a harness-reported number. The rate-card **estimate** exists, but it is app-side (`domain/costing.py`, `adr/0021`), lands in the *separate* `cost_usd_estimated` field, and is always labeled with its rate-card vintage; `images/` never computes a dollar.

The token report message posted to the activity feed (format in `03-mission-lifecycle.md` §8) accompanies **every** step, and the same numbers ride the `run.finalize` span as `devcake.tokens.*` / `devcake.cost.usd` / `devcake.cost.usd_estimated` attributes (`12-observability.md`).

## 6. Transcript capture

The shared entrypoint's `assemble_transcript(seq, mtype, run_id, dev_type, harness, token_report, dump, result_text, result)` builds one markdown document: header with run metadata (turns / duration from the token report), then either the full session dump or (when no dump) the agent report, then the outcome JSON. That string is what ships as `transcript_md` on `run.artifacts` and becomes `{seq}_{TYPE}.md` on the PMO feed. Secrets are redacted app-side before feed post (`14-security.md` §7).

| Template | Dump source fed into `assemble_transcript` |
|---|---|
| `claude-code` | Session JSONL from `~/.claude/projects/...` (plus the `-p` JSON result). |
| `grok-build` | The output of a separate **`grok export <sessionId>`** call (shape below) — it cannot be reconstructed from stdout, since grok's stream carries no tool events at all (§1). |
| `codex` | Session files under `$CODEX_HOME/sessions/` + captured JSONL. |

There is **no** `/workspace/out/transcript/` contract — the entrypoint does not stage raw transcript trees under that path for collection.

### `grok export` output shape (observed at 0.2.112)

Verbatim, because this text is parsed downstream. `grok export <sessionId>` writes
**Markdown**, opening with a `## User` section that echoes the prompt, followed by
zero or more further sections. The section headings observed are:

| heading | contents |
|---|---|
| `## User` | the prompt, verbatim — **always present** |
| `## Assistant` | the assistant's text |
| `## Tools` | one `- Execute: <command>` line per tool execution |

Sections are **conditional**: a run whose only output was whitespace produced a
dump containing the `## User` section and nothing else. `## Assistant` and
`## Tools` were never observed together, so their relative order is unverified. The load-bearing consequence: because `## User` always echoes
the prompt, **a grok dump is never empty when a `sessionId` exists**, however
little the run actually did — dump length is not evidence of work. When no
`sessionId` exists (every `{"type":"error"}` path, §1) there is nothing to export
and the dump is empty.

**Continuations multiply `## User` sections (ADR-0022).** A resumed session's
export is cumulative — it contains every exchange of the chain, so one export
carries the original prompt AND each nudge as separate `## User` echoes; the
entrypoint keeps only the LATEST export per chain (it supersedes its
ancestors) and anchors the activity test on each invocation's OWN prompt (the
attempt-numbered nudge), which is what keeps the echo separated from new
work. Fresh-mode continuations open new sessions: their exports become
additional, attempt-labeled transcript segments.

## 7. MCP registration syntax

What the admin panel's free-text MCP command area (`11-admin-panel.md` §3) must emit — one command per line, executed verbatim by the entrypoint (failure ⇒ exit 14):

| Template | Syntax |
|---|---|
| `claude-code` | `claude mcp add [--transport http\|stdio] [--env K=V] <name> -- <command…>` (or per-run `--mcp-config <file>`) |
| `grok-build` | **Verified (CLI v0.2.93):** `grok mcp add [-t stdio\|http\|sse] [-s user\|project] [-e K=V] [-H "Name: value"] <name> [--] <command…>` (or a URL for http/sse) — writes `~/.grok/config.toml` (user scope) or `./.grok/config.toml` (project). Also `grok mcp list\|remove\|doctor`. |
| `codex` | **Verified (CLI 0.144.4; syntax re-probed at 0.146.0):** `codex mcp add <name> (--url <url> \| -- <command…>)` — stored in `~/.codex/config.toml`. |

Scope caveat: `claude mcp add`'s default (local) scope is **cwd-keyed**, and the entrypoint runs both the MCP commands and the harness in the **same** directory — the cloned repo root `/workspace/repo/<repo-name>` — so local-scope registrations survive into the harness. Anything that re-registers from a different cwd silently disappears.

### Installing MCP plugins at run time

Plugins are standalone MCP servers living OUTSIDE this repo (hexagonal rule: core ships no vendor/connector code). A Dev Type opts in with two `mcp_setup_commands` lines — install, then register (worked example: `tutorials/03-mcp-plugins.md`; the official log connector is <https://github.com/fidecastro/devcake-logs-mcp>):

```
pip install --user --quiet "git+https://${PLUGIN_GIT_TOKEN}@github.com/OWNER/REPO@vX.Y.Z"
claude mcp add <name> -e SOME_KEY=$SOME_KEY -- python -m <package_module>
```

Mechanics: commands run before harness launch as uid 1000 with stdin closed, a 300 s cap each, and full outbound network (`07-dev-runtime.md` §5/§7). `$VAR` expands from the Dev Type's secret env vars (`11-admin-panel.md` §3) — a private-repo install token is just another secret env var (fine-grained PAT, Contents read-only; drop the `${PLUGIN_GIT_TOKEN}@` part when the repo goes public). `~/.local/bin` and `~/.npm-global/bin` are ON `PATH` in every harness image (ADR-0023 toolchain floor — this used to be a claude/codex trap requiring absolute-path registration), so `pip install --user` console scripts and user-space `npm i -g` binaries are directly invocable. Always pin a release tag — a run must not float with a moving branch. This hook is also the general per-Dev-Type **setup** lever beyond MCP: one `pip install --user` / `npm i -g` line self-provisions any user-space tool the `07` §7a floor does not bake.

## 7a. Skills (all harnesses; registry-driven skills dir)

Skill-store skills (`02-domain-model.md` `DevType.skills` / `skills_required`)
are materialized by the entrypoint before harness launch into the harness's
**registry-declared skills directory** (`harness.py` `skills_dir`, snapshotted
onto the Run at dispatch and delivered as the runspec `skills_dir` key). All
three CLIs read the same `SKILL.md` format; the verified read-set per pinned
or observed CLI:

| Harness | skills_dir | Verified read locations |
|---|---|---|
| `claude-code` (2.1.210) | `.claude/skills` | `~/.claude/skills` only (cli.js-verified; `-p` print-mode load live-verified) |
| `grok-build` (0.2.103) | `.agents/skills` | `~/.agents/skills`, `~/.grok/skills`, and `~/.claude/skills` claude-compat (`grok inspect`-verified) |
| `codex` (0.144.4) | `.agents/skills` | `~/.agents/skills` (user), repo `.agents/skills`, `/etc/codex/skills` ([official docs](https://developers.openai.com/codex/skills) + binary strings) |

One canonical dir per harness, deliberately: grok reads BOTH `.agents` and
`.claude` dirs, so writing skills to two locations would double-list every
skill in the agent's own discovery — do not add a compat double-write. A
harness whose registry entry declares no `skills_dir` skips skills at
dispatch with a warning (and the admin UI disables the selector).

**Consult-optional by default (ADR-0016):** skill *invocation* is model-driven
(description matching). Installing a skill makes it **Available** as a
resource — DevCake must run with zero skills selected. **Required** skills
(`DevType.skills_required`, a subset of `skills`) also get a short soft-force
append on the composed prompt (“must consult these skills”). That is
**instructional only** — harnesses do not hard-enforce skill load; honesty
in the admin UI matches this contract. Skills are domain modules, never
mission-step scripts (`app/devcake/skills/README.md`).

## 8. Running against local / OpenAI-compatible backends

A template owns an invocation, not a model (§1), so any harness can be pointed at a
local or OpenAI-compatible backend through its own base-URL and API-key environment
variables; DevCake neither knows nor validates which backend a Dev Type reaches.
What every template *does* assume is that the model **actually tool-calls** — outside
PLAN a Dev produces its deliverable by writing files, so a model that answers in
prose yields a run that exits 0 having done nothing.

**The three templates are configured by different mechanisms**, which is the part
that surprises operators. `codex` takes the whole backend definition as `-c`
overrides in the per-Mission-Type extra CLI args. `claude-code` and `grok-build` take
**no CLI args at all** and are steered entirely by environment variables, delivered
through the Dev Type's `secret_env` names plus the GUI harness-secret store
(`11-admin-panel.md` §3). The model comes from the Dev Type's `model` field in all
three cases (`$DEVCAKE_MODEL`, §1).

| harness | how the backend is selected | base-URL shape | extra CLI args |
|---|---|---|---|
| `claude-code` | env `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` | **no `/v1` suffix** — `http://<host>:8000` | **none** |
| `grok-build` | env `GROK_MODELS_BASE_URL` + `XAI_API_KEY` | **`/v1` suffix required** — `http://<host>:8000/v1` | **none** |
| `codex` | `-c` overrides (extra CLI args) + env `CODEX_API_KEY` | `/v1` suffix, inside `-c …base_url` | the whole `-c` block |

**The `/v1` asymmetry is load-bearing and easy to get wrong.** Each CLI documents its
own half; observed at claude 2.1.210 (re-verified 2.1.221) and grok 0.2.112:

- `claude-code` defaults to `https://api.anthropic.com` and appends the route itself
  (`/v1/messages`), so `ANTHROPIC_BASE_URL` stops **before** `/v1`.
- `grok-build` appends only the method path and fetches the model list from
  `{base_url}/models`, so the `/v1` must be **in** the variable.
- `codex` is handed `base_url` verbatim inside the `-c` block, `/v1` included.

**Credentials.** Set the key variable even when the backend ignores it: each CLI
treats its key as required, so a backend that checks nothing still needs a non-empty
value. Where a Dev Type has a stored OAuth credential file for its template (§4) it
is still delivered; clear it if the Dev Type is dedicated to a local backend.

**Ports.** Where a deployment fronts the model with a request-rewriting proxy, point
only the harness that proxy exists for at it — a transformation shaped for one CLI is
not neutral for the others.

The operator walkthrough — which field in the admin panel takes which value, and the
codex `-c` block verbatim — is `11-admin-panel.md` §3.

> **Known limitation — codex and large tool schemas.** `codex` 0.144.4 declares an
> `exec_command` tool with ten properties, one of which is required. Models that
> handle large optional-parameter schemas poorly answer with **prose containing
> invented tool syntax**, execute nothing and exit **0**, which DevCake reports as
> exit 11 `DEV_BAD_OUTPUT` — with no brake (ADR-0018 keys on exit 15;
> `15-errors-and-retries.md` §4a). PLAN can mask it: plan mode synthesises its
> result from returned text. Remedies: assign the stage to `grok-build` or
> `claude-code`; slim tool schemas or force `tool_choice` proxy-side; use a model
> that handles large optional schemas. **Recognition:** codex writes no
> `result.json` while the other harnesses finish the same task on the same backend.

## 9. Adding or changing a template (checklist)

1. Add a `HARNESSES` entry in `app/devcake/harness.py` (`image`, `credential_env`, `credential_files`, optional `oauth` flow, optional `skills_dir` — the home-relative dir the CLI reads personal skills from; leave unset if unsupported) and the new value to `DevType.harness_template`'s Literal (`config.py`).
2. Add a target to `images/Dockerfile` (bake `ENV DEVCAKE_HARNESS=<id>` as fallback) and a matching target in `docker-bake.hcl` (group `images` / `all`).
3. Add the invocation + renderer + token-extraction branches in `images/common/dev_entrypoint.py` (§1, §1a, §5).
4. Run the M1 hello-world DAG with the new image, then the M3 ONBOARD end-to-end demo.
5. Update the token-extraction section (§5) and this document.
