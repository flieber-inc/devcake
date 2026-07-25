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
| `claude-code` | Claude Code (`claude`) | CLI default | `judgment` pins `claude-fable-5`; `mapper` pins `claude-haiku-4-5` |
| `grok-build` | Grok Build (`grok`) | Registry default `grok-4.5` | `implementer` leaves the model empty and receives that registry default |
| `codex` | Codex CLI (`codex`) | CLI default | *(none seeded)* |

Each template defines: base image, invocation pattern, plan-mode mapping, credential modes, MCP registration syntax, transcript source, and token-extraction strategy.

> Verification is capability- and version-specific, not blanket. Invocation,
> headless output, plan flags, MCP syntax, and token extraction below record
> live evidence at the version each statement names. The **headless output
> shapes in §1, §5 and §6 were re-captured on 2026-07-25** inside the baked Dev
> images at **grok 0.2.112 (`9bbd559437`)**, **codex-cli 0.144.4** and **Claude
> Code 2.1.210** — versions read from inside the image by the capture rig (§8)
> and recorded in every sidecar's `cli_version`. Where a 0.2.112 measurement
> differs from the older **Grok Build 0.2.93** record, the 0.2.112 statement
> wins and says so; grok claims *not* re-measured at 0.2.112 (plan mode §3, MCP
> syntax §7, skills read-set §7a at **0.2.103**) keep their original version
> tag and are unverified at 0.2.112. The Grok image installs latest rather than
> a pinned artifact, so every rebuild can invalidate the recorded shapes. Grok
> PLAN is flag-verified but not exercised end-to-end (§3). §8 is narrower
> still: it is one model+backend pairing measured on 2026-07-25, not a
> statement about local backends generally. Treat each statement's own version
> and caveat as its verification boundary.
>
> The 2026-07-25 captures are committed verbatim under
> `app/tests/fixtures/harness_streams/` (`<name>.jsonl` stream, `<name>.meta.json`
> measured sidecar, `<name>.stderr.txt`, grok `<name>.dump.txt`); a claim below
> that names a `<harness>_<scenario>` fixture is backed by those bytes, and a
> claim sourced from the campaign's own notes rather than from a committed
> fixture says so. They were taken
> against the stub backend of `scripts/harness_capture/stub_backend.py`, so the
> **presence and shape** of a field is real CLI evidence while any **numeric
> value** in them is whatever the stub served.

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
- **Verified on an installed CLI (v0.2.93, 2026-07):** binary is `grok` ("Grok Build TUI"); `-p/--single` is the headless mode; `--always-approve` auto-approves all tool executions (also available: `--permission-mode bypassPermissions|dontAsk|acceptEdits`, `--sandbox <PROFILE>` / `GROK_SANDBOX`, `--max-turns <N>`, `--json-schema` for schema-constrained output). `--max-turns` was **exercised end-to-end at 0.2.112** (below); the other flags are unverified at 0.2.112.
- The image is NOT pinned — xAI ships `install.sh` only (no versioned artifact), so the Dockerfile installs latest; re-verify the shapes below after rebuilds (ISSUES #29 residual). There is no `LABEL devcake.grok_cli_verified` in the image.

**Headless stream, observed at 0.2.112 (captured 2026-07-25).** Not a complete
event catalogue — the distinct `type` values present across the eleven `grok_*`
captures are exactly:

| `type` | payload observed | fixture |
|---|---|---|
| `text` | assistant text under **`data`** | `grok_healthy`, `grok_refusal`, `grok_whitespace` |
| `end` | `{stopReason, sessionId, requestId, usage, num_turns, modelUsage}` | the same three + `grok_tool_only`, `grok_turn_budget` |
| `max_turns_reached` | no payload — the bare `{"type":"max_turns_reached"}` | `grok_turn_budget` |
| `error` | `{message}` — **never a `sessionId`** | `grok_empty`, `grok_http_401/429/500`, `grok_truncated` |

`thought` is recorded from the 0.2.93 verification and is **unverified at
0.2.112**: no capture contains one (the stub emitted no reasoning content).
**No tool-call events at all** — this survives at 0.2.112 and is now measured
rather than assumed: `grok_tool_only.jsonl` is 16 real tool executions and is
*one line long* (the `end` event). Each capture carries exactly one `text`
event because the stub delivered the completion in a single `content_block_delta`;
delta granularity follows the backend's chunking, so the entrypoint still
coalesces `text` deltas for the relay (§1a) and concatenates them for the
result text. `sessionId` comes from the `end` event only — an `error` event
carries none, so a crashed run cannot be located on disk.

- **Usage/cost — corrected at 0.2.112.** The older record "neither contains usage/cost fields in this version" was true of 0.2.93 and is **false at 0.2.112**: every captured `end` event carries `usage = {input_tokens, cache_read_input_tokens, output_tokens, reasoning_tokens, total_tokens}`, `num_turns`, and `modelUsage = {<model>: {inputTokens, outputTokens, cacheReadInputTokens, modelCalls}}` — an input/output split inline in stdout, which 0.2.93 did not have. There is **no cost field** (`total_cost_usd` or otherwise) in any capture. Caveat: the numbers are the stub's; the presence and key names are the CLI's. §5 records what this means for extraction.
- **Turn cap, observed at 0.2.112:** with `--max-turns 2` grok emits **both** a dedicated `{"type":"max_turns_reached"}` event **and** an `end` event with `stopReason: "Cancelled"` (`num_turns: 2`), writes `Error: max turns reached` to stderr, and exits **1** (`grok_turn_budget`). Consumers must expect the pair, not one or the other. With no `--max-turns`, grok ended a 16-turn always-tool-calling run itself with `stopReason: "EndTurn"` and exit 0 (`grok_tool_only`); whether that 16 is a default cap is unverified.
- **`--output-format json` is unreachable through `$DEVCAKE_EXTRA_ARGS` at 0.2.112.** The invocation already passes `--output-format streaming-json`, and a second one makes grok exit **2** with `error: the argument '--output-format <OUTPUT_FORMAT>' cannot be used multiple times` and an empty stdout (`grok_json_blob`, `.stderr.txt`). The 0.2.93 blob shape `{text, stopReason, sessionId, requestId, thought}` is therefore unverified at 0.2.112 and cannot be reached from DevCake's own argv.
- Sessions persist under `~/.grok/sessions/{urlencoded-cwd}/{session_id}/` (verified at 0.2.93) — the `sessionId` from the headless output locates the directory; `signals.json` there carries `contextTokensUsed`/`totalTokens`, `contextWindowTokens`, `modelsUsed` (totals only — no input/output split, no cost). The capture campaign reports `signals.json` **still present at 0.2.112, and only for cleanly-ended sessions** (present for `grok_healthy`/`whitespace`/`refusal`/`tool_only`, absent after a turn-cap stop or an HTTP failure) — **unverified here**: no `signals.json` is committed as a fixture, so treat it as a campaign note (`app/tests/fixtures/harness_streams/README.md`) until one is. The TUI `/usage` command is interactive-only and not used.

### `codex`
```bash
codex exec "$PROMPT" --json -o /workspace/out/last_message.txt \
  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
```
- **Verified on the installed CLI pinned by the image (`@openai/codex@0.144.4`, 2026-07).** `--json` emits a JSONL event stream; **`-o/--output-last-message FILE` writes the final agent message to a file** — the cleanest result-text source, no JSONL parsing needed (`item.completed` with `item.type: agent_message` is the in-stream equivalent). A run that produced no agent message leaves that file **empty** rather than absent (`codex_empty`, `codex_empty_no_model` — `last_message_bytes: 0`), and a whitespace-only completion writes the whitespace verbatim (`codex_whitespace`, 4 bytes).

**`--json` stream, observed at 0.144.4 (captured 2026-07-25).** Not a complete
event catalogue — the distinct `type` values present across the thirteen
`codex_*` captures are exactly `thread.started`, `turn.started`,
`item.started`, `item.completed`, `turn.completed`, `turn.failed`, `error`;
the distinct `item.type` values are `error`, `agent_message`,
`command_execution`. Shapes:

| sequence | events | fixture |
|---|---|---|
| success | `thread.started` (`thread_id`) → `turn.started` → 0..n `item.*` → `turn.completed` (`usage`), exit 0 | `codex_healthy`, `codex_empty`, `codex_empty_no_model`, `codex_whitespace`, `codex_refusal`, `codex_tool_only` |
| failure | `thread.started` → `turn.started` → 1..n `{"type":"error","message":…}` → `{"type":"turn.failed","error":{"message":…}}`, exit 1 | all eight `codex_http_*` / `codex_no_route` / `codex_truncated*` |

- The **`turn.failed`** terminal event is real at 0.144.4 (present on all eight failure captures) and is always preceded by a plain `{"type":"error"}` carrying the same text. With default retries, a failing turn emits five `Reconnecting... N/5` `error` events first and takes ~6.5 s (`codex_http_401_retrying`, `codex_truncated_retrying`); with retries disabled every failure capture finished in under 0.6 s. codex fails **fast**, where grok burns ~5¾ minutes of backoff before reporting (`grok_empty` / `grok_http_500` / `grok_truncated`: 344.7–344.9 s).
- **A pinned `-m` costs a benign error item.** When `-m` names a model codex has no metadata for — which is every local/OpenAI-compatible backend — codex emits `{"type":"item.completed","item":{"type":"error","message":"Model metadata for \`<model>\` not found. Defaulting to fallback metadata; …"}}` **before `turn.started`, on every run** (`codex_healthy` and eleven others). It is not a failure; anything counting `item.completed` as work must exclude it. Without `-m` the item is absent (`codex_empty_no_model`).
- **stderr carries nothing about the failure.** Every codex capture that exited nonzero has **exactly 39 bytes** of stderr, and those bytes are `Reading additional input from stdin...\n` — boilerplate, identical across a 400, a 401, a 429, a 500, a 404 and a truncated stream. The only diagnostic line codex writes to stderr, `Warning: no last agent message; wrote empty content to <path>` (134 bytes total, in `codex_empty`, `codex_empty_no_model`, `codex_tool_only`), appears on **zero-exit** runs. This is the second independent confirmation of ADR-0018's founding premise: classifying harness failure on stderr cannot work. (For contrast, the two Claude Code 2.1.210 captures — both successes — carry **0 bytes** of stderr: `claude_healthy`, `claude_refusal`. No 2.1.210 *failure* capture exists, so claude's failure stderr is unverified at that version.)
- Sandboxing: `--dangerously-bypass-approvals-and-sandbox` is the container invocation — its own help text says "intended solely for running in environments that are externally sandboxed", which is exactly the Dev container. The plan-substitute run uses `--sandbox read-only` instead. `--ephemeral` (no session files) and `--ignore-user-config` exist for hermetic runs.
- Token usage **verified live**: the final `turn.completed` event carries `usage = {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}` (probe: 12196/10112/5/0). Re-confirmed at 0.144.4 across all six `turn.completed` captures: those four keys and **no others** — in particular **no `total_tokens`** on `turn.completed`, so a total must be summed, never read. `usage` is present even when the turn produced nothing (`codex_empty`: `output_tokens: 0`). Caveat: on `codex exec resume` these are cumulative across the session — DevCake runs are single-session so this is naturally correct. No cost field in the stream → `cost_usd` is omitted (never guessed).
- Secondary source **verified live**: rollout files at `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` contain `token_count` events with `total_token_usage` (incl. `total_tokens`) and `last_token_usage`; the `thread_id` from `thread.started` locates the file.

