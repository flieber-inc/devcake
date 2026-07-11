# 08 — Harness Templates

> **Audience:** implementers. This document absorbs all harness churn: when a CLI changes or a model is swapped, only this doc (and the template files it specifies) change.
> **Depends on:** `07-dev-runtime.md` (container contract), `02-domain-model.md` (DevType, TokenReport).

A **harness template** fully describes how one model/harness pair runs inside a Dev container. Exactly three exist, hardcoded as one small config file each under `app/harness_templates/` — *hardcoded but easy to edit*: adding a fourth template or changing a model is a one-file change plus an image build (§8).

| Template id | Model | Harness CLI | v0 Dev Type |
|---|---|---|---|
| `claude-code` | Claude Fable (`claude-fable-5`) | Claude Code (`claude`) | Senior Dev |
| `grok-build` | Grok 4.5 (`grok-4.5`) | Grok Build (`grok`) | Main Dev |
| `codex` | gpt-5.6-sol | Codex CLI (`codex`) | *(unused in v0)* |

Each template defines: base image, invocation pattern, plan-mode mapping, credential modes, MCP registration syntax, transcript source, and token-extraction strategy.

> Facts below were verified in July 2026 against official docs — and, for **Grok Build (v0.2.93)** and **Codex (codex-cli 0.144.1)**, against live installed CLIs including live probes of their headless output shapes. No open items remain; every invocation, flag, and extraction path in this document is verified.

## 1. Invocation patterns

The entrypoint composes a single prompt: the Dev Type's **identifying prompt** + the Mission Type **playbook prompt** (`03-mission-lifecycle.md` §7), then invokes the harness command below **plus `$DEVCAKE_EXTRA_ARGS`** — the per-Mission-Type extra CLI args from `assignments` (`02-domain-model.md` §9), delivered in the run spec. Rule (confirmed decision): **Mission-Type-specific flags are never hardcoded** — they are admin-set config, because assignments between Mission Types and Dev Types (and therefore harnesses) can change at any time.

### `claude-code`
```bash
claude -p "$PROMPT" \
  --output-format json \
  --dangerously-skip-permissions        # containerized; the container IS the sandbox
```
- Do **not** use the `--bare` flag when running on subscription OAuth: `--bare` skips OAuth/keychain reads and requires `ANTHROPIC_API_KEY`. Since DevCake prefers subscription auth (§4), the template omits `--bare`.
- `--output-format json` returns a single JSON object including `result`, `session_id`, `num_turns`, `duration_ms`, **`usage` (tokens) and `total_cost_usd`** — token extraction is first-class.

### `grok-build`
```bash
grok -p "$PROMPT" --output-format streaming-json --always-approve
```
- **Verified on an installed CLI (v0.2.93, 2026-07):** binary is `grok` ("Grok Build TUI"); `-p/--single` is the headless mode; `--always-approve` auto-approves all tool executions (also available: `--permission-mode bypassPermissions|dontAsk|acceptEdits`, `--sandbox <PROFILE>` / `GROK_SANDBOX`, `--max-turns <N>`, `--json-schema` for schema-constrained output).
- **The image pins CLI v0.2.93** (current stable at spec time). Verified output shapes: `--output-format json` returns one object `{text, stopReason, sessionId, requestId, thought}`; `streaming-json` emits typed line events (`thought`, `text`, …, final `{type:"end", stopReason, sessionId, requestId}`). **Neither contains usage/cost fields in this version**, so token extraction uses the session files (§5). If a future CLI release adds usage/cost to headless output, bump the pin and promote that to the primary strategy.
- Sessions persist under `~/.grok/sessions/{urlencoded-cwd}/{session_id}/` (verified) — the `sessionId` from the headless output locates the directory; `signals.json` there carries `contextTokensUsed`/`totalTokens`, `contextWindowTokens`, `modelsUsed` (totals only — no input/output split, no cost). The TUI `/usage` command is interactive-only and not used.

### `codex`
```bash
codex exec "$PROMPT" --json -o /workspace/out/last_message.txt \
  --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check
```
- **Verified on an installed CLI (codex-cli 0.144.1, 2026-07 — pin this).** `--json` emits a JSONL event stream (`thread.started` → `turn.started` → `item.completed` → `turn.completed`); **`-o/--output-last-message FILE` writes the final agent message to a file** — the cleanest result-text source, no JSONL parsing needed (`item.completed` with `type: agent_message` is the in-stream equivalent).
- Sandboxing: `--dangerously-bypass-approvals-and-sandbox` is the container invocation — its own help text says "intended solely for running in environments that are externally sandboxed", which is exactly the Dev container. The plan-substitute run uses `--sandbox read-only` instead. `--ephemeral` (no session files) and `--ignore-user-config` exist for hermetic runs.
- Token usage **verified live**: the final `turn.completed` event carries `usage = {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}` (probe: 12196/10112/5/0). Caveat: on `codex exec resume` these are cumulative across the session — DevCake runs are single-session so this is naturally correct. No cost field → `cost_usd` from the price table.
- Secondary source **verified live**: rollout files at `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl` contain `token_count` events with `total_token_usage` (incl. `total_tokens`) and `last_token_usage`; the `thread_id` from `thread.started` locates the file.

