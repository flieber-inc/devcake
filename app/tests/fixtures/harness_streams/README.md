# Harness stream fixtures

Live `--output-format stream-json` captures from **Claude Code 2.1.219**, taken 2026-07-24 while
investigating the fleet-wide exit-11 cascade (ADR-0018). Each was produced by pointing
`ANTHROPIC_BASE_URL` at a local stub server that reproduced one backend condition, so the shapes are
real CLI output — not hand-written.

Local paths, plugin/skill/MCP listings and memory paths in the `system/init` event are replaced with
placeholders — including every `mcp__<server>__<tool>` entry of its `tools` array, rewritten to
`mcp__example-server__tool_N` with the position and count preserved. Nothing else is altered, and no
test reads the tool list.

| File | Backend condition | CLI exit | Terminal event |
|---|---|---|---|
| `claude_api_error_400.jsonl` | HTTP 400 (`context length exceeded`) | 1 | `is_error:true`, `subtype:"success"`, `terminal_reason:"api_error"`, `api_error_status:400` |
| `claude_empty_completion.jsonl` | **HTTP 200 with an empty completion — the incident** | **0** | `is_error:false`, `subtype:"success"`, `terminal_reason:"completed"`, `result:""` |
| `claude_aborted_streaming.jsonl` | connection refused, then SIGTERM during retry backoff | (killed) | `is_error:true`, `subtype:"error_during_execution"`, `terminal_reason:"aborted_streaming"` |
| `claude_max_turns.jsonl` | stub always returns a `tool_use` block, `--max-turns 15` | 1 | `is_error:true`, `subtype:"error_max_turns"`, `terminal_reason:"max_turns"` |

## Why `usage.output_tokens` is NOT a fault signal

Measured across these captures:

| capture | `usage.output_tokens` | `modelUsage.*.outputTokens` | assistant text blocks | `tool_use` blocks |
|---|---|---|---|---|
| `claude_empty_completion` (did nothing) | **0** | 2 | 0 | 0 |
| `claude_max_turns` (15 turns of real tool work) | **0** | 75 | 0 | 15 |

Top-level `usage.output_tokens` is `0` in **both** — it does not distinguish "the backend returned
nothing" from "the agent worked for fifteen turns". An earlier design keyed `empty_completion` on
`output_tokens == 0` on the assumption that it excluded tool-only runs; these fixtures disprove that.

`modelUsage` is non-zero for the empty completion too (the stop token costs tokens), so it fails in the
other direction: a real backend emitting a whitespace/EOS-only completion reports `outputTokens >= 1`.

The discriminator that actually works is **structural**: count assistant `tool_use` blocks and
non-whitespace `text` blocks. The empty completion has neither; a tool-only run has tools; a refusal has
text. `harness_fault()` keys on that, and records the token counts only as corroborating evidence in
`error_detail`.

## Synthetic fixtures

Any fixture added here that was *not* captured from a real CLI must be named `synthetic_*.jsonl` and say
so in this table. Two precedence-table rows can only be exercised synthetically: **zero-exit
`turn_budget`** (Claude Code exits 1 on max-turns, as measured above — the zero-exit arm is defensive
completeness for other harnesses) and any row for a harness we have not yet captured. `codex` and
`grok-build` have **no** real captures yet; their predicate arms stay provisional until they do.
