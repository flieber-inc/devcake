# ADR-0018 — Harness fault classification and the model-backend brake

- **Status:** accepted (2026-07-24)
- **Context:** On 2026-07-24 a large run on a 16 GB VM driving a local vLLM had one Dev container fail with `exit 11 — result.json missing/invalid: [Errno 2] No such file or directory: '/workspace/out/result.json'`, and within moments every other in-flight container failed identically. The operator's inference — "something fundamental is wrong" — was right. `dev_entrypoint.py` decided harness failure from ONE signal, the process exit status (`:864`, `:964`), and classified it on **stderr**, which these CLIs leave empty on failure (measured **on the incident harness, Claude Code 2.1.219**: 349 bytes of unrelated connector warnings — that figure is harness- and version-specific, not a fleet property; the 2026-07-25 amendment records what each CLI actually writes). Every in-band failure signal was parsed for the token report and then discarded — `grep is_error` over the repo returned zero hits. Measured against stub backends (Claude Code 2.1.219): a hard 400 exits **1**; `--max-turns` exhaustion exits **1**; and **HTTP 200 with an empty completion exits 0**, producing `subtype:"success"`, `result:""` and no `result.json` — which the entrypoint reported as the Dev's bad output. A saturated inference server returns 200-with-nothing while its health endpoint stays green, so every container hit it simultaneously and independently produced a byte-identical exit-11 artifact. There is no cross-container contagion: the transducer is uniform, and `DEV_BAD_OUTPUT` was the one failure class with no breaker, no backoff and a counted attempt, so `max_attempts=3` burned the whole board to `DEVCAKE-FAILED` in about three poll cycles.

## Decision

### 1 — The entrypoint asks whether the harness actually worked

`harness_fault(harness, out, harness_exit, *, dump, last_message)` returns `{"reason", "detail"}` or `None`, dispatching to per-harness predicates. Four reasons: `turn_budget`, `terminal_error`, `empty_completion`, `no_terminal_event`. *(2026-07-25: the signature gained a keyword-only `prompt` — see the amendment.)*

**Conservatism is the governing constraint.** A model refusal and a tool-only run must never be called faults — a false positive excuses a mission's failed attempt and throttles a whole Dev Type. `empty_completion` therefore uses a **structural** discriminator (no non-blank assistant text blocks AND no `tool_use` blocks AND an empty final `result` AND the run claims success), never a token threshold. The committed fixtures prove why: `usage.output_tokens` is `0` for **both** the incident and a fifteen-turn tool-only run, and `modelUsage` is non-zero even for an empty completion because the stop token costs tokens. Block counts separate them cleanly and also classify a whitespace-only completion correctly.

`subtype` is matched on the `"error"` prefix only — the 400 capture carries `subtype:"success"` *alongside* `is_error:true`, so a positive match on "success" proves nothing. grok's `stopReason` only ever annotates (`GROK_FAULT_STOP_REASONS` is empty by design), and codex treats `turn.completed` as the success signal, so an `error` event on either side of it is superseded or post-hoc noise.

### 2 — Two new exit codes, and turn budget gets its own

- **15 `DEV_HARNESS_FAULT`** — correlation-eligible.
- **16 `DEV_TURN_BUDGET`** — never correlation-eligible, always counted.

Splitting turn budget into its own **code and class** is what makes the carve-out implementable at all: the detector keys on `error_class`, so a single shared class would have left it no way to exclude turn-budget runs from the evidence. Turn exhaustion is deterministic — retrying against the same cap cannot help — so treating a fleet that all hit `--max-turns 15` (the shipped ONBOARD default) as a transient shared fault would excuse them all and retry forever against a wall. Its detail names the cap and where to change it.

> *Annotation (2026-07-25).* The motivating fleet was **claude-code-only** when this was written: it was the only harness with a turn-budget arm. After the fix commit `grok-build` reaches 16 too (it takes `--max-turns` and announces the stop in band), and `codex` 0.144.4 **never can** — it has no turn cap of any kind, so a `--max-turns 15` fleet is not a shape codex can take at all. See the amendment and `15-errors-and-retries.md` §2a.

### 3 — Precedence, and the success rows are scoped to a clean exit

