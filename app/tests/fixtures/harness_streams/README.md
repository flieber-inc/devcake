# Harness stream fixtures (scenario captures)

Regression inputs for `harness_fault`, log renderers, and token extraction
(ADR-0018). Every `.jsonl` is **verbatim stdout** from a real harness CLI run
inside a baked Dev image — not hand-written JSON.

## How a capture is made

1. Start `scripts/harness_capture/stub_backend.py` — a tiny HTTP server that
   implements OpenAI-/Anthropic-shaped routes and returns one **named scenario**
   (`/s/empty/…`, `/s/http_401/…`, `/s/loop/…`, …).
2. Run the real CLI (`claude` / `codex` / `grok`) with its base URL pointed at
   that server (argv built via the entrypoint’s own `harness_argv`).
3. Commit stdout as `<name>.jsonl`. Optional companions: `.stderr.txt`,
   `.last_message.txt`, `.dump.txt` (grok export only), and `.meta.json`.

CLI versions come from the **image pin**, recorded in each sidecar’s
`cli_version`. Re-run the rig after a pin bump if stream shapes change.

## Sidecars vs expectations

| File | Contents |
|---|---|
| `*.meta.json` | Measured facts only: argv, exit code, byte counts, harness, condition |
| `CAPTURES` in `test_harness_captures.py` | **Intended** verdict (human review) |

Expectations never live in a sidecar, so a wrong predicate cannot “correct”
itself by rewriting metadata. Sidecar byte counts are rechecked against the
committed files on every test run.

Four older claude streams predate the rig and have **no** sidecar; they are
asserted only by `test_entrypoint_fault.py`. The rig-covered claude captures
(`claude_healthy`, `claude_refusal`, the resume pair) are at **2.1.229**
(2026-08-13 bump — usage gained `output_tokens_details.thinking_tokens`,
absorbed into the v1 reasoning slot; result-event shape otherwise
unchanged). The full codex battery is at **0.147.0** (same bump, full
16-lane recapture — ZERO structural drift from 0.146.0: identical event
sequences, usage keys, exit codes, and classifier verdicts; resume usage
re-measured CUMULATIVE despite openai/codex#35621's restored-usage replay
skip).

## Why structural activity, not token counts

On Claude Code, `usage.output_tokens` is `0` for both an empty completion and a
fifteen-turn tool-only run; `modelUsage` is non-zero even for empty (stop token).
Fault arms therefore count non-blank text / tool blocks, not tokens.

| capture | `output_tokens` | text blocks | tool blocks |
|---|---|---|---|
| `claude_empty_completion` | 0 | 0 | 0 |
| `claude_max_turns` | 0 | 0 | 15 |

## Fixture catalog

Intended DevCake exit / class is what `test_harness_captures.py` asserts (or
`test_entrypoint_fault.py` for pre-sidecar rows).

### Claude Code (pre-sidecar + conservatism)

| Name | Scenario / shape | CLI exit | Asserted class |
|---|---|---|---|
| `claude_empty_completion` | HTTP 200, empty completion | 0 | exit 15 empty |
| `claude_api_error_400` | HTTP 400 | 1 | exit 15 terminal |
| `claude_aborted_streaming` | stream aborted | nonzero | exit 15 terminal |
| `claude_max_turns` | tool every turn, `--max-turns 15` | 1 | exit 16 budget |
| `claude_healthy` | normal completion | 0 | no fault |
| `claude_refusal` | model refusal text | 0 | no fault |

### codex-cli 0.147.0

| Name | Scenario | CLI exit | Asserted class |
|---|---|---|---|
| `codex_empty` | empty completion (`-m` set) | 0 | exit 15 empty |
| `codex_empty_no_model` | empty completion (no `-m`) | 0 | exit 15 empty |
| `codex_whitespace` | whitespace-only message | 0 | exit 15 empty |
| `codex_healthy` | normal | 0 | no fault |
| `codex_tool_only` | tools, no closing prose | 0 | no fault |
| `codex_refusal` | refusal text | 0 | no fault |
| `codex_http_400` / `_429` / `_500` / `no_route` | hard HTTP errors | 1 | exit 15 terminal |
| `codex_http_401` / `_retrying` | 401 | 1 | exit 12 auth |
| `codex_truncated` / `_retrying` | mid-stream cut | 1 | exit 15 terminal |

With `-m` on a model id the backend does not advertise, codex always emits a
benign `item.completed` / `type:"error"` metadata warning before `turn.started`.
That item is **not** tool activity.

### grok-build 0.2.112

| Name | Scenario | CLI exit | Asserted class |
|---|---|---|---|
| `grok_healthy` / `refusal` / `tool_only` | success shapes | 0 | no fault |
| `grok_empty` | 200 with no content | 1 | exit 15 terminal |
| `grok_whitespace` | whitespace-only | 0 | exit 15 empty |
| `grok_http_401` | 401 | 1 | exit 12 auth |
| `grok_http_429` / `_500` / `truncated` | hard errors | 1 | exit 15 terminal |
| `grok_turn_budget` | `--max-turns 2` | 1 | exit 16 budget |
| `grok_loop_nocap` / `_cap30` | repeated identical tool | 0 | no fault → exit 11 path |
| `grok_loop_varying_cap20` | varying tools, cap 20 | 1 | exit 16 budget |
| `grok_json_blob` | duplicate `--output-format` | 2 | exit 10 crash |

`grok export` always echoes the prompt under `## User`; activity is whatever
follows that echo (see `grok_export_activity`).

## Known gaps (not reclassified as exit 15)

Some real-world failures still land as exit 11 `DEV_BAD_OUTPUT` (missing
`result.json`) with no correlation brake — the model produced text or tools but
no deliverable. Operator notes:

- Codex + models that invent tool syntax as prose: `docs/08-harness-templates.md` §8
- Grok silent non-progress halt on a repeated tool call: `docs/15-errors-and-retries.md` §2b

## Regenerating

See `scripts/harness_capture/` (`stub_backend.py`, `in_container.py`). Not run in
CI; operator-driven when pins or CLI stream shapes change.