### Turn caps — codex is unbounded (§1b, observed 2026-07-25)

| Template | Cap flag | What exhaustion looks like |
|---|---|---|
| `claude-code` (2.1.219) | `--max-turns <N>` | `is_error:true`, `subtype:"error_max_turns"`, exit 1 (`claude_max_turns`) |
| `grok-build` (0.2.112) | `--max-turns <N>` | `max_turns_reached` **and** `end` `stopReason:"Cancelled"`, exit 1 (`grok_turn_budget`) |
| `codex` (0.144.4) | **none** | nothing — the run does not stop |

**Operational hazard.** `codex exec` at 0.144.4 exposes no `--max-turns`
equivalent and no config key for one, so the per-Mission-Type extra CLI args
(`02-domain-model.md` §9) cannot bound a codex Dev the way they bound the
seeded ONBOARD claude Dev. Against a backend that answers every turn with a
tool call, a codex run is **unbounded**: the capture campaign measured one such
run issuing **~5,535 requests in ~7 minutes** and still going when it was killed
at the container level. (That run produced no capture files at all — it is a
campaign note in `app/tests/fixtures/harness_streams/README.md`, not a committed
fixture; the committed `codex_tool_only` is the redesigned, terminating lane.)
The only bound left is DevCake's own run timeout (`07-dev-runtime.md`), which
arrives as a signal kill rather than as a turn-budget stop — so a looping codex
Dev burns wall-clock and backend capacity for the whole timeout, every attempt.

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