**Nonzero exit** (result.json is never read, matching today): `turn_budget` → 16; distinctive auth evidence → 12; predicate fired → 15; else 10.
**Zero exit**: a valid canonical `result.json` → success; `turn_budget` → 16; predicate fired → 15; a recovered misplaced result → success; else 11.

Scoping the success rows to zero-exit preserves the property that makes this safe to ship: **net effect on retry behaviour is zero** — every case moving to 15/16 was already counted as 10 or 11. Letting a valid `result.json` win on a *nonzero* exit would have turned today's failure into a success and driven a real PMO transition.

`12` now needs **distinctive** evidence (`api_error_status ∈ {401,403}`, or the `not signed in` / `grok login` markers) whenever the predicate has already explained the failure. `HARNESS_AUTH_MARKERS` includes bare `authentication` and `unauthorized`, which OpenAI-compatible gateways emit on ordinary rejections — without this, a backend fault would latch a per-Dev-Type breaker and pause every mission for that Dev Type, reproducing the original misdiagnosis in a new code.

### 4 — Evidence on every failure artifact

Exits 10, 11, 15 and 16 now carry `error_class`, `error_detail` and a bounded `workspace_forensics` block (three syscalls, no recursion, budgeted so `json.dumps` stays under ~1 KB even with non-ASCII filenames): the harness's own exit status (negative ⇒ killed by signal — previously unrecoverable), a listing of `/workspace/out`, writability, free disk, stdout size, and the stderr tail that proves the classified channel was empty. It is also rendered into `transcript_md`, because on a lockstep skew an old app drops `error_detail` but the transcript is always posted (INV-5).

### 5 — Misplaced `result.json`: diagnose always, recover behind a toggle

The harness's cwd is the repo clone, so a cwd-relative write lands there. `find_result_json` checks a **fixed candidate list** (no traversal), gated on `mtime >= harness start` so a file already in the clone can never be adopted, skipping symlinks and non-regular files, and losing to the predicate. Diagnosis is unconditional — the path, its mtime and its git-tracked status reach the run terminal and the transcript whether or not `recover_misplaced_result` (default on) lets the run finalize on it. Git-tracked matters separately from freshness: EXECUTE commits at the end, so a stray may already be in the PR.

### 6 — A store-derived brake, not a third circuit breaker

`domain/backend_health.py`, in the `mapper_service.degraded()` idiom: store-derived, restart-safe, no counters, cleared implicitly by success. A latched breaker was rejected because its documented reset is "the write itself is the reset" (`docs/15` §4) and a backend outage has no credential to write — it would need an interactive clear control the same section forbids, and would convert a three-minute wobble into an indefinite stop pending a human. A timer was rejected as the first time-based mechanism in the domain layer.

**Two predicates, because throttling and accounting are different questions.** `backend_correlated` (≥2 faults spanning ≥2 distinct *truthy* `mission_pmo_id` in the last 3 terminal runs of a dev type) may excuse an attempt. `backend_degraded` is a superset that also fires on a solo streak and only caps concurrency. Routing both through one return value made the single-mission fallback inexpressible: it must throttle *without* excusing.

The solo arm selects its **own** evidence — the three most recent *mission-bearing* terminal runs of the dev type — rather than filtering mission-bearing runs out of the shared 3-run window. Filtering the shared window made the arm dead in exactly the deployment it exists for: one PMO-less run (a Relations Mapper sharing the dev type) capped the list at two forever, so the streak was unreachable. The mission-bearing filter itself stays, because three MAPPER faults must not throttle a dev type's real missions.

`refresh_degraded` intersects its run-derived keys with the **live dev-type registry** (unioned from the managers `PollRuntime` already holds — no composition-root import, ADR-0015). Without it a renamed or deleted dev type keeps a permanent, unclearable entry, because the two greens that clear degradation can never arrive; the renamed type meanwhile inherits no evidence and dispatches at full concurrency mid-outage.

Degraded mode caps the dev type to **one probe run** rather than blocking. The probe IS the half-open — it is what lets the condition clear itself, and it is why no timer is needed. Successes are included in the window on purpose: evicting fault evidence is the entire clearing mechanism.

### 7 — Escape hatches, in scope rather than deferred

