# Harness stream fixtures

Live captures of real CLI stdout. Most were taken against stub backends that reproduce one backend
condition each; the `live_*` batch was taken against the **real model backend that caused the
2026-07-24 production incident**. No fixture byte is hand-written or edited: when a capture came out
wrong the *condition* was changed and the run repeated.

Six batches so far — Claude Code 2.1.219 (2026-07-24, the incident investigation), three from
the ADR-0018 gate-1 capture session on 2026-07-25 (grok-build 0.2.112, codex 0.144.4, and two
Claude Code 2.1.210 conservatism baselines), three more grok captures taken later the same day
when the gate-1 batch turned out to have measured something nobody had asked about — grok halting a
run *itself* at 16 turns ([below](#grok-halts-a-repeated-tool-call-run-itself-at-16-turns--and-that-is-not-a-turn-cap))
— and the **17 `live_*` real-backend captures**
([below](#the-live-captures--the-2026-07-24-incident-reproduced-against-the-real-backend)), which are
the only ones here that are *not reproducible* and the only ones that answer what the incident
actually was.
Every harness now has real captures; no predicate arm rests on a synthetic stream any more.

**CLI versions are read from inside the baked image by the rig itself** and recorded in every
sidecar's `cli_version` — the host's CLIs drift from the image pins, so a capture taken at the wrong
version silently stops describing what production runs. The codex and Claude Code stub captures
below were taken at **codex-cli 0.144.4** and **2.1.210 (Claude Code)**, the pins in
`devcake/dev-codex:latest` and `devcake/dev-claude-code:latest` on that date.

Companion files, where a capture produced them: `<name>.dump.txt` (what the entrypoint passes to the
predicate as `dump`), `<name>.stderr.txt` (the channel the pre-ADR-0018 classifier read), and
`<name>.meta.json` — a sidecar of **measured facts only** written by
`scripts/harness_capture/in_container.py`. Expected reasons deliberately live in the pytest tables,
never in a sidecar, so an expectation cannot be "corrected" without review.

`.dump.txt` is promoted only for grok, where it is the output of a **separate** `grok export` call and
cannot be recovered from stdout. For codex and claude the dump is a pure function of the committed
`.jsonl` (`codex_text_dump` / `claude_text_dump`), so it is not duplicated here.

Every sidecarred capture is asserted by **`app/tests/test_harness_captures.py`**, which discovers rows
from the `*.meta.json` files on disk — a capture added here without a judgement in that file's
`CAPTURES` table fails collection rather than going unasserted. That table is also where the
**intended** verdict for each row lives, and where the reconciled matrix at the bottom of this README
comes from. Read [The assertion table](#the-assertion-table) last: it is the authoritative `intended`
column, and it supersedes the per-harness `intended` cells above on the three rows named there.

---

## Claude Code 2.1.219 — captured 2026-07-24

Produced by pointing `ANTHROPIC_BASE_URL` at a local stub, `--output-format stream-json`.

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

### Why `usage.output_tokens` is NOT a fault signal

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

---

## grok-build 0.2.112 (`9bbd559437`) — captured 2026-07-25

Produced inside `devcake/dev-grok-build:latest` by `scripts/harness_capture/in_container.py`, so argv
came from the entrypoint's own `harness_argv`:
`grok -p <prompt> --output-format streaming-json --always-approve --model stub-model [extra]`.

Backend: `scripts/harness_capture/stub_backend.py`, selected per capture by path prefix
(`http://devcake-capture-stub:8080/s/<scenario>/v1`). The stub URLs are kept in the sidecars on
purpose — they are the provenance that proves a capture hit the stub and not a real API. Nothing
needed scrubbing: grok's streaming-json carries no paths, no tool listing and no environment.

**Wire protocol.** `GROK_MODELS_BASE_URL` + `XAI_API_KEY` (no `grok login`). The eleven gate-1
captures were taken with `$GROK_HOME/config.toml` holding only
`[model.stub-model] api_backend = "messages"`, because grok's default `chat_completions` lane
**rejected the stub**: it requires `created` on every streamed chunk and aborted with
`Internal error: "serialization error: missing field 'created' at line 1 column 641"`. That was a
stub gap and it has since been closed — the streamed chunks carry `created` and `model` like real
OpenAI ones, so the default lane works and needs no `config.toml` at all. **`grok_loop_varying_cap20`
is the first grok capture taken over the default `chat_completions` lane**; the others are
`messages`. The lane does not change grok's stdout shape (measured both ways for the 16-turn stop
below), which is what these fixtures are about. A sidecar does not record the lane — only the
preflight URL — so this paragraph is the record of which capture used which.

| File | Backend condition | CLI exit | grok stdout (terminal event) | `dump` (`grok export`) |
|---|---|---|---|---|
| `grok_healthy.jsonl` | ordinary completion | 0 | `text` deltas + `end` `stopReason:"EndTurn"` | 601 B — prompt **and** answer |
| `grok_empty.jsonl` | **HTTP 200 with zero content blocks — the incident shape** | 1 | `{"type":"error"}` — `empty response from model (no_visible_content)`, after 5 min 45 s of retries | empty |
| `grok_whitespace.jsonl` | HTTP 200, whitespace-only text | **0** | `{"type":"text","data":"  \n "}` + `end` `EndTurn` | **100 B — the prompt only** |
| `grok_refusal.jsonl` | a genuine refusal | 0 | `text` + `end` `EndTurn` | 139 B — prompt + refusal |
| `grok_tool_only.jsonl` | a `tool_use` block on every turn, no `--max-turns` | **0** | **only** `end` `EndTurn`, `num_turns:16` | 916 B — prompt + a `## Tools` list |
| `grok_http_401.jsonl` | HTTP 401 on every inference POST | 1 | `{"type":"error"}` (no `sessionId`) | empty |
| `grok_http_429.jsonl` | HTTP 429 | 1 | `{"type":"error"}` | empty |
| `grok_http_500.jsonl` | HTTP 500 (retried ~5 min first) | 1 | `{"type":"error"}` | empty |
| `grok_truncated.jsonl` | stream cut mid-body (retried ~5 min first) | 1 | `{"type":"error"}` — `reqwest error stream: Transport error` | empty |
| `grok_turn_budget.jsonl` | `tool_use` every turn, `--max-turns 2` | 1 | `{"type":"max_turns_reached"}` then `end` `stopReason:"Cancelled"` | 482 B |
| `grok_json_blob.jsonl` | `--extra '--output-format json'` | **2** | **empty** — clap refuses a duplicate `--output-format` | empty |
| `grok_loop_nocap.jsonl` | the `loop` lane — the **same** `tool_use` every turn, no `--max-turns` | **0** | **only** `end` `EndTurn`, `num_turns:16` | 916 B — 16 identical tool lines |
| `grok_loop_cap30.jsonl` | the same lane, **`--max-turns 30`** | **0** | byte-identical shape — `end` `EndTurn`, `num_turns:16` | 916 B |
| `grok_loop_varying_cap20.jsonl` | `loop_varying` — a **different** `tool_use` every turn, `--max-turns 20` | 1 | `{"type":"max_turns_reached"}` then `end` `Cancelled`, **`num_turns:20`** | 1140 B — 20 distinct tool lines |

### What the shipped predicate does with them

Recomputed from the promoted bytes; identical to every sidecar's `observed_reason` /
`devcake_exit_now` / `devcake_exit_before_adr0018`. `11` on a zero-exit row simply means the capture
prompt wrote no `result.json` — it is not a fault verdict.

| capture | intended | observed | exit now | exit pre-0018 | |
|---|---|---|---|---|---|
| `grok_healthy` | none | none | 11 | 11 | |
| `grok_empty` | `empty_completion` | `empty_completion` | 15 | 10 | right answer, wrong evidence — the arm fired on a missing session id, not on `no_visible_content` |
| `grok_whitespace` | `empty_completion` | **none** | 11 | 11 | **mismatch** — the dump (the prompt echo) blocks the arm |
| `grok_refusal` | none | none | 11 | 11 | conservative, as required |
| `grok_tool_only` | none | none | 11 | 11 | conservative — but only because of the dump |
| `grok_http_401` | `terminal_error` | `empty_completion` | **15** | **12** | **mismatch + regression**, see below |
| `grok_http_429` | `terminal_error` | `empty_completion` | 15 | 10 | reason wrong, exit unchanged in effect |
| `grok_http_500` | `terminal_error` | `empty_completion` | 15 | 10 | idem |
| `grok_truncated` | `no_terminal_event` | `empty_completion` | 15 | 10 | grok always emits a terminal `error`, so this arm is unreachable for grok |
| `grok_turn_budget` | `turn_budget` | **none** | 10 | 10 | **mismatch** — `grok_run_fault` has no turn-budget arm |
| `grok_json_blob` | — | `no_terminal_event` | 15 | 10 | |

The three later captures are graded against the **shipped** predicate (they postdate the fix commit),
and none of them moved it: `grok_loop_nocap` and `grok_loop_cap30` are `none` / 11 / 11 like every
other clean-exit row, and `grok_loop_varying_cap20` is `turn_budget` / 16 / 10. What they measure is
a property of the CLI, not of the predicate.

`grok_run_fault` has no reachable `terminal_error` arm: `GROK_FAULT_STOP_REASONS` is empty by design
and the predicate never inspects `{"type":"error"}`, so every hard backend failure lands in
`empty_completion`.

`grok_http_401` is the one row where ADR-0018 **changes** an outcome, and it changes it for the
worse. grok's 401 stderr is

```
Error: Internal error: "Unauthorized (401) from <base>/v1/messages: invalid_request_error: stub injected HTTP 401\n\n  Model:     stub-model\n  Auth:      ApiKey\n  Version:   0.2.112\n  Available: stub-model"
```

`unauthorized` is a **generic** `HARNESS_AUTH_MARKER`, so pre-ADR-0018 this exited **12 `DEV_AUTH`**
and latched the per-Dev-Type auth breaker. Neither distinctive marker (`not signed in`,
`grok login`) appears, and `api_error_status` is never computed for grok, so
`auth_evidence_is_distinctive` is false and the generic arm now ranks *below* the predicate —
`classify_nonzero_exit` returns **15 `DEV_HARNESS_FAULT`**, which is correlation-eligible.

### A backend fault costs grok ~5¾ minutes before it gives up

`grok_empty`, `grok_http_500` and `grok_truncated` all ran **344.7 s / 344.9 s / 344.9 s** —
~14 POSTs with a backoff ramp that plateaus around 30 s, then a single terminal `error` event. Only
`grok_http_401` (0.4 s) and `grok_http_429` (3.0 s) are treated as non-retryable. `duration_ms` in
every sidecar is measured, not estimated. An outage therefore burns roughly six minutes per attempt
per container before DevCake learns anything at all.

Unlike Claude Code, grok does **not** report a strictly empty completion as a clean success: it names
the condition (`no_visible_content`) and exits 1. The near-empty case still slips through — see
`grok_whitespace`, which grok accepts and exits 0 on.

### `grok export` echoes the prompt, so `dump` is never empty when a session id exists

This is the finding that decides whether the grok arm of `harness_fault` works at all.
`grok_run_fault`'s `empty_completion` arm requires no text **and** no thoughts **and** no `dump`, and
for grok `dump` is the output of `grok export <sessionId>` — a **Markdown transcript that begins with
the user prompt**. `grok_whitespace.dump.txt` is the whole of what a run that produced nothing useful
returns:

```
## User

Reply with exactly the word: ACKNOWLEDGED

Do not use any tools. Do not explain. One word.
```

Exporting the session of a run that produced *literally nothing* — the `grok_http_429` session, zero
assistant messages — returns those same bytes and exits 0. So the arm cannot fire on a run whose
session id is known, whatever the backend did.

It fires on `grok_empty` and the HTTP captures only because grok's `{"type":"error"}` event carries
**no `sessionId`**, so neither the capture rig nor `main()` can name a session to export. A 401 on a
**resumed** session was captured too and is byte-identical — an existing session on disk still does
not put a `sessionId` on the error event. The arm therefore fires exactly when the harness *crashed*,
and never in the 200-with-nothing case it was written for.

### grok's streaming-json is silent about tool work

`grok_tool_only.jsonl` is 16 real tool executions and is **one line long** — the `end` event. No
`text`, no `thought`, no tool events at all (docs/08 §1 is still right about that at 0.2.112). Its
`grok export`, by contrast, lists every one under a `## Tools` heading. The dump is the *only*
evidence a grok run did anything, which is why it cannot simply be dropped from the predicate.

It was captured against a stub whose `tool_only` lane answered **every** turn with a tool call; grok
ended the run itself at 16 turns with `stopReason:"EndTurn"` and exit **0**, which is why it is a
clean tool-only capture and not a turn-cap one.

**That lane has since changed, so `grok_tool_only` is no longer reproducible from `tool_only`.**
`tool_only` now answers the first turn with a tool call and the turn *after a tool result* with
nothing (`tool_result_present`), because codex — which has no turn cap — otherwise looped until the
capture timeout and produced a killed process instead of the conservatism evidence the scenario
exists for. grok on that lane does **not** end its turn at the content-free response: it reports
`no_visible_content` and exits 1 (see `grok_empty`). **To reproduce `grok_tool_only` today, use the
`loop` lane** — `grok_loop_nocap` is exactly that run, and its stream differs only in the session and
request ids. The `tool_only` shape is right for codex (`codex_tool_only`: one tool call, a turn that
ends with no final text, exit 0, no fault — the arm that must never fire) and is left alone.

### Turn exhaustion

`grok_turn_budget.jsonl` was driven by `--max-turns 2` and answers what grok emits: **both** a
dedicated `{"type":"max_turns_reached"}` event **and** `end` with `stopReason:"Cancelled"`,
`num_turns:2`, plus `Error: max turns reached` on stderr, exit **1**. `GrokCoalescer` dropped both
event types when this was captured (it returned `None` for anything that was not `text` or `end`), so
the operator's live transcript said nothing about the stop; it now renders `max_turns_reached` and
`error`, and the `error` arm flushes the text buffer the way `end` does.

`grok_loop_varying_cap20` is the same shape at `num_turns:20` — the evidence that the cap is not
pinned to some lower ceiling, and the reason the "raise `--max-turns`" remedy in
`docs/15-errors-and-retries.md` §2a is real advice for grok.

### grok halts a repeated-tool-call run itself at 16 turns — and that is NOT a turn cap

The gate-1 batch left a hedge in `docs/08` ("whether that 16 is a default cap is unverified"). It is
answered now, and the answer is *neither* of the two things it could have been.

**Measured.** Six runs of the `loop` lane — the byte-identical `tool_use` every turn — stopped at
exactly `num_turns: 16` / `modelCalls: 16`:

| run | lane | `--max-turns` | result |
|---|---|---|---|
| `grok_tool_only` (committed) | messages | unset | `EndTurn`, exit 0, **16** |
| `grok_loop_nocap` (committed) | messages | unset | `EndTurn`, exit 0, **16** |
| `grok_loop_cap30` (committed) | messages | **30** | `EndTurn`, exit 0, **16** |
| `probe_cap17` (campaign note) | chat_completions | **17** | `EndTurn`, exit 0, **16** |
| `grok_loop_chatlane` (campaign note) | chat_completions | unset | `EndTurn`, exit 0, **16** |
| `probe_cap16` (campaign note) | chat_completions | **16** | `max_turns_reached` + `Cancelled`, exit 1, **16** |

Reading only those, "grok has a hard 16-turn ceiling and `--max-turns` can only lower it" is the
obvious conclusion, and it is **wrong**. The control that breaks it is `grok_loop_varying_cap20`:
the same lane, the same CLI, the same protocol, with **one** difference — the tool call's arguments
change every turn (`stub_backend.py::vary`). That run sails past 16 and is stopped by `--max-turns
20` at `num_turns: 20`, loudly. Without a cap at all it does not stop: **~2,900 model calls in 300 s,
still going when the rig killed it** (campaign note — a killed grok run writes zero bytes of stdout,
so there is nothing to commit).

So `--max-turns 30` did not "fail to raise a ceiling" on `grok_loop_cap30`. **The flag was accepted
and armed; the halt simply fired first, at 16, and a cap of 30 was never reached** — exactly as a cap
of 20 was never reached on the repeating lane and *was* reached on the varying one. Independently of
these captures, the founder inspected the CLI on 2026-07-25 and found no default to raise in the
first place: `--max-turns <N>` is documented with **no default**, `config.toml` has no `max_turns`
key, and the binary's strings carry the cap path only for an explicit limit (`max turns reached`,
`max_turns_reached`, `max_turns must be greater than 0`) with no `DEFAULT_MAX_TURNS` equivalent —
plus live runs stopping at exactly 5, 30 and 50. That is CLI inspection, not a capture, and is
recorded here as corroboration only; the committed proof that the flag works above 16 is
`grok_loop_varying_cap20`.

**The mechanism is grok's own stall detector, and it is visible only in the stub's journal.** Every
tool result grok fed back to the model on the repeating runs carried an injected reminder:

```
exit: 0
working

<system-reminder>
You appear to be running empty commands to stay active while waiting for background work. End your
turn — you will be woken automatically when there is something to do.
</system-reminder>
```

So the run does not hit a limit — grok talks the model into ending its turn, and after 16 such turns
the run ends. Nothing about this appears in grok's **output** stream: `stopReason:"EndTurn"`, exit 0,
one `end` event, no `max_turns_reached`. It is byte-identical in shape to a clean success.

**Why an operator cares.** A grok Dev that gets stuck repeating one command stops at 16 turns having
completed nothing, writes no `result.json`, and is reported **exit 11 `DEV_BAD_OUTPUT`** — before and
after ADR-0018, and correctly so (16 tool executions really happened; the fault predicate is right
not to fire). There is no diagnostic anywhere saying the run was truncated. `docs/15` §2b carries the
operator-facing version of this.

**What is NOT established.** Whether the constant is exactly 16 for every prompt and tool, what
counts as "the same command" to the detector, and whether a real model — which would read the
reminder and act on it — behaves the way the stub's fixed answer forces. The stub cannot read a
system-reminder, so these captures measure grok's *harness* behaviour under a pathological model, not
a model's behaviour.

**Campaign notes above are uncommitted runs** taken with the same rig on 2026-07-25; the `probe_*`
ones are reproducible from the committed stub (`/s/loop/v1` with `--extra '--max-turns N'`), and the
~2,900-call one needs `/s/loop_varying/v1` with no cap.

### `usage` on the `end` event (docs/08 §2 is out of date)

At 0.2.112 the terminal `end` event carries `usage`, `num_turns` and `modelUsage` inline — see any
capture above. docs/08 records "Neither contains usage/cost fields in this version" from 0.2.93.
`total_cost_usd` is absent here only because the stub reports no cost.

`signals.json` **still exists** at 0.2.112 under
`$GROK_HOME/sessions/{urlencoded-cwd}/{session-id}/`, carrying `contextTokensUsed`,
`contextWindowTokens`, `modelsUsed`, `turnCount` and much more, so grok token extraction is not
broken. It is written only for sessions that end cleanly: it is present for `grok_healthy`,
`grok_whitespace`, `grok_refusal` and `grok_tool_only`, and **absent** for `grok_turn_budget`,
`grok_http_429` and every other failed run — which is why those reported `extraction_method:
"unavailable"` at capture time. Since then the `end` event is the primary source and `signals.json`
the fallback (docs/08 §5), so a failed run that still emits `end` (`grok_turn_budget`) now reports
`end_event`, and only the runs with no `end` event at all (`grok_empty`, `grok_http_401/429/500`,
`grok_truncated`, `grok_json_blob`) fall through to `unavailable`. Note also that
`signals.turnCount` is **1** for the run whose `end` event reported `num_turns:16`.

### The `--output-format json` blob branch is unreachable

`harness_argv` always passes `--output-format streaming-json`, and grok 0.2.112 exits **2** with
`error: the argument '--output-format <OUTPUT_FORMAT>' cannot be used multiple times` when
`$DEVCAKE_EXTRA_ARGS` adds a second one — `grok_json_blob.jsonl` is empty and its
`.stderr.txt` holds the clap usage error. `--json-schema` does not override the explicit flag either
(it only adds `structuredOutput`/`structuredOutputError` to the `end` event). Run directly, the blob
is `{"text","stopReason","sessionId","requestId","usage","num_turns","modelUsage"}`, pretty-printed
over multiple lines — so `grok_stream_parse` correctly returns `None` for it and the blob fallback
would parse it — but no `EXTRA_ARGS` path reaches that branch on this CLI version.

---

## codex-cli 0.144.4 — captured 2026-07-25

Produced inside `devcake/dev-codex:latest` by `scripts/harness_capture/in_container.py`, so argv came
from the entrypoint's own `harness_argv`:
`codex exec <prompt> --json -o <out>/last_message.txt --skip-git-repo-check
--dangerously-bypass-approvals-and-sandbox -m stub-model [-c overrides]`.

Backend: `scripts/harness_capture/stub_backend.py` over the **Responses** wire
(`-c model_providers.stub.wire_api=responses`; `chat` was removed in 0.144.x), selected per capture by
path prefix. The stub URLs are kept verbatim in the sidecars and inside several streams — they are the
provenance that proves a capture hit the stub and not a real API. Nothing needed scrubbing: codex's
`--json` stream carries no paths, no tool listing and no environment.

Two rig defects had to be fixed before any of this was capturable, and both are in the stub, not in
DevCake:

* the `response.completed` usage payload omitted **`total_tokens`**, which codex deserializes into a
  typed `ResponseCompleted`. Every scenario — including `healthy` — aborted with
  `stream disconnected before completion: failed to parse ResponseCompleted: missing field 'total_tokens'`
  and was classified `terminal_error`. A stub that cannot answer `healthy` cannot measure conservatism.
* `tool_only` answered **every** turn with a tool call, which is `loop`'s job. codex has no turn cap,
  so it looped: **5,535 requests in ~7 minutes** before the capture was killed. `tool_only` now
  answers a tool call only while the request carries no tool result yet (`tool_result_present()`) —
  stateless, derived from the request exactly as a real model's next turn is, so parallel captures
  stay independent. `loop` is untouched. **`grok_tool_only.jsonl` above predates this change** and its
  note about "a stub whose `tool_only` lane answered every turn" describes the old lane.

Because the shared stub instance was in use by the grok session, these were taken against a second
instance of the same file on the same network, so the recorded base URLs read
`http://devcake-capture-stub-codex:8080/s/<scenario>/v1`.

### Why the model is pinned, and what pinning drags in

`-m stub-model` is not decoration. With **no** `-m`, codex 0.144.4 sends **no `tools` key at all**:
the surface moves into an `additional_tools` *input item* advertising a `custom` JavaScript `exec`
orchestrator plus `wait` and `request_user_input`. The stub reads `body["tools"]`, finds nothing, and
answers with no items — so `tool_only` silently degenerates into `empty` and the conservatism arm is
never exercised. With `-m` set, codex sends the classic ten-function surface
(`exec_command`, `write_stdin`, `update_plan`, …) that docs/08 §8's bisect measured. Pinning is also
the production shape for a local backend, which is the deployment ADR-0018 exists for.

The cost of pinning is that codex emits a benign
`{"type":"item.completed","item":{"type":"error","message":"Model metadata for \`stub-model\` not
found. Defaulting to fallback metadata…"}}` **before `turn.started` on every run**, exactly as it does
against a real vLLM. It is in every capture below except `codex_empty_no_model`, and it is not
inert — see the first finding.

| File | Backend condition | CLI exit | Terminal event / stream shape |
|---|---|---|---|
| `codex_healthy.jsonl` | ordinary completion | 0 | `agent_message` + `turn.completed`; `last_message.txt` 486 B |
| `codex_empty.jsonl` | **HTTP 200 with zero output items — the incident shape** | **0** | `turn.completed`, `output_tokens:0`, no items, empty `-o` file |
| `codex_empty_no_model.jsonl` | same, but **no `-m`** so no metadata item | **0** | `turn.completed` and nothing else — 3 lines |
| `codex_whitespace.jsonl` | HTTP 200, one whitespace-only `output_text` | **0** | `agent_message` `"  \n "` + `turn.completed`; `-o` file is 4 bytes of whitespace |
| `codex_refusal.jsonl` | a genuine refusal | 0 | `agent_message` + `turn.completed` |
| `codex_tool_only.jsonl` | one `function_call`, then an empty final turn | 0 | `command_execution` item (`/bin/bash -lc stub`, exit 127) + `turn.completed`, **no `agent_message`** |
| `codex_http_400.jsonl` | HTTP 400 JSON error body | 1 | `error` + **`turn.failed`**; message is the raw JSON body |
| `codex_http_401.jsonl` | HTTP 401 | 1 | `error` + `turn.failed`: `unexpected status 401 Unauthorized: …` |
| `codex_http_429.jsonl` | HTTP 429 | 1 | `error` + `turn.failed`: `exceeded retry limit, last status: 429 Too Many Requests` |
| `codex_http_500.jsonl` | HTTP 500 | 1 | `error` + `turn.failed`: `We're currently experiencing high demand…` |
| `codex_truncated.jsonl` | SSE opened, one event, connection closed mid-stream | 1 | `error` + `turn.failed`: `stream disconnected before completion: Transport error` |
| `codex_no_route.jsonl` | 404 on `/v1/responses` (a backend lacking the route) | 1 | `error` + `turn.failed`: `unexpected status 404 Not Found` |
| `codex_http_401_retrying.jsonl` | 401 with **default** retries — the production shape | 1 | five `Reconnecting... N/5 (…)` `error` events, then the bare error + `turn.failed`; 6.5 s |
| `codex_truncated_retrying.jsonl` | truncation with **default** retries | 1 | idem, 6.5 s |

All twelve `-c ... _max_retries=0` rows complete in **under 0.6 s**; the two default-retry rows take
**6.5 s**. codex therefore fails fast — unlike grok's ~5¾ minutes — and it retries a **401** five
times before giving up.

### What the shipped predicate does with them

Recomputed from the promoted bytes; identical to every sidecar's `observed_reason` /
`devcake_exit_now` / `devcake_exit_before_adr0018`. `11` on a zero-exit row means only that the
capture prompt wrote no `result.json` — it is not a fault verdict.

| capture | intended | observed | exit now | exit pre-0018 | |
|---|---|---|---|---|---|
| `codex_healthy` | none | none | 11 | 11 | conservatism baseline holds |
| `codex_empty` | `empty_completion` | **none** | 11 | 11 | **mismatch** — the metadata item is counted as tool activity |
| `codex_empty_no_model` | `empty_completion` | `empty_completion` | 15 | 11 | the counterfactual: same backend, no metadata item, arm fires |
| `codex_whitespace` | `empty_completion` | **none** | 11 | 11 | **mismatch** — `messages` never checks for blankness |
| `codex_refusal` | none | none | 11 | 11 | conservative, as required |
| `codex_tool_only` | none | none | 11 | 11 | conservative — tool work with no final text is not a fault |
| `codex_http_400` | `terminal_error` | `terminal_error` | 15 | 10 | |
| `codex_http_401` | `terminal_error` | `terminal_error` | **15** | 10 | intended 12 `DEV_AUTH`; see below |
| `codex_http_429` | `terminal_error` | `terminal_error` | 15 | 10 | |
| `codex_http_500` | `terminal_error` | `terminal_error` | 15 | 10 | |
| `codex_no_route` | `terminal_error` | `terminal_error` | 15 | 10 | |
| `codex_truncated` | `no_terminal_event` | `terminal_error` | 15 | 10 | **mismatch**, same exit — see below |
| `codex_http_401_retrying` | `terminal_error` | `terminal_error` | 15 | 10 | retry noise does not change the verdict |
| `codex_truncated_retrying` | `no_terminal_event` | `terminal_error` | 15 | 10 | idem |

### `empty_completion` is unreachable for codex on any backend it does not recognise

This is the finding that matters, because it is the incident shape. `codex_run_fault` counts every
`item.completed` that is not an `agent_message` as tool activity:

```python
elif item:
    items += 1          # command_execution / file_change / patch_apply / mcp_tool_call
```

The benign `Model metadata … not found` item is an `item.completed` with `item_type: "error"`, so it
scores as a tool call, `items == 1`, and the arm cannot fire. `codex_empty` and `codex_empty_no_model`
are the same backend condition and the same CLI, differing only in whether `-m` was passed:

| capture | `-m` | metadata item | `agent_messages` | `tool_items` | observed |
|---|---|---|---|---|---|
| `codex_empty` | `stub-model` | yes | 0 | **1** | none |
| `codex_empty_no_model` | — | no | 0 | 0 | `empty_completion` |

Every local backend serves a model id codex has no metadata for, so in the deployment ADR-0018 was
written for, a 200-with-nothing reports **exit 11 `DEV_BAD_OUTPUT`** — precisely the laundering the
ADR set out to stop. The defect was already known from the codex parsing code; these two captures
measure it, and pin down that it is not merely cosmetic.

### Whitespace-only completions are not classified either

ADR-0018 §1 says the structural discriminator "also classifies a whitespace-only completion
correctly". That is true of `_claude_activity`, which requires `str(block.get("text")).strip()`. The
codex arm has no such test — `messages += 1` fires on an `agent_message` of any content — so
`codex_whitespace` (whose `-o` file is four whitespace bytes, and whose `codex_text_dump` is empty)
observes no fault. Two arms, two different notions of "text".

### `turn.failed` measured — and the arm written for it is dead

The ADR names `turn.failed` as an unverified gap. It exists, on **all eight** failure captures, shaped
`{"type":"turn.failed","error":{"message":"…"}}`. But the predicate's guard for it

```python
elif kind.startswith("turn.") and kind not in ("turn.started", "turn.completed"):
    error_msg = error_msg or f"unrecognized terminal event {kind!r}"
```

never contributes: codex always emits a plain `{"type":"error","message":…}` **immediately before**
`turn.failed`, so `error_msg` is already set and the `or` short-circuits. The arm is correct and
unreachable. Nothing in the *predicate* reads `turn.failed`'s own `error.message` — the live output
relay now does (`render_codex`), because a terminal event that decides a run's fate must not be
invisible in the operator's transcript.

By the same token **`no_terminal_event` is unreachable for codex**: every failure path emits an
`error` event, so a stream with no `turn.completed` always classifies as `terminal_error`.
`codex_truncated` and `codex_truncated_retrying` are the mismatches this produces, and they are
benign — both reasons map to exit 15, so only the wording of `error_detail` differs.

### A 401 reaches exit 15, not 12 — and 15 is correlation-eligible

`codex_http_401` carries `401 Unauthorized` **in band**, inside the `error` and `turn.failed` events.
It reaches neither auth arm:

* `api_error_status` is computed **only for claude** (`run_once` and `main()` both call
  `claude_result_event`), so `auth_evidence_is_distinctive(err, None)` sees no status;
* stderr is **39 bytes** — `Reading additional input from stdin...` — on every codex failure, so
  neither the distinctive markers nor the generic ones can match.

`classify_nonzero_exit` therefore returns **15 `DEV_HARNESS_FAULT`**. That is worse than a wrong
label: exit 15 is correlation-eligible, so an expired key rolled out to a whole Dev Type looks
exactly like a shared backend outage — `backend_correlated` would **excuse** the attempts and retry
against a credential that will never work, instead of latching the auth breaker that tells the
operator to refresh it. (Pre-ADR-0018 it exited 10, so this is a change in classification, not in
retry accounting.) The same reasoning applies to grok's 401 above, from the other direction.

### stderr is empty on every codex failure, and codex says so out loud

Confirming ADR-0018's central premise for a second harness: stderr is **39 bytes** on every failure
capture, and it is not even about the failure. The one genuinely diagnostic line codex writes there —
`Warning: no last agent message; wrote empty content to <path>`, present in `codex_empty` and
`codex_tool_only`, 134 bytes total — is exactly the "the model returned nothing" signal, and nothing
reads it.

The rig launches the harness the same way `main()` does (`Popen(..., stdout=PIPE, stderr=PIPE)` with
stdin inherited), so `Reading additional input from stdin...` is production's stderr too, not an
artifact.

### codex has no turn cap, so `turn_budget` cannot fire for it

There is no `--max-turns` equivalent in `codex exec --help` at 0.144.4 and no config key for one. The
runaway measured above — 5,535 turns, still going when killed — is what an always-tool-calling
backend produces. `FAULT_TURN_BUDGET` is measured only for claude (`claude_max_turns.jsonl`); grok
emits the events but has no predicate arm (see above); codex has neither, and its failure mode is an
unbounded run terminated by DevCake's own timeout, arriving as a signal kill.

**Rig defect found while measuring it:** `in_container.py::run_once` hangs forever on the timeout
path. `proc.kill()` kills codex, but `exec_command` leaves a persistent `/bin/bash` child holding the
inherited stdout pipe, so the following `proc.communicate()` never sees EOF. The runaway capture
produced **no files at all** and had to be killed at the container level.

**Fixed** in the same commit as the assertion table: the harness is launched with
`start_new_session=True` and the timeout path SIGKILLs the whole process group
(`os.killpg(os.getpgid(proc.pid), …)`), which closes every inherited pipe end at once, followed by a
bounded second `communicate()` and — if something escaped the group — a fallback that keeps
`TimeoutExpired`'s partial reads, closes the pipes and stops waiting. Measured on the same shape
(a child inheriting stdout, parent SIGKILLed): `proc.kill()` alone was still blocked after the grace
window; the group kill drained in 2.0 s with the partial stdout intact.

---

## Claude Code 2.1.210 — captured 2026-07-25 (the conservatism baseline)

The four 2.1.219 captures above are all *faults*. These two are not, and they close the gap: the
conservatism arm of `claude_run_fault` — the arm that must never fire — had no real capture for the
harness that ships to two of the three Dev Types.

Produced inside `devcake/dev-claude-code:latest`, argv from `harness_argv`
(`claude -p <prompt> --output-format stream-json --verbose --dangerously-skip-permissions`), with
`ANTHROPIC_BASE_URL=http://devcake-capture-stub-codex:8080/s/<scenario>` and a dummy token.

No scrubbing was needed, unlike the 2.1.219 batch: the run is inside the image, so the `system/init`
event's `cwd`, `memory_paths` and listings are all container paths from the baked image
(`/tmp/capture/slot0/repo`, `/home/dev/.claude/…`), with `mcp_servers: []` and `plugins: []`.

| File | Backend condition | CLI exit | Terminal event | observed |
|---|---|---|---|---|
| `claude_healthy.jsonl` | ordinary completion | 0 | `is_error:false`, `subtype:"success"`, `terminal_reason:"completed"`, `result` 486 B | none |
| `claude_refusal.jsonl` | **a genuine refusal** | 0 | `is_error:false`, `subtype:"success"`, `terminal_reason:"completed"`, `result:"I can't help with that."` | none |

Both are `num_turns:1`, `api_error_status:null`, `output_tokens:24`, and **stderr is zero bytes** —
claude writes nothing there at all, where codex writes 39 unrelated bytes. Neither trips any arm, so
the refusal that ADR-0018 §1 is built around not misclassifying is now measured rather than asserted.

Note that a refusal is indistinguishable from a healthy run at the protocol level — same `subtype`,
same `terminal_reason`, same shape, only shorter text. That is exactly why `empty_completion` has to
be structural: there is no flag to key on.

---

## The live captures — the 2026-07-24 incident, reproduced against the real backend

Every capture above is a *stub* reproducing one condition. These seventeen are the other half: the
real model backend the incident happened on, driven with the founder's actual incident configuration.
They exist to answer one question, and they answer it.

> **Does the production incident reproduce against the real backend, and does ADR-0018 now classify
> it as exit 15?**
>
> **It reproduces exactly, on the first mission-shaped attempt, and ADR-0018 reports 11 — the same
> code it reported before the ADR.** Not a regression and not a laundering: the run is not a harness
> fault under any honest reading of the stream, because the model *did* answer. It answered with
> prose containing invented tool syntax, executed nothing, wrote no `result.json`, and exited 0.

### The rig

Taken on 2026-07-26 (UTC) with `scripts/harness_capture/in_container.py` inside the baked images, on
the `devcake_runtime` network. Backend: vLLM serving **`DeepSeek-V4-Flash-DSpark-Abliterated`**
(`max_model_len` 300000) — `:8000` raw, `:8765` the request-rewriting proxy that repositions the
system prompt where codex expects it. **`:8765` is the incident path.**

codex was given the founder's verbatim incident block, with the model id arriving as `-m` (as it does
in production from `DEVCAKE_MODEL`), and **no `request_max_retries` override** — this is production's
retry shape, unlike most of the stub captures:

```
-m DeepSeek-V4-Flash-DSpark-Abliterated
-c model_provider=vllm
-c model_providers.vllm.name=vLLM
-c model_providers.vllm.base_url=http://vllm-backend:8765/v1
-c model_providers.vllm.env_key=CODEX_API_KEY
-c model_providers.vllm.wire_api=responses
-c model_context_window=300000
-c model_auto_compact_token_limit=240000
```

**Timeout bounds, chosen deliberately** because codex 0.144.4 has no turn cap and will hammer a
backend that keeps it looping: **180 s** for trivial prompts, **300 s** for every mission-shaped and
concurrent codex run, **420 s** for claude and grok (grok's measured retry ramp is ~345 s, so 420 s
lets a *failing* grok finish its ramp and emit its terminal `error` event instead of being killed
mid-backoff). Nothing timed out; every sidecar records `timed_out: false`.

**A dummy API key worked for all three harnesses** (`CODEX_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`XAI_API_KEY` all set to `dummy-key`). The backend does not check credentials, which removes an
expired or mis-rolled key from the list of things the incident could have been.

**`result.json` delivery is a filesystem fact here, not an inference.** The rig was run with
`--workspace-root /work` bind-mounted to the host, so after each run the workspace was inspected
directly for `out/result.json` and for the requested edit to `src/greet.py`. (That is a fact about
the *run*, not about the committed bytes, so it lives in this table rather than in a test.) Note the
consequence for the sidecars: these captures' workspace paths read `/work/slot0/...` where the stub
batch's read `/tmp/capture/slot0/...`.

### What each capture measured

| capture | harness · port | prompt | CLI exit | tool calls | `result.json` | verdict | exit now | exit pre-0018 |
|---|---|---|---|---|---|---|---|---|
| `live_codex_proxy_trivial` | codex · **:8765** | trivial | 0 | — (none asked) | n/a | none | 11 | 11 |
| `live_codex_proxy_mission` | codex · **:8765** | mission | **0** | **0** | **MISSING** | **none** | **11** | **11** |
| `live_codex_raw_trivial` | codex · :8000 | trivial | 0 | — | n/a | none | 11 | 11 |
| `live_codex_raw_mission` | codex · :8000 | mission | **0** | **0** | **MISSING** | none | 11 | 11 |
| `live_claude_raw_mission` | claude · :8000 | mission | 0 | **4 real** | **written** | none | 11 | 11 |
| `live_grok_raw_mission` | grok · :8000 | mission | 0 | **7 real** | **written** | none | 11 | 11 |
| `live_claude_proxy_trivial` | claude · :8765 | trivial | 0 | — | n/a | none | 11 | 11 |
| `live_grok_proxy_trivial` | grok · :8765 | trivial | 0 | — | n/a | none | 11 | 11 |
| `live_codex_proxy_conc8.0…7` | codex · **:8765** ×8 | mission | **0** ×8 | **0** ×8 | **MISSING** ×8 | **none** ×8 | **11** ×8 | **11** ×8 |
| `live_codex_proxy_conc16.8` | codex · :8765, N=16 | mission | 0 | 0 | MISSING | none | 11 | 11 |

The `11` on a clean-exit row means what it always means here — no `result.json` was written. On the
codex rows that is **not** an artifact of the capture prompt: the prompt *asks* for `result.json`,
claude and grok both produce it, and codex does not.

### The reproduction

`live_codex_proxy_mission` is five events long. One `item.completed` carrying the benign
`Model metadata … not found` error, one `item.completed` carrying an `agent_message`, and
`turn.completed` with `output_tokens: 242`. No `command_execution`. The message reads:

```
Let me start by reading the existing file to understand its structure.

<exec>
<cmd>cat /work/slot0/repo/src/greet.py</cmd>
</exec>
…
Added `farewell(name)` to `src/greet.py`. The function mirrors the existing `greet`
pattern and writes the outcome to `out/result.json`:
…
<exec>
<cmd>echo '{"schema_version": 1, "outcome": "executed", …}' > /work/slot0/repo/out/result.json
</cmd>
</exec>
```

`<exec>` is not a tool call. It is text. codex executed nothing, `src/greet.py` was unchanged on
disk and `out/result.json` did not exist — while the model's closing message **asserts that it wrote
both**. That is the precise character of the incident report: the CLI exits 0, believing it
succeeded, and DevCake finds no deliverable.

**Why ADR-0018 does not and should not reclassify this.** `empty_completion` is structural — no tool
activity *and* no non-blank text. Here there are 242 output tokens of non-blank text. The only thing
separating this from a legitimately chatty run is the *content* of a model-controlled string, and
firing a fault arm on a model-controlled string value is exactly what the predicate refuses to do
(the rule is stated on `HARNESS_STATUS_PATTERNS`: an arm may fire on an event **type**, never on a
value the model chose). Every one of these rows is therefore `NO_FAULT` in the assertion table, and
they serve as a **false-positive guard**: a future predicate change that starts faulting them has
begun grading prose.

This is the cascade `docs/08` §8 and `docs/15` §1/§4a already record as a **known gap in ADR-0018's
coverage** — the brake keys on exit 15, and this never reaches 15. What was previously argued from
five uninstrumented runs is now measured, with sidecars, under concurrency, with the filesystem
checked.

### The `:8765` vs `:8000` A/B: the proxy is not the variable

Raw vLLM misplaces the system prompt for codex, so `:8000` is a **known-degenerate** configuration
and the obvious suspect if the proxy had hiccuped. It behaves identically:

| | `:8765` (proxy) | `:8000` (raw) |
|---|---|---|
| trivial prompt | `ACKNOWLEDGED`, exit 0, 1.7 s | `ACKNOWLEDGED`, exit 0, 2.5 s |
| mission prompt | `<exec>` prose, 0 tool calls, no `result.json` | `<exec>` prose, 0 tool calls, no `result.json` |
| item shape | `{error: 1, agent_message: 1}` | `{error: 1, agent_message: 1}` |

Both ports serve codex perfectly on a prompt that needs no tools, and both degenerate the same way on
one that does. **"The proxy hiccuped" explains nothing**, and the A/B is why the placeholder in these
sidecars preserves the port (`http://vllm-backend:8765/v1` vs `…:8000/v1`) rather than flattening it.

### The control: the same backend serves claude and grok correctly

Same server, same model id, same mission-shaped prompt, different harness — and both finish the job:

* **claude** made four real `tool_use` calls (`Read`, `Edit`, `Bash`, `Write`), `num_turns: 5`,
  `is_error: false`, and on disk `src/greet.py` gained `farewell` and `out/result.json` existed.
* **grok** made seven real tool calls over six turns — visible only in its `grok export`, as the
  gate-1 batch established: `- Read: src/greet.py`, `- ListDir: .`, `- Edit: src/greet.py`,
  `- Edit: out/result.json`, … — with `stopReason: "EndTurn"`, and delivered both artifacts on disk.

So this is not "the backend was down" — the hypothesis a simultaneous fleet-wide failure invites, and
the one an operator would chase first. It is codex + this model, and it is total: **thirteen codex
runs across two prompts, two ports and three concurrency levels produced zero tool calls between
them.**

Pointing claude or grok at the codex-shaped `:8765` proxy — a misconfiguration an operator could
land on — turns out not to break them either (`live_claude_proxy_trivial`, `live_grok_proxy_trivial`,
both clean). There is no mis-shaped-prompt failure mode to distinguish, which is itself the record.

### Lane 2 — concurrency, and what saturation does

Both surviving hypotheses for the incident (degenerate generation under load; vLLM preemption /
KV-cache eviction) are pressure-dependent, and the incident happened during a very large run. So the
mission-shaped codex capture was repeated concurrently against `:8765`, starting at the deployment's
real `global_max: 8`:

| N | wall | per-run latency (min / median / max) | outcome |
|---|---|---|---|
| 1 | 6.5 s | — | 0 tool calls, no `result.json`, exit 11 |
| **8** | 45.6 s | 9.1 s / 32.0 s / 50.4 s | **8 of 8** identical: 0 tool calls, no `result.json`, exit 11 |
| **16** | 3 m 30 s | 7.0 s / 59.8 s / **232.7 s** | **16 of 16** identical: 0 tool calls, no `result.json`, exit 11 |

The backend saturates (median per-run latency roughly doubles from N=8 to N=16 and the tail goes out
by 4.6x), and **the verdict never moves**. This is the cascade half of the incident report — "after
which every other container failed identically" — reproduced: a whole board's worth of Devs burning
simultaneously, with no classifiable fault anywhere in it.

**The one thing pressure did change** is committed as `live_codex_proxy_conc16.8`. At N=16, one slot
of sixteen degenerated much further than its siblings: **9,666 output tokens over 232 s** of
repetitive non-progress (`Let me start by reading the existing src/greet.py` and variants, ~40x the
stdout of every other slot), instead of the ~300 tokens of invented tool syntax an unloaded run
produces. It is the only measured instance of backend pressure changing what the model emits. It
still exits 0, still produces exactly one `agent_message` and no tool call, and is still exit 11.

**Escalation stopped at N=16 by design.** The instruction was to cap there and report rather than
escalate to manufacture a failure, and nothing suggests going further would have changed the verdict
— the shape was already identical at 1, 8 and 16.

### What these captures do NOT establish

1. **They do not prove this is what happened on 2026-07-24.** They prove the incident's *signature*
   is reproducible on the incident's own backend, with the incident's own configuration, including
   under concurrency. The production report says the fleet "ran fine during a large run **until**"
   one container failed — a transition. These captures show the failed state, reachable immediately
   and reliably; they do not show the transition into it, and nothing here explains why tool calling
   would have worked earlier in the same run. Reaching the failed state took no special condition,
   which makes the "ran fine until" part of the report the remaining puzzle, not the failure itself.
2. **The concurrency is N processes in one container**, not N containers — equivalent load at the
   backend, which is what a saturation fault depends on, but it does not reproduce per-container
   memory pressure. Every concurrent sidecar records this in `concurrency_note`. One visible
   consequence: `live_codex_proxy_conc8.5` carries an extra stderr line
   (`failed to install system skills: Directory not empty`) because eight codex processes raced on a
   shared `$HOME`. A real eight-container fleet would not hit it, and it changed nothing about the run.
3. **A live backend is a sample.** Every `live_*` sidecar records `reproducible: false`; the model
   id and `captured_at` are the whole of the provenance. A rerun draws a different completion.
4. **Model build drift is unmeasurable from here.** If the deployment's model or proxy changed
   between 2026-07-24 and now, these captures describe today's pairing.

### Scrubbing

The backend's host address is replaced by **`vllm-backend`** in the `live_*` sidecars, and every
sidecar records the rewrite under a `scrub` key — `placeholder`, `occurrences_rewritten`,
`length_preserving`, and `found_in_stream_files: false` — so the substitution is never silent
(`test_live_captures_declare_their_scrub_and_their_non_reproducibility`). Two properties make it
cheap: the address and the placeholder are both **12 characters**, and the address appears in **no**
stream, dump, stderr or `last_message` file, only in sidecar `argv` / `extra` / `preflight.url`. So
no committed stream byte moved and every byte count in this directory is still recomputable.

**Scheme and port are preserved on purpose** — `http://vllm-backend:8765/v1` (proxy) versus
`http://vllm-backend:8000/v1` (raw) is the A/B above, and flattening it would destroy the finding.
The stub URLs elsewhere in this directory stay verbatim; they are provenance that a capture hit a
stub and not a real API.

---

## The assertion table

`app/tests/test_harness_captures.py` is the executable form of everything above. It is parametrized
over the `*.meta.json` sidecars found on disk, loads each capture's companions exactly as `main()`
does (grok's `dump` from `.dump.txt`, codex's `last_message` from `.last_message.txt`, always the
`.stderr.txt` tail), and asserts the composed `(reason, exit code, error class)` against an
**intended** verdict that lives in that file and nowhere else.

Two rules make the exercise mean something:

* **measured facts and expectations never share a file.** A sidecar records what the rig measured,
  including `observed_reason` — the verdict of the predicate *at capture time*. The intended verdict
  is a human judgement and lives in the pytest table, under review. If an expectation could be edited
  in a sidecar, a capture could be quietly "corrected" into agreeing with a wrong predicate.
* **every mismatch was `@pytest.mark.xfail(strict=True)`**, with a reason naming the mechanism. This
  was the evidence half of a two-commit sequence; the fix commit was finished exactly when all of
  them were gone, and `strict=True` meant an accidental fix could not pass silently either.

> **Status: the fix commit has landed.** All 47 rows hold and the test file carries no xfails. The
> "observed today" column below is preserved as the **pre-fix** snapshot — it is the record of what
> the captures found, not a description of the shipped predicate. One intended verdict was changed
> by the fix commit and is marked in place: `grok_empty`. The three `grok_loop_*` rows postdate the
> fix commit entirely, so for them "observed" and "shipped" are the same thing.

The sidecars' `observed_reason` is deliberately **not** asserted (it is a snapshot of a predicate
about to change); their **byte counts** are, on every row, which is the guard that catches a stream
and its sidecar drifting apart.

### The reconciled matrix

`none` means the predicate returns no fault and, on a clean exit, the run is simply handed to the
`result.json` path — there is no failure verdict at all. (The `11` the sidecars record on those rows
is the capture prompt writing no `result.json`; it is not a fault verdict.)

| capture | CLI exit | intended (reason / exit / class) | observed today | xfail |
|---|---|---|---|---|
| `claude_healthy` | 0 | none | none | — |
| `claude_refusal` | 0 | none | none | — |
| `codex_empty` | 0 | `empty_completion` / 15 / DEV_HARNESS_FAULT | none | yes |
| `codex_empty_no_model` | 0 | `empty_completion` / 15 / DEV_HARNESS_FAULT | `empty_completion` / 15 / DEV_HARNESS_FAULT | — |
| `codex_healthy` | 0 | none | none | — |
| `codex_whitespace` | 0 | `empty_completion` / 15 / DEV_HARNESS_FAULT | none | yes |
| `codex_tool_only` | 0 | none | none | — |
| `codex_refusal` | 0 | none | none | — |
| `codex_http_400` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `terminal_error` / 15 / DEV_HARNESS_FAULT | — |
| `codex_http_401` | 1 | `terminal_error` / **12 / DEV_AUTH** | `terminal_error` / 15 / DEV_HARNESS_FAULT | yes |
| `codex_http_401_retrying` | 1 | `terminal_error` / **12 / DEV_AUTH** | `terminal_error` / 15 / DEV_HARNESS_FAULT | yes |
| `codex_http_429` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `terminal_error` / 15 / DEV_HARNESS_FAULT | — |
| `codex_http_500` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `terminal_error` / 15 / DEV_HARNESS_FAULT | — |
| `codex_no_route` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `terminal_error` / 15 / DEV_HARNESS_FAULT | — |
| `codex_truncated` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `terminal_error` / 15 / DEV_HARNESS_FAULT | — |
| `codex_truncated_retrying` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `terminal_error` / 15 / DEV_HARNESS_FAULT | — |
| `grok_healthy` | 0 | none | none | — |
| `grok_refusal` | 0 | none | none | — |
| `grok_tool_only` | 0 | none | none | — |
| `grok_empty` | 1 | `empty_completion` / 15 / DEV_HARNESS_FAULT — **fix commit changed this to `terminal_error` / 15**, see below | `empty_completion` / 15 / DEV_HARNESS_FAULT | — |
| `grok_whitespace` | 0 | `empty_completion` / 15 / DEV_HARNESS_FAULT | none | yes |
| `grok_http_401` | 1 | `terminal_error` / **12 / DEV_AUTH** | `empty_completion` / 15 / DEV_HARNESS_FAULT | yes |
| `grok_http_429` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `empty_completion` / 15 / DEV_HARNESS_FAULT | yes |
| `grok_http_500` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `empty_completion` / 15 / DEV_HARNESS_FAULT | yes |
| `grok_truncated` | 1 | `terminal_error` / 15 / DEV_HARNESS_FAULT | `empty_completion` / 15 / DEV_HARNESS_FAULT | yes |
| `grok_turn_budget` | 1 | `turn_budget` / **16 / DEV_TURN_BUDGET** | none / 10 / DEV_CRASH | yes |
| `grok_json_blob` | 2 | none / **10 / DEV_CRASH** | `no_terminal_event` / 15 / DEV_HARNESS_FAULT | yes |
| `grok_loop_nocap` | 0 | none | none (post-fix capture) | — |
| `grok_loop_cap30` | 0 | none | none (post-fix capture) | — |
| `grok_loop_varying_cap20` | 1 | `turn_budget` / 16 / DEV_TURN_BUDGET | same (post-fix capture) | — |
| **the 17 `live_*` rows** | **0** | **none, every one** | same (post-fix captures) | — |

The seventeen `live_*` rows all postdate the fix commit and all intend `none`. That uniformity is the
finding, not a gap in the table:
[the batch above](#the-live-captures--the-2026-07-24-incident-reproduced-against-the-real-backend)
reproduces the production incident against the real backend and the predicate correctly declines to
fault it, because the model produced text. They are carried here as a **false-positive guard** — the
rows a predicate change must keep *not* firing on.

**11 of the 27 rows that existed then were xfail** at capture time (the three `grok_loop_*` rows
came later and were never xfail — they measure the CLI, not the predicate). Four of them (`codex_empty`, `codex_whitespace`,
`grok_whitespace`, `grok_turn_budget`) were wrong *reasons*; four (`codex_http_401`,
`codex_http_401_retrying`, `grok_http_401`, `grok_json_blob`) were wrong *exit codes*, and all four
of those were ADR-0018 **regressions** — every one of them reached a better code before the ADR
(12, 12, 12, 10). All eleven were flipped by the fix commit; none of the sixteen baselines moved,
and the one intended verdict that changed (`grok_empty`) kept its exit code.

### Where this supersedes the per-harness tables above

The two "What the shipped predicate does with them" tables were written per harness, before the rows
were reconciled against each other. Three cells differ, and the table here is the one the tests
assert:

| row | per-harness table says | assertion table says | why |
|---|---|---|---|
| `codex_truncated`, `codex_truncated_retrying` | intended `no_terminal_event` (a mismatch) | intended `terminal_error` (**not** a mismatch) | codex always emits a plain `{"type":"error"}` immediately before `turn.failed`, on all eight failure captures, so `no_terminal_event` is unreachable for it. A reason the harness cannot express is not an intent. |
| `grok_truncated` | intended `no_terminal_event` | intended `terminal_error` | same rule, same evidence: grok's truncation arrives as a single terminal `{"type":"error"}` event. |
| `grok_json_blob` | intended left blank | intended none / 10 / `DEV_CRASH` | the CLI refused its own argv and never ran, so this is a crash with a precise stderr diagnosis, not a shared-backend-shaped fault. Judgement call — flagged as such in the test file. |

`grok_empty` was a **constraint on the fix**, not a finding: it already landed on the intended exit
code, but for the wrong reason (the arm fired because the error event carries no `sessionId`, not
because grok said `no_visible_content`). **Resolved in the fix commit by letting the new
error-event arm claim it:** the intended reason is now `terminal_error`, the exit code is unchanged
at 15, and the detail carries grok's verbatim `empty response from model (no_visible_content)`.
Keeping it at `empty_completion` would have required matching that message STRING, which buys
nothing when the exit code is identical either way.

### Arms that proved structurally unreachable — and how the fix commit reopened them

Each mechanism below is asserted as a fact about the captured **bytes**, so those assertions stay
true; what changed is the predicate that read them. The right-hand column is the pre-fix diagnosis:

| harness | arm | mechanism |
|---|---|---|
| codex | `empty_completion` | with `-m` set against a backend that does not advertise the id, codex emits `item.completed` with item type `"error"` (`Model metadata … not found`) **before** `turn.started`, and `elif item: items += 1` scores it as tool activity ⇒ `items == 1`. `codex_empty` vs `codex_empty_no_model` is the controlled counterfactual — same stub scenario, argv differing only in the `-m stub-model` pair. |
| codex | `empty_completion` (whitespace) | `messages += 1` fires on any `agent_message` with no `.strip()`, unlike `_claude_activity`. |
| codex | `no_terminal_event` | every failure emits `{"type":"error"}` before `turn.failed`, so `error_msg` is always set. |
| codex | the `turn.*` guard | same reason — `error_msg or …` short-circuits, so nothing ever reads `turn.failed`'s own `error.message`. |
| codex | `turn_budget` | there is no turn cap in `codex exec` at 0.144.4 at all. |
| grok | `empty_completion` | INVERTED: `grok export` prints `## User` + the prompt, so `dump` is non-empty whenever a session id exists. The arm fires only when grok crashed hard enough to have none. |
| grok | `terminal_error` | `GROK_FAULT_STOP_REASONS` is empty by design and there is no `error`-event arm. |
| grok | `turn_budget` | no arm exists, although grok announces the cap twice in band (`{"type":"max_turns_reached"}` **and** `end`/`stopReason:"Cancelled"`) plus `Error: max turns reached` on stderr. |
| grok | the json-blob branch | `harness_argv` always passes `--output-format streaming-json`; a second one is a clap error, exit 2. |
| codex + grok | both auth arms | `api_error_status` is extracted only for claude in `main()`, so an in-band 401 has no structured status; and neither harness's stderr carries the failure (codex: 39 bytes about stdin; grok: generic `unauthorized` wording only). |

**How each was reopened** (all five fixes are in one commit; `codex`'s missing turn cap is a CLI
fact and stays unreachable):

| arm | fix |
|---|---|
| codex `empty_completion` | error items get their own bucket — evidence, never activity — and `agent_message` counts only when its text is non-blank. |
| grok `empty_completion` | `grok_export_activity(dump, prompt)` locates the prompt echo in the export, keeps only what follows it, drops `#`-prefixed headings, and asks whether anything non-blank remains. Deliberately brittle, and both brittleness modes fail SAFE toward "activity found" ⇒ no fault. |
| grok `terminal_error` | fires on the `error` **event type**; `error` and `end` never co-occur across the eleven captures, so no ordering rule is needed. `GROK_FAULT_STOP_REASONS` stays empty. |
| grok `turn_budget` | fires on the `max_turns_reached` **event type**, checked first, mirroring claude — a deterministic cap must never become correlation-eligible. |
| grok json-blob branch | kept as a defensive read only, and no longer reachable for a zero-byte stdout: a CLI that refused its own argv is not a harness fault at all. |
| codex + grok auth | `harness_api_error_status()` extracts a status for **every** harness, matching each CLI's own HTTP-layer wording (`unexpected status NNN`, `last status: NNN`, `Unauthorized (NNN)`, `(status NNN`) rather than a generic `status (\d{3})` — codex echoes the server's response body into `error.message` (`codex_http_400`), so a generic pattern would let a backend put "status 401" in a 500 and pause a whole Dev Type. |

### Known gaps between these fixtures and production

These are the ways a real run can differ from what is committed here. None of them is a defect in a
capture; they are the boundary of what the captures can be used to argue.

1. **Retry-shaped streams exist for two rows only.** Six of the eight codex failure captures were
   taken with `request_max_retries=0` / `stream_max_retries=0` so the stream would show one clean
   failure. Production runs with codex's defaults, which prepend five `Reconnecting... N/5 (…)`
   `error` events. That shape is captured **only** in `codex_http_401_retrying` and
   `codex_truncated_retrying` — where it is proven not to change the verdict (the last `error`
   message wins, and `turn.completed` is still absent). There is no retrying counterpart for 400,
   429, 500 or `no_route`, and none for grok, where no capture disables retries — grok's are already
   the production shape, at ~5¾ minutes per attempt.
2. ~~**The backend is a stub, not a saturated inference server.**~~ **Closed by the `live_*` batch**
   for codex, claude and grok against one real backend, including a saturated one (N=16, per-run
   latency out to 232 s). It remains true of every `codex_*` / `grok_*` / `claude_*` row: those
   reproduce one clean condition each, whereas a real shared fault mixes them, arrives mid-stream and
   can differ per container. What no capture here has is a real backend *failing* — the live one
   answered every request it was given.
3. ~~**Concurrency is not represented.**~~ **Closed by `live_codex_proxy_conc8.0…7` (N=8, the
   deployment's `global_max`) and `live_codex_proxy_conc16.8` (N=16).** The residual caveat stands
   and is recorded in those sidecars' `concurrency_note`: the rig runs N harness processes in **one**
   container, which is equivalent load at the backend but does not reproduce per-container memory
   pressure. Every other committed capture is still `concurrency: 1`.
4. **No timeout/SIGKILL capture exists.** codex's real turn-cap failure mode is an unbounded run
   terminated by DevCake's own timeout, arriving as a signal kill. The runaway that proved it exists
   produced no files (see the rig defect above); the nearest committed evidence is
   `claude_aborted_streaming.jsonl`, which is a SIGTERM during retry backoff on a different harness.
5. **Two CLI versions of Claude Code.** The four fault captures are 2.1.219 and the two conservatism
   captures are 2.1.210; grok and codex each have exactly one version. Sidecars carry `cli_version`,
   and the 2.1.219 batch predates the rig so it has no sidecar at all — those four are asserted by
   `test_entrypoint_fault.py`, and `test_harness_captures.py` names them explicitly so no stream can
   fall between the two suites.
6. **The capture prompt does not write `result.json`.** That is why every clean-exit sidecar records
   `devcake_exit_now: 11`; it says nothing about the fault predicate, and the tests do not assert it.
   **The `live_*` rows are the exception and must be read differently:** their prompt *does* ask for
   `result.json`, the workspace was bind-mounted and inspected afterwards, and claude and grok both
   produced it. So on those rows the `11` is a measured outcome, not an artifact — and on the codex
   rows it is the incident.
7. **A live capture cannot be re-taken.** The seventeen `live_*` rows sample a real model once;
   each sidecar says `reproducible: false`. Their backend address is placeholdered to
   `vllm-backend` with scheme and port preserved, recorded per sidecar under `scrub`.

---

## Synthetic fixtures

Any fixture added here that was *not* captured from a real CLI must be named `synthetic_*.jsonl` and say
so in this table. **Zero-exit `turn_budget`** can only be exercised synthetically: Claude Code exits 1
on max-turns, grok exits 1, and codex has no turn cap at all, so the zero-exit arm is defensive
completeness for a harness that does not exist yet. There are no synthetic fixtures in this directory
today; all three harnesses now have real captures.
