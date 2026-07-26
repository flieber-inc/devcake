# 19 — The local-backend pairing: a measured tool-calling failure

> **Audience:** implementers and operators. This document is the evidence file for
> one model+backend pairing on which **codex does not tool-call**, and for the
> exit-11 cascade that follows. It is a case study, not a statement about local
> backends generally.
> **Depends on:** `08-harness-templates.md` §8a (how to point each harness at a
> local backend — the configuration recipes live there, not here),
> `15-errors-and-retries.md` (exit classes),
> `adr/0018-harness-fault-classification-and-backend-brake.md` (the brake this
> failure is NOT covered by).

Split out of `08-harness-templates.md` §8 on 2026-07-26: the analysis had grown to
half of a reference document about harness templates, which is the wrong shape for
both. `08` keeps the per-harness configuration contract and the capture rig; the
incident evidence lives here.

**One-line version.** Against `DeepSeek-V4-Flash-DSpark-Abliterated`, codex 0.144.4
answers with prose containing invented tool syntax, executes nothing, and exits 0 —
so DevCake reports exit 11 `DEV_BAD_OUTPUT`, on every container at once, with no
brake. `claude-code` and `grok-build` complete the same task on the same server and
model. The cause is the size of codex's optional-parameter tool surface, not the
backend and not the proxy.

## 1. The measured pairing (2026-07-26)

| element | measured |
|---|---|
| Backend | vLLM serving `DeepSeek-V4-Flash-DSpark-Abliterated`, `max_model_len` 300000, at `http://<vllm-host>:8000` (raw) |
| Proxy | request-rewriting proxy on `:8765` that repositions the system prompt for codex |
| Routes | **both** ports serve `/v1/chat/completions`, `/v1/responses` and `/v1/messages` |
| CLI versions | read from inside the baked images: codex-cli **0.144.4**, claude **2.1.210**, grok **0.2.112** |

**Symptom.** `codex`, invoked through DevCake's own `harness_argv`
(`08-harness-templates.md` §1), never makes a real tool call. It emits tool syntax
as **prose** inside an `agent_message`,
executes nothing and writes no files — while its closing message asserts that it
wrote `out/result.json` — and exits **0**.

**Committed evidence since 2026-07-26, not a campaign note.** The seventeen `live_*`
captures in `app/tests/fixtures/harness_streams/` were taken against this same
backend with the founder's verbatim incident configuration, and they carry the
symptom into the test suite: **13 codex runs across two prompts, both ports and three
concurrency levels (1, 8 and 16) produced zero tool calls between them**, every stream
being exactly one `agent_message` plus the benign `-m` metadata error item. The
workspaces were bind-mounted and inspected afterwards, so "wrote no `result.json`" is
a filesystem fact on every row. These supersede the 2026-07-25 campaign, which argued
the same finding from five uninstrumented mission-shaped runs; that campaign also saw
two invented formats no committed capture holds (`<tool_call type="exec" cmd="…">` and
narrated HTML with a fabricated `▶` prompt and hallucinated command output), against
the `<exec><cmd>…</cmd></exec>` form committed in `live_codex_proxy_mission`. See that
directory's README, *The live captures*, for the reproduction, the `:8765`-vs-`:8000`
A/B (identical — the proxy is not the variable) and the concurrency measurements.

**The backend is not at fault**, and that was established before anything else was
touched. Direct protocol probes with no CLI involved, on both ports: `POST /v1/responses`
with one simple tool returns a real `function_call`; `POST /v1/messages` returns a real
`tool_use` (2026-07-25 campaign notes, no fixture). And against the *same* model,
backend and prompt the other two harnesses finish the job — both committed controls:
`live_claude_raw_mission` (**4 real `tool_use` blocks**) and `live_grok_raw_mission`
(7 real tool calls), each leaving `result.json` on disk.

## 2. What the bisect isolated (2026-07-25)

codex's verbatim outbound request was taken from the capture stub's `journal.jsonl`
(`08-harness-templates.md` §8, *The capture rig*) and replayed by hand against `:8765` with `stream: false` — campaign notes,
not committed fixtures:

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
non-`function` tool types — each of those was eliminated by a row in the table above.
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

## 3. What DevCake sees, and why PLAN masks it

Because the model is being pushed to the edge of its schema-handling ability, it
degrades **probabilistically**: it tool-calls on some runs and narrates on others.
When it narrates, no Dev can write `/workspace/out/result.json`, so the run reports
**exit 11 `DEV_BAD_OUTPUT`** (`15-errors-and-retries.md` §1) — on every container, at
once, for the same reason the ADR-0018 incident was fleet-wide: the transducer is
uniform, so a shared backend fault arrives identically everywhere with no contagion.

**PLAN is the exception, and that asymmetry is the confusing part.** Plan mode is
read-only by construction, so the entrypoint synthesises `PLAN.md` and `result.json`
from the returned text (`08-harness-templates.md` §3), gated only on that text being ≥ 200 chars. A PLAN step
therefore **succeeds with zero tool calls**, while ONBOARD, EXECUTE and REVIEW fail.
A board can look healthy until the pipeline advances into a stage that needs a real
tool call.

**Known gap — the ADR-0018 brake does not cover this.** `backend_correlated` /
`backend_degraded` key on `error_class == "DEV_HARNESS_FAULT"` (exit 15,
`15-errors-and-retries.md` §4a). These runs are `DEV_BAD_OUTPUT` (exit 11), so a
fleet-wide bad-output cascade is throttled by nothing, excused by nothing, and every
failure counts toward `max_attempts`. Recorded, not fixed.

**Measured, including under concurrency (2026-07-26).** At the deployment's real
`global_max: 8`, and again at N=16 with the backend visibly saturated (per-run latency
out to 232 s), every concurrent mission-shaped codex run on the incident path failed
this way at once and the verdict never moved off exit 11 — there is no load at which
this cascade becomes visible to the predicate. Nor is it a predicate defect to fix:
the model *answers*, at length, so the only thing separating these runs from a
legitimately chatty one is the content of a model-controlled string, which is exactly
what no fault arm may key on
(`adr/0018-harness-fault-classification-and-backend-brake.md`: an arm fires on an event
**type**, never on text the model chose). The remedies in §4 are the whole of the response.

**A second route to the same exit 11, on grok.** A model that keeps repeating the
*same* tool call is halted by grok itself at 16 turns, silently and with exit 0
(`08-harness-templates.md` §1c, which has the signature to recognise it by) — same class, same absent brake,
and the same fleet-wide arrival, because "the model is too weak for this step" is a
property of the shared backend. Operator's version: `15-errors-and-retries.md` §2b.

## 4. Operator remedies

In increasing order of effort; all three are operator-side, because nothing in
DevCake misbehaved — the invocation, the entrypoint and the predicate all did exactly
what they specify:

| effort | remedy | evidence |
|---|---|---|
| none | **don't assign codex to a Dev Type pointed at this model** — route the stage to `grok-build` or `claude-code` (`08-harness-templates.md` §8a has the per-harness recipes) | `live_claude_raw_mission`, `live_grok_raw_mission`: same server, same model, same prompt, task finished and `result.json` on disk |
| lowest | set `tool_choice: "required"` on the request | worked every time in the bisect |
| medium | slim tool schemas proxy-side, dropping optional properties on `/v1/responses` — the proxy already rewrites requests, so it is the natural home | dropping `exec_command`'s nine optional properties restored tool calling |
| highest | use a model with better large-optional-schema handling | `claude-code` on this same model and backend never exhibited the fault |