The evidence *is* the faults, so once armed every later fault would be excused and become fresh evidence, re-arming the window forever: a bad model id or a quota wall would retry with no give-up at all. `excusals_left` bounds it — a given (mission, mission_type) step may be excused at most `MAX_EXCUSALS_PER_STEP` (3) times, after which its failures count and it reaches `DEVCAKE-FAILED` normally.

The same bound now covers **`DEV_FORGE`**. The founder's decision was to stop plain forge failures burning attempts, but plain exit 13 latches no breaker (only the `DEV_FORGE_AUTH` arm calls `forges.latch`), so an unconditional exemption would re-dispatch every poll interval forever on a bad branch name, a DNS failure or a 500. It is uncounted while the step has excusals and counted once they are spent.

`DEV_FORGE_AUTH` keeps the *uncapped* exemption of `UNCOUNTED_CLASSES`, and that is bounded **only** because the class always latches the per-repo breaker — so the class is stamped exclusively on the container's structured `error_class`. Auth *wording* in the failure detail is not evidence for it: an earlier marker arm ("403"/"401"/auth phrases anywhere in the 500-char detail) stamped the exempt class while leaving the latch to the structured arm, so a push rate-limited with "HTTP 403" — or any pre-taxonomy image sending no `error_class` — produced uncounted, breaker-less retries forever, contradicting this section's own invariant. Marker-only hits now take the bounded `DEV_FORGE` path; the detail still carries the wording, and an orphaned or skew-dropped genuine credential failure degrades to a terminating path instead of a livelock.

### 8 — Classify at the chokepoint, never by enumeration