## 2. Base images

Each template names a Dockerfile under `images/`:

| Template | Base | Installs |
|---|---|---|
| `claude-code` | `node:22-bookworm-slim` | `@anthropic-ai/claude-code`, git, `devcake-relay`, entrypoint |
| `grok-build` | `debian:bookworm-slim` | Grok Build via official installer, git, `devcake-relay`, entrypoint |
| `codex` | `node:22-bookworm-slim` | Codex CLI, git, `devcake-relay`, entrypoint |

Images are built by the compose build matrix (`13-deployment.md` §6) and pinned by digest in the run spec.

## 3. Plan-mode mapping (the "/plan function")

The PLAN playbook requires the harness's native planning capability where one exists (mission-doc requirement); the mapping is explicit:

| Template | Plan invocation | Notes |
|---|---|---|
| `claude-code` | `claude -p --permission-mode plan "$PROMPT" --output-format json` | Read-only plan mode; the plan text is the `result` field. Entrypoint writes it to `/workspace/out/PLAN.md`. |
| `grok-build` | `grok -p "$PROMPT" --permission-mode plan --output-format json` | **Verified (CLI v0.2.93):** `--permission-mode plan` is a first-class headless mode — same convention as Claude Code. Plan text returned as the `text` field; entrypoint writes it to `/workspace/out/PLAN.md`. |
| `codex` | Plan-only prompt substitute (`codex exec` with a read-only sandbox: `--sandbox read-only`). | No documented headless plan artifact. |

In all three cases the deliverable is the same: `/workspace/out/PLAN.md`, uploaded by the app to the activity feed (`03-mission-lifecycle.md` §3).

**The materialization contract (why this is harness-agnostic):** plan modes are read-only, so the agent cannot write `PLAN.md`/`result.json` itself. The playbook states "your final message IS the plan"; the shared entrypoint then writes the harness's returned final text to `PLAN.md` and synthesizes `result.json` (`outcome: planned`). The only per-harness requirement is *some* way to run headless + read-only + return final text — the flag lives in the template, the materialization is universal. Final text comes from the **documented stdout JSON** (never session-folder internals, which are undocumented and version-churned; session files are used only where data exists nowhere else, e.g. Grok token totals). A final text under 200 chars is treated as `DEV_BAD_OUTPUT` — an empty plan fails the attempt rather than advancing the mission. Verified end-to-end for `claude-code` (M4, DEV-18→PR#3); `grok-build`'s plan flag is CLI-verified but unexercised (re-verify before assigning PLAN to a grok Dev Type); `codex` uses the documented read-only-sandbox substitute.

## 4. Credential modes

Per DevType `credential.kind` (`02-domain-model.md` §6); **OAuth/subscription is preferred** (mission-doc requirement — the goal is to run Dev work on subscriptions):

| Template | `env_api_key` mode | `credentials_json` / subscription mode |
|---|---|---|
| `claude-code` | `ANTHROPIC_API_KEY` | `claude setup-token` (run once, interactively, on any machine with a Pro/Max/Team subscription) → paste the ~1-year OAuth token into the admin panel → injected as `CLAUDE_CODE_OAUTH_TOKEN`. Alternatively upload the `~/.claude` auth state as credentials JSON, installed by the entrypoint. |
| `grok-build` | `XAI_API_KEY` (one key covers CLI + API) | Device-code flow (RFC 8628) performed once; resulting session/config from `~/.grok/` uploaded as credentials JSON, installed to `~/.grok/` in-container. |
| `codex` | `CODEX_API_KEY` (per-invocation) or `OPENAI_API_KEY` piped to `codex login --with-api-key` | `codex login --device-auth` once (ChatGPT subscription); upload `~/.codex/auth.json` as credentials JSON, installed to `$CODEX_HOME/auth.json`. |

Uploaded credential files live at `/data/secrets/{dev_type}/` (0600); their content is delivered to the Dev in the run spec (`runspec.get`, `09-messaging.md` §3) and the entrypoint writes it to the harness-expected path (0600) before dropping privileges (`07-dev-runtime.md` §5, `14-security.md` §3). Auth failure at harness launch ⇒ exit 12 ⇒ the per-Dev-Type circuit breaker (`15-errors-and-retries.md`, `DEV_AUTH`).