Implemented extraction methods (`TokenReport.extraction_method`); silence is never acceptable (INV-5):

1. **`session_json`** — the harness's own structured output, named for the shape it reads:
   - `claude-code`: the final `stream-json` `result` event's `usage` object + `total_cost_usd` (authoritative, includes per-model breakdown).
   - `codex`: the final `turn.completed` event's `usage` in the `--json` stream — mapping: `input_tokens→input`, `cached_input_tokens→cache_read`, `output_tokens→output`; `reasoning_output_tokens` recorded in `notes`. Re-confirmed at 0.144.4 (§1): those four keys, **no `total_tokens`**, present on every `turn.completed` including zero-output turns. No native cost field → `cost_usd` omitted.
   - `grok-build` **fallback** (implemented against verified v0.2.93 shapes, and now second in line): `signals.json` in the session directory located via the headless output's `sessionId` (`~/.grok/sessions/{urlencoded-cwd}/{session_id}/`) — carries token **totals** only (`contextTokensUsed`/`totalTokens`, plus `modelsUsed`, turn counts), so `input`/`output` stay null and `cost_usd` is omitted (no split → no honest price computation). It is kept because its survival at 0.2.112 is an uncommitted campaign note (§1): dropping it would be as much of a guess as relying on it. It is reached only when `end`-event extraction finds no `usage`.