`run.error_class` is stamped inside `RunManager._kill_inner` from a state-keyed map with a `DEV_KILLED` catch-all, so a future kill site cannot silently produce an unclassified run. Two drafts of the plan tried to enumerate the kill sites and both were wrong — the second still missed `api/clear.py`, mislabelled `router.py` as "orphaned" when it sets `"failed"`, and omitted two watchdog paths. The two genuine bypasses (`router.py::_fail`, `finalize.py`'s `transitions` ValueError) stamp their own.

`attempt_number` now matches the structured class, not a substring of `run.error`. That match was **injectable and the path is live**: `decomposition.py` raises with the Dev's `blocked_by` list verbatim and finalize wraps it into `run.error`, so a Dev emitting `blocked_by: ["DEV_AUTH"]` made its own failures stop counting and its mission never give up. Pre-upgrade records fall back to a **prefix** match, which injected text in the tail cannot reach.

## Alternatives considered

- **A third entry in `/health`'s `circuit_breakers`.** Rejected: the SPA renders that whole map as ONE alert whose remediation is chosen by a `repo:` prefix test, so a third namespace would tell the operator to refresh a credential that is fine; `services.js` would paint the Dev card red for a self-healing throttle; and the map's documented semantics are *latched until a credential write*. It gets its own `dev_backend_degraded` field, shaped like `poll_degraded`. (The breaker alert's single trailing remediation was fixed to be per-entry while we were there — a mixed map previously gave half the entries the wrong advice.)
- **Always-uncounted for exit 15.** Rejected: one mission deterministically producing exit 15 would retry every ~60 s forever with two feed comments per attempt and no give-up.
- **A cooldown/half-open timer.** Rejected — see §6.
- **Blocking dispatch while degraded.** Rejected: nothing could then succeed, so nothing could clear the condition.

## Consequences

- Worst case per outage: two missions lose one attempt each *per dev type* before the brake engages, because the failing run's own record is still `finalizing` in the store when it classifies itself. Compare today: every in-flight mission burns to `DEVCAKE-FAILED`.
- Protection is strongest for multi-mission fleets and weakest for single-mission deployments and dev types already at `max_concurrency = 1` — those get throttling only, via the solo arm.
- The brake is per-dev-type while the fault is per-backend; DevCake has no first-class backend concept. A shared-vLLM outage proves correlation within each dev type independently. Escalating "≥2 degraded dev types ⇒ global" is deliberately deferred until real exit-15 data exists.
- Evidence is shared across PMO instances deliberately: instances share one `/data/state/runs` (ADR-0009) and a model backend is a property of the deployment.
- This deepens the control plane's dependence on a store documented as advisory (INV-1). `attempt_number` already did; the throttle now does too. Clear-runs consequently resets the brake — correct here, and the opposite of the auth-breaker rule, because this condition *is* run history.
- `dev_failure_error` now MUTATES the run (`error_class`, and `attempt_counted` on exits 13/15). The port docstring says so; a fake returning a bare string silently exercises the legacy branch.
- The no-outcome finalize branch has no `finalized_steps` guard, so a redelivered artifact re-evaluates the detector against a different snapshot and `attempt_counted` can flip **either way**. Bounded and accepted, not a guarantee.
- The exit-15 excusal gate keys on a container-supplied `error_class`, a new Dev-reachable lever on attempt accounting. Bounded by the ≥2-mission correlation requirement and the excusal cap, and consistent with the existing trust in `exit_code`.
- `empty_completion` covers "the backend returned nothing from the first turn". A backend degrading **mid-run** leaves real tokens and text, so it reports exit 11 unless the CLI surfaces an error flag. Deliberate boundary — loosening it is how a refusal gets excused.
- The codex and grok predicate arms are designed from `docs/08` and the parsing code; only `claude-code` has real captures. They stay provisional until a capture session supplies streams (`turn.failed` is a named gap). — **The capture session happened on 2026-07-25 and this bullet was right to worry: both arms were wrong. See the amendment below.**
- `dev_failure_error` gained an `O(runs)` store scan on the failure path, which fires precisely during a cascade. Measure it against an incident-scale store; the fallback is threading the poll cycle's existing snapshot.

## Amendment — 2026-07-25: what the capture campaign measured

*Added after the fact; nothing above is rewritten. The decision stands — the entrypoint asks whether the harness worked, and that was right. What the campaign found is that the **implementation** of that question was right for exactly one of the three harnesses.*

27 captures were taken against a controllable stub backend across `claude-code` 2.1.210, `codex` 0.144.4 and `grok-build` 0.2.112 (`app/tests/fixtures/harness_streams/`, one `*.meta.json` sidecar per capture recording argv, exit status and byte counts). Every claim below traces to a committed fixture and is asserted in `app/tests/test_harness_captures.py`. The evidence commit landed eleven rows as strict expected failures; the fix commit flipped all eleven.

### A. The predicate as merged worked for claude, and only for claude

**`empty_completion` was structurally unreachable for codex.** `codex_run_fault` scored every unrecognized `item.completed` as tool activity (`elif item: items += 1`). With `-m` naming a model the backend does not advertise, codex emits an `item.completed` whose item type is `"error"` — `Model metadata for <model> not found` — **before `turn.started`, on every run, healthy or not**. That benign metadata warning made `items == 1`, so the arm could never fire. This is not an edge case: *every* local backend serves a model id codex has no metadata for, so the headline arm of ADR-0018 was dead in exactly the deployment ADR-0018 was written for. The controlled counterfactual is `codex_empty` vs `codex_empty_no_model` — same stub scenario, same CLI, argv differing **only** in the `-m stub-model` pair (`test_the_two_codex_empty_captures_differ_only_in_the_model_pin`); the pinned run carries the error item, the bare one carries no item at all, and both completed a turn with `output_tokens: 0`. Error items now have their own bucket and are evidence only. A whitespace-only `agent_message` is also no longer counted as a message, mirroring `_claude_activity` (`codex_whitespace`).

**`empty_completion` was inverted for grok.** grok's stream carries no tool-call events at all, so the `grok export` transcript is the activity signal — and the export **always opens by echoing the whole prompt back under `## User`**. `dump.strip()` was therefore truthy for every run that got far enough to have a session id, so the arm fired only when grok had crashed hard enough to have *no* session, and never in the 200-with-nothing case it exists for. `grok_whitespace` is grok's genuine miss: a whitespace-only answer is dropped from the export entirely, leaving a ~100-byte dump that is the prompt and nothing else, and the naive `dump.strip()` test still reports "the run worked" (`test_grok_export_echoes_the_prompt_so_dump_is_never_empty`).

Two smaller corrections came out of the same table. `grok_empty` (a 200 with nothing) is reported by grok itself as an `error` event naming its own condition — `empty response from model (no_visible_content)` — so it lands on `terminal_error`, not `empty_completion`; same exit 15, strictly more detail. `grok_json_blob` is **not** a harness fault at all: grok 0.2.112 exits 2 at *argument parsing* when `$DEVCAKE_EXTRA_ARGS` duplicates `--output-format`, so the CLI never ran and stdout is zero bytes. An operator misconfiguration must not be correlation-eligible — it is deterministic across the whole fleet, which is the property §2 separates `turn_budget` out to avoid — so it falls through to exit 10 `DEV_CRASH`, where it was before ADR-0018, and stderr carries the exact clap diagnosis.

### B. The auth regression this ADR shipped, and the design rule that fixed it

`main()` computed `api_error_status` for **claude only**, so a 401 on codex or grok had no structured status, the predicate won §3's precedence, and the run landed on **15** — excusable and correlation-eligible. That is a live regression introduced here: before ADR-0018 `grok_http_401` reached **12** on the generic stderr marker and `codex_http_401` reached **10**. An expired key rolled out to a whole Dev Type therefore read as a shared backend outage and was *excused* instead of latching the auth breaker.

The fix is a pure helper, `harness_api_error_status(harness, out)`, and it carries a rule the rest of the predicate should be read against:

> **Status patterns are anchored on each CLI's own transport wording, and are read only from the CLI's own `error` / `turn.failed` events — never from the whole stream.** An assistant message is model-controlled text, and a model that writes `unexpected status 401` into its answer must never be able to pause a Dev Type.

The patterns are `unexpected status NNN`, `last status: NNN` (codex), `Unauthorized (NNN)`, `(status NNN` (grok). A generic `status (\d{3})` was rejected on evidence, not taste: codex echoes the server's response body **verbatim** into `error.message` (`codex_http_400`), so a backend that put `status 401` in the body of a 500 would drive exit 12. **Precision over recall, because the two failure directions are not symmetric** — missing a 401 falls through to exit 15, which is excusable, still counted, and still shows the operator the message; a false 12 pauses an entire Dev Type until a human re-uploads credentials. Rows restored: `codex_http_401`, `codex_http_401_retrying`, `grok_http_401` → 12.

### C. grok's export parse is deliberately brittle — a founder decision

`grok_export_activity(dump, prompt)` locates the prompt verbatim in the export, keeps only what **follows** it, drops `#`-prefixed heading lines, and asks whether anything non-blank is left. It can miss in exactly two ways:

1. **the prompt must appear verbatim** — if grok ever reflows, re-indents or truncates the echo, the search returns -1;
2. **`#`-prefixed lines must be section markers** — if the export format changes, fewer lines are stripped and more content survives.

**Both fail SAFE, toward "activity found" ⇒ no fault ⇒ exit 11**, the pre-ADR-0018 status quo. Neither can manufacture a *false* fault, and a false fault is the expensive direction: exit 15 excuses the attempt and feeds the per-Dev-Type correlation brake. This was accepted knowingly rather than hardened.

The obvious alternative — match on heading **names**, "any `##` other than `## User` means activity" — is **inert in production**: every real DevCake prompt opens with `## Your current mission type: …` plus `### Workspace` / `### The mission` (`app/devcake/prompts/__init__.py`), and those headings live *inside* the echoed User section, so the rule would report activity for every run. `prompt` is plumbed through `harness_fault` into `grok_run_fault` for this and only this.

### D. Arms measurement proved unreachable — do not "fix" these

| Arm / concern | Verdict | Evidence |
|---|---|---|
| `no_terminal_event`, codex | **unreachable** — every codex failure emits a plain `{"type":"error"}` immediately before `turn.failed`, so a stream with no `turn.completed` always has a message to classify on; `terminal_error` is the intended reason for all eight failure rows | `test_codex_always_emits_an_error_event_before_turn_failed` |
| `no_terminal_event`, grok | **unreachable** — every grok hard failure is exactly one `error` event and nothing else | `test_grok_reports_every_hard_failure_as_one_terminal_error_event` |
| exit 16, codex | **unreachable** — codex 0.144.4 has no turn cap of any kind and no config key for one; the only bound on a runaway codex Dev is the **global** `dev_timeout_minutes` (`config.py:343`), which arrives as a signal kill reported `DEV_TIMEOUT` | `08-harness-templates.md` §1b, `15-errors-and-retries.md` §2a |
| grok `error` vs `end` ordering | **no rule needed** — the two never co-occur across the eleven grok captures; an `error` event *is* the terminal verdict | the capture set |
| grok `thought` events | **none at 0.2.112** — no capture contains one (`docs/08` §1 records `thought` as a 0.2.93 shape, unverified at 0.2.112), so the `thoughts` counter `grok_run_fault` kept could never be nonzero; it is gone, and unrecognized event types are no longer counted as activity either — that is the same mistake that killed codex's arm | the capture set |

Reachable and now asserted: grok **does** reach exit 16. `grok_run_fault` gained a `turn_budget` arm firing on the `max_turns_reached` **event type**, checked first exactly as claude's is, so a deterministic cap can never fall through into `terminal_error` and become correlation-eligible (`grok_turn_budget`).

> *Annotation (2026-07-25, later the same day).* Three more grok captures were taken after the fix commit and change none of the above — they measure the CLI, not the predicate. grok **halts a run itself** at 16 model calls when the model repeats the identical tool call, with `stopReason:"EndTurn"` and exit 0, so the run reports **exit 11 `DEV_BAD_OUTPUT`** with no diagnosis (`grok_loop_nocap`, `grok_loop_cap30`). That is a non-progress halt, **not** a turn cap: the same lane with varying tool arguments runs past 16 and honours `--max-turns 20` (`grok_loop_varying_cap20`), so §D's "grok reaches 16" and `15-errors-and-retries.md` §2a stand unchanged. It is a second route into the unbraked exit-11 gap §4a already records — see `15-errors-and-retries.md` §2b and `08-harness-templates.md` §1c.

### E. stderr, per harness

The §Context premise — the classified channel carries no failure information — **holds**, and is now measured rather than assumed. It is not one number:

- **codex 0.144.4** writes exactly **39 bytes** on **every** failure, whatever the failure is: `Reading additional input from stdin...`, which is not about the failure and matches no auth marker at all (`test_codex_reports_a_401_in_band_only`). The one genuinely diagnostic line codex puts on stderr — `Warning: no last agent message; wrote empty content to <path>`, 134 bytes total — appears on a *zero-exit* empty completion, and nothing reads it.
- **grok 0.2.112** does write real wording (25–247 bytes across the failure captures), but its 401 matches only the *generic* auth markers, which rank below the predicate in §3 — which is why its 12 is restored from the in-band status and not from stderr.
- **claude-code 2.1.210** writes **0 bytes on success** (both conservatism captures). Its *failure* stderr is **unverified at 2.1.210**: the 349-byte figure in §Context was measured at **2.1.219** on the incident host, and no 2.1.210 failure stderr was captured.

## Related

- `docs/07-dev-runtime.md` §4 (exit codes), `docs/15-errors-and-retries.md` §1/§2/§2a/§4a, `docs/09-messaging.md` §3 (`run.artifacts` now documents `exit_code`/`error_class`/`error_detail`), `docs/03-mission-lifecycle.md` §3 (INV-6 scopes code changes, not outputs), `docs/11-admin-panel.md`, `docs/12-observability.md`.
- Implementation: `images/common/dev_entrypoint.py` (`harness_fault`, `harness_api_error_status`, `harness_error_messages`, `grok_export_activity`, `workspace_forensics`, `find_result_json`, `bad_output_reason`, and the `render_codex` / `GrokCoalescer` relay arms for the terminal events), `app/devcake/domain/backend_health.py`, `finalize.dev_failure_error`, `dispatch.counts_toward_attempts`, `runs.KILL_CLASSES`, `schedule.schedule`, `poll.run_cycle`.
- Evidence: `app/tests/fixtures/harness_streams/` — 34 live CLI captures (Claude Code 2.1.219 ×4 from the incident and 2.1.210 ×2, codex 0.144.4 ×14, grok-build 0.2.112 ×14), 30 of them with a measured-facts sidecar, plus a README recording why token counts are not a fault signal and where each fixture's claims stop. Asserted by `app/tests/test_harness_captures.py` (verdicts), `app/tests/test_entrypoint_fault.py` (the pre-rig incident captures) and `app/tests/test_entrypoint_render.py` (every capture renders, and every terminal event produces a visible line).