## 5. Token extraction (INV-5 — a report is ALWAYS posted)

Strategy order per template; the first that yields data wins, and `TokenReport.extraction_method` records which:

1. **`session_json`** — structured harness output:
   - `claude-code`: the `-p --output-format json` result's `usage` object + `total_cost_usd` (authoritative, includes per-model breakdown).
   - `codex`: the final `turn.completed` event's `usage` in the `--json` stream — mapping: `input_tokens→input`, `cached_input_tokens→cache_read`, `output_tokens→output`; `reasoning_output_tokens` recorded in `notes`. Secondary: last `token_count` event in the session rollout file (§1).
   - `grok-build` (pinned v0.2.93, verified): `signals.json` in the session directory located via the headless output's `sessionId` (`~/.grok/sessions/{urlencoded-cwd}/{session_id}/`) — carries token **totals** only (`contextTokensUsed`/`totalTokens`, plus `modelsUsed`, turn counts). Reported with `input/output` left null, the total in `notes`, and `cost_usd` omitted (no input/output split → no honest price computation). This is the known weak spot of cost visibility (`12-observability.md`); revisit on every CLI pin bump.
2. **`stdout_parse`** — regex over captured stdout/stderr for token summary lines.
3. **`unavailable`** — post an explicit report: *"Token usage could not be extracted for this run (harness: grok-build). See the trace in OpenObserve for duration-based cost estimation."* Silence is never acceptable.

**Price table** (maintained here; used to compute `cost_usd` only when the harness doesn't report cost natively AND the model's prices are known — otherwise `cost_usd` is omitted, never guessed):

| Model | Input $/M | Output $/M | Source |
|---|---|---|---|
| `grok-4.5` | 2.00 | 6.00 | x.ai pricing, 2026-07 |
| `claude-fable-5` | *(native `total_cost_usd` used; table entry not needed)* | | |
| gpt-5.6-sol | *(fill at M4 from OpenAI pricing page)* | | |

The token report message posted to the activity feed (format in `03-mission-lifecycle.md` §8) accompanies **every** step, and the same numbers ride the `dev.run` span as `devcake.tokens.*` / `devcake.cost.usd` attributes (`12-observability.md`).

## 6. Transcript capture

| Template | Source |
|---|---|
| `claude-code` | Session JSONL from `~/.claude/projects/...` (plus the `-p` JSON result). |
| `grok-build` | `grok export <session_id>` — emits a clean Markdown transcript to stdout (verified) — plus the captured `streaming-json` stream and the session dir's `chat_history.jsonl` for tool-call detail. |
| `codex` | Session files under `$CODEX_HOME/sessions/` + captured JSONL. |

A shared `transcript_render` module (spec: one markdown document — header with run metadata, then chronological turns, tool calls collapsed to fenced blocks, secrets redacted per `14-security.md` §5) converts the raw source into `{seq}_{TYPE}.md`. Raw artifacts stay in `/workspace/out/transcript/` and are shipped to OpenObserve as logs; only the rendered markdown goes to the PMO feed.

## 7. MCP registration syntax

What the admin panel's free-text MCP command area (`11-admin-panel.md` §3) must emit — one command per line, executed verbatim by the entrypoint (failure ⇒ exit 14):

| Template | Syntax |
|---|---|
| `claude-code` | `claude mcp add [--transport http\|stdio] [--env K=V] <name> -- <command…>` (or per-run `--mcp-config <file>`) |
| `grok-build` | **Verified (CLI v0.2.93):** `grok mcp add [-t stdio\|http\|sse] [-s user\|project] [-e K=V] [-H "Name: value"] <name> [--] <command…>` (or a URL for http/sse) — writes `~/.grok/config.toml` (user scope) or `./.grok/config.toml` (project). Also `grok mcp list\|remove\|doctor`. |
| `codex` | **Verified (CLI 0.144.1):** `codex mcp add <name> (--url <url> \| -- <command…>)` — stored in `~/.codex/config.toml`. |

## 8. Adding or changing a template (checklist)

1. Add/edit the template file under `app/harness_templates/{id}.yaml` (fields: `image`, `invoke`, `invoke_plan`, `credential_modes`, `transcript_source`, `token_strategy`, `mcp_registration`).
2. Add/adjust the Dockerfile under `images/` and the compose build matrix.
3. Implement/adjust the token-extraction strategy class (one per `token_strategy` id).
4. Run the M1 hello-world DAG with the new image, then the M3 ONBOARD end-to-end demo.
5. Update the price table (§5) and this document.