2. **`end_event`** — `grok-build` **primary**, since the 0.2.112 captures: the terminal `end` event's `usage` (`input_tokens` / `cache_read_input_tokens` / `output_tokens` / `total_tokens`; `reasoning_tokens` recorded in `notes`, the codex convention) plus `num_turns`, with `model` taken as the dominant key of `modelUsage` — whose inner keys are **camelCase** (`outputTokens`), unlike claude's `usage` (§1). Read from the stdout stream the entrypoint already parses: no session id, no filesystem access, and it works for a **failed** run — `grok_turn_budget` exits 1 and still carries a full `end` event, exactly the case `signals.json` cannot serve (it is written only for cleanly-ended sessions). Still absent at 0.2.112: any cost field, so `cost_usd` is `None` — never `0`, which would read as "this run was free" in the feed report and aggregate as real spend on `devcake.cost.usd`. (The capture rig's stub reports no cost of its own, so absence against a *real* xAI backend is untested; `test_entrypoint_tokens.py` asserts no capture carries a cost key, and that assertion is the tripwire for revisiting the mapping if one ever appears.) Two standing caveats: the captured token *values* came from a stub (only the field names and their presence are CLI evidence), and grok is installed unpinned, so a rebuild can withdraw the fields again — whereupon extraction falls through to `session_json` and then `unavailable`. Grok remains the weak spot of **cost** visibility (`12-observability.md` §4); it is no longer a weak spot of token visibility.
3. **`unavailable`** — post an explicit report when structured extraction fails. Every method above reads a **documented structured field**: there is no heuristic scraping of prose output, and **no** in-code price table that invents `cost_usd` from token counts.

The token report message posted to the activity feed (format in `03-mission-lifecycle.md` §8) accompanies **every** step, and the same numbers ride the `dev.run` span as `devcake.tokens.*` / `devcake.cost.usd` attributes (`12-observability.md`).

## 6. Transcript capture

The shared entrypoint's `assemble_transcript(seq, mtype, run_id, dev_type, harness, token_report, dump, result_text, result)` builds one markdown document: header with run metadata (turns / duration from the token report), then either the full session dump or (when no dump) the agent report, then the outcome JSON. That string is what ships as `transcript_md` on `run.artifacts` and becomes `{seq}_{TYPE}.md` on the PMO feed. Secrets are redacted app-side before feed post (`14-security.md` §7).

| Template | Dump source fed into `assemble_transcript` |
|---|---|
| `claude-code` | Session JSONL from `~/.claude/projects/...` (plus the `-p` JSON result). |
| `grok-build` | The output of a separate **`grok export <sessionId>`** call (shape below) — it cannot be reconstructed from stdout, since grok's stream carries no tool events at all (§1). |
| `codex` | Session files under `$CODEX_HOME/sessions/` + captured JSONL. |

There is **no** `/workspace/out/transcript/` contract — the entrypoint does not stage raw transcript trees under that path for collection.

### `grok export` output shape (observed at 0.2.112, captured 2026-07-25)

Verbatim, because this text is parsed downstream. `grok export <sessionId>` writes
**Markdown**, opening with a `## User` section that echoes the prompt, followed by
zero or more further sections. The section headings observed across the five
`grok_*.dump.txt` fixtures are exactly:

| heading | contents observed | fixture |
|---|---|---|
| `## User` | the prompt, verbatim — **always present** | all five |
| `## Assistant` | the assistant's text | `grok_healthy`, `grok_refusal` |
| `## Tools` | one `- Execute: <command>` line per tool execution | `grok_tool_only` (16 lines), `grok_turn_budget` (2 lines) |

Sections are **conditional**: a run whose only output was whitespace produced a
dump containing the `## User` section and nothing else (`grok_whitespace`, 100
bytes). No capture contains both `## Assistant` and `## Tools`, so their relative
order is unverified. The load-bearing consequence: because `## User` always echoes
the prompt, **a grok dump is never empty when a `sessionId` exists**, however
little the run actually did — dump length is not evidence of work. When no
`sessionId` exists (every `{"type":"error"}` path, §1) there is nothing to export
and the dump is empty.

## 7. MCP registration syntax

What the admin panel's free-text MCP command area (`11-admin-panel.md` §3) must emit — one command per line, executed verbatim by the entrypoint (failure ⇒ exit 14):

| Template | Syntax |
|---|---|
| `claude-code` | `claude mcp add [--transport http\|stdio] [--env K=V] <name> -- <command…>` (or per-run `--mcp-config <file>`) |
| `grok-build` | **Verified (CLI v0.2.93):** `grok mcp add [-t stdio\|http\|sse] [-s user\|project] [-e K=V] [-H "Name: value"] <name> [--] <command…>` (or a URL for http/sse) — writes `~/.grok/config.toml` (user scope) or `./.grok/config.toml` (project). Also `grok mcp list\|remove\|doctor`. |
| `codex` | **Verified (CLI 0.144.4):** `codex mcp add <name> (--url <url> \| -- <command…>)` — stored in `~/.codex/config.toml`. |

Scope caveat: `claude mcp add`'s default (local) scope is **cwd-keyed**, and the entrypoint runs both the MCP commands and the harness in the **same** directory — the cloned repo root `/workspace/repo/<repo-name>` — so local-scope registrations survive into the harness. Anything that re-registers from a different cwd silently disappears.

### Installing MCP plugins at run time

Plugins are standalone MCP servers living OUTSIDE this repo (hexagonal rule: core ships no vendor/connector code). A Dev Type opts in with two `mcp_setup_commands` lines — install, then register (worked example: `tutorials/03-mcp-plugins.md`; the official log connector is <https://github.com/fidecastro/devcake-logs-mcp>):

```
pip install --user --quiet "git+https://${PLUGIN_GIT_TOKEN}@github.com/OWNER/REPO@vX.Y.Z"
claude mcp add <name> -e SOME_KEY=$SOME_KEY -- python -m <package_module>
```

Mechanics: commands run before harness launch as uid 1000 with stdin closed, a 300 s cap each, and full outbound network (`07-dev-runtime.md` §5/§7). `$VAR` expands from the Dev Type's secret env vars (`11-admin-panel.md` §3) — a private-repo install token is just another secret env var (fine-grained PAT, Contents read-only; drop the `${PLUGIN_GIT_TOKEN}@` part when the repo goes public). Register via `python -m <module>` or an absolute path: `pip install --user` puts console scripts in `~/.local/bin`, which is **not** on `PATH` in the claude/codex images. Always pin a release tag — a run must not float with a moving branch.

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

A template owns an invocation, not a model (§1). Any harness can therefore be
pointed at a local or OpenAI-compatible backend through its own base-URL / API-key
environment variables, and DevCake neither knows nor validates which backend a Dev
Type reaches. What every template *does* assume is that the model **actually tool-calls**:
outside PLAN, a Dev produces its deliverable by writing files, so a model that
answers in prose yields a run that exits 0 having done nothing. The pairing below
is one measured worked example of that failure mode — read it as the shape to look
for, not as a verdict on local backends.

### The measured pairing (2026-07-25)

| element | measured |
|---|---|
| Backend | vLLM serving `DeepSeek-V4-Flash-DSpark-Abliterated`, `max_model_len` 300000, at `http://192.168.2.20:8000` (raw) |
| Proxy | request-rewriting proxy on `:8765` that repositions the system prompt for codex |
| Routes | **both** ports serve `/v1/chat/completions`, `/v1/responses` and `/v1/messages` |
| CLI versions | read from inside the baked images: codex-cli **0.144.4**, claude **2.1.210**, grok **0.2.112** |

**Symptom.** `codex`, invoked through DevCake's own `harness_argv` (§1), never makes
a real tool call. It emits tool syntax as **prose** inside an `agent_message` — in at
least three invented formats (`<tool_call type="exec" cmd="…">`, `<exec>…</exec>`,
and narrated HTML with a fabricated `▶` prompt and hallucinated command output).
codex executes nothing, writes no files, and exits **0**. 5 of 5 mission-shaped runs
behaved this way.

**The backend is not at fault**, and that was established before anything else was
touched. Direct protocol probes with no CLI involved, on both ports: `POST /v1/responses`
with one simple tool returns a real `function_call`; `POST /v1/messages` returns a real
`tool_use`. And `claude-code`, against the *same* model, backend and prompt, produced
**4 real `tool_use` blocks** and completed the task.

### What the bisect isolated

codex's verbatim outbound request was taken from the capture stub's `journal.jsonl`
(below) and replayed against `:8765` with `stream: false`:

| variant | real `function_call`? |
|---|---|
| codex's request verbatim (10 tools) | no |
| minus the `namespace` and `web_search` tools (10 → 8 function tools) | no |
| `exec_command` alone | no |
| codex's full `instructions` + `input`, but ONE simple tool (`get_weather`) | **yes** |
| codex's tools + `tool_choice: "required"` | **yes** |
| `exec_command` with only its REQUIRED parameter (`cmd`) | **yes** |
| `exec_command` name kept, minimal schema | **yes** |
| codex's request minus `reasoning` | no |
| no `instructions`, codex's tools | no |

**Root cause: the size of the optional-parameter surface.** Not the backend, not the
proxy, not the `instructions`, not `reasoning`, not the tool count, and not the
non-`function` tool types — each of those was eliminated by a row above.
`exec_command` declares ten properties of which exactly one (`cmd`) is required;
removing the nine optional ones (`justification`, `login`, `max_output_tokens`,
`prefix_rule`, `sandbox_permissions`, `shell`, `tty`, `workdir`, `yield_time_ms`)
restores correct tool calling. This is a model-side limitation of **this model**
handling large optional schemas — not a DevCake bug and not a vLLM bug.

**That ten-function surface only exists because the model is pinned.** With `-m`
set, codex 0.144.4 sends the classic `tools` array (`exec_command`, `write_stdin`,
`update_plan`, …) the bisect measured. With **no `-m`**, the same CLI sends **no
`tools` key at all** — the tool surface moves into an `additional_tools` *input
item* advertising a `custom` JavaScript `exec` orchestrator plus `wait` and
`request_user_input`. Anything that reads `body["tools"]` to decide what to answer
(the capture stub does exactly that, `stub_backend.py::pick_tool`) therefore sees
nothing and answers nothing. Fixture evidence for the difference: `codex_empty`
(with `-m`) and `codex_empty_no_model` (without) are the same CLI against the same
backend condition and differ in their streams. The request-body detail itself is a
campaign note read from the stub's uncommitted `journal.jsonl`
(`app/tests/fixtures/harness_streams/README.md`), not a committed fixture. Practical
consequence: `$DEVCAKE_MODEL` on a codex Dev Type is not only a model choice, it
decides which tool protocol the backend is asked to support.

### What DevCake sees, and why PLAN masks it

Because the model is being pushed to the edge of its schema-handling ability, it
degrades **probabilistically**: it tool-calls on some runs and narrates on others.
When it narrates, no Dev can write `/workspace/out/result.json`, so the run reports
**exit 11 `DEV_BAD_OUTPUT`** (`15-errors-and-retries.md` §1) — on every container, at
once, for the same reason the ADR-0018 incident was fleet-wide: the transducer is
uniform, so a shared backend fault arrives identically everywhere with no contagion.

**PLAN is the exception, and that asymmetry is the confusing part.** Plan mode is
read-only by construction, so the entrypoint synthesises `PLAN.md` and `result.json`
from the returned text (§3), gated only on that text being ≥ 200 chars. A PLAN step
therefore **succeeds with zero tool calls**, while ONBOARD, EXECUTE and REVIEW fail.
A board can look healthy until the pipeline advances into a stage that needs a real
tool call.

**Known gap — the ADR-0018 brake does not cover this.** `backend_correlated` /
`backend_degraded` key on `error_class == "DEV_HARNESS_FAULT"` (exit 15,
`15-errors-and-retries.md` §4a). These runs are `DEV_BAD_OUTPUT` (exit 11), so a
fleet-wide bad-output cascade is throttled by nothing, excused by nothing, and every
failure counts toward `max_attempts`. Recorded, not fixed.

### Operator remedies

In increasing order of effort; all three are operator-side, because nothing in
DevCake misbehaved — the invocation, the entrypoint and the predicate all did exactly
what they specify:

| effort | remedy | evidence |
|---|---|---|
| lowest | set `tool_choice: "required"` on the request | worked every time in the bisect |
| medium | slim tool schemas proxy-side, dropping optional properties on `/v1/responses` — the proxy already rewrites requests, so it is the natural home | dropping `exec_command`'s nine optional properties restored tool calling |
| highest | use a model with better large-optional-schema handling | `claude-code` on this same model and backend never exhibited the fault |

### The capture rig (`scripts/harness_capture/`)

The rig this section was measured with. It runs **inside a baked Dev image** on
purpose: host CLI versions drift from the image pins (codex host 0.144.6 vs image
0.144.4; grok is installed unpinned, §2), and a capture taken at the wrong version
silently stops describing what production runs.

| file | role |
|---|---|
| `in_container.py` | One real harness run inside the image. argv comes from the entrypoint's own `harness_argv` (§1), so a capture cannot drift from the production invocation; the backend is preflighted, so a routing failure aborts loudly instead of being recorded as a backend fault; exit status, stdout, stderr, codex's `-o` file and grok's `grok export` are all recorded verbatim; and the **current** predicate is run against the capture with its verdict written beside the intended one — a mismatch is the finding, never something the capture is edited to hide. |
| `stub_backend.py` | Stdlib three-protocol stub — `/v1/messages`, `/v1/responses`, `/v1/chat/completions`, plus `/v1/models` and `/healthz` — with deterministic failure injection (401/429/500, truncated stream, empty completion, tool-only, refusal). Its `journal.jsonl` records each CLI's outbound request **verbatim**; that record is what made the bisect above possible at all, and it is also what proves a capture hit the stub rather than quietly reaching a real API. |
| `prompts/*.md` | The prompt shapes: `trivial.md` (one word, no tools), `mission_shaped.md` (edit a file + write `out/result.json`), `execute_real.md` (the real EXECUTE playbook, absolute `/workspace` paths included). |

Its output is `app/tests/fixtures/harness_streams/` — the fixtures cited throughout
§1, §5 and §6, one committed stream per backend condition per harness, each with a
machine-written `.meta.json` of measured facts (`cli_version`, `argv`, `exit_code`,
byte counts, `session_id`, duration). One rig hazard worth knowing before re-running
it: a backend that omits `total_tokens` from the Responses `response.completed`
payload aborts **every** codex turn with `failed to parse ResponseCompleted: missing
field 'total_tokens'` — codex requires the field on the wire even though it does not
re-emit it on `turn.completed` (§1). That was a stub defect, found and fixed during
the campaign; it is the same failure a real local backend would produce.

Honesty rule, stated in the stub itself: it answers HTTP and nothing else. Every
committed fixture byte is real CLI stdout, and a scenario a real backend could not
produce is never added.

## 9. Adding or changing a template (checklist)

1. Add a `HARNESSES` entry in `app/devcake/harness.py` (`image`, `credential_env`, `credential_files`, optional `oauth` flow, optional `skills_dir` — the home-relative dir the CLI reads personal skills from; leave unset if unsupported) and the new value to `DevType.harness_template`'s Literal (`config.py`).
2. Add a target to `images/Dockerfile` (bake `ENV DEVCAKE_HARNESS=<id>` as fallback) and a matching target in `docker-bake.hcl` (group `images` / `all`).
3. Add the invocation + renderer + token-extraction branches in `images/common/dev_entrypoint.py` (§1, §1a, §5).
4. Run the M1 hello-world DAG with the new image, then the M3 ONBOARD end-to-end demo.
5. Update the token-extraction section (§5) and this document.
