# ADR-0022 — In-container continuation of clean-but-incomplete runs

- **Status:** accepted (2026-08-02)
- **Context:** Stress-testing against a weak local model (Grok3.6-27B via
  vLLM, grok-build) killed ~50% of long runs as exit 11 `DEV_BAD_OUTPUT`:
  a clean `end` event (`stopReason EndTurn`), no fault, no
  `/workspace/out/result.json`. The mechanism, confirmed from failed-run
  tails, is *narrate-and-stop*: the model ends a turn with intent prose
  ("Let me continue with the git workflow…") instead of a tool call, and a
  no-tool-call turn is every harness's end-of-run signal. The per-turn
  hazard compounds with run length, so failures cluster LATE — one specimen
  lost ~3 h of finished extraction work with only the git/PR/result.json
  endgame remaining. The only recourse was an attempt restart (new
  container, fresh clone), discarding all unpushed work. The grok
  non-progress halt (`15-errors-and-retries.md` §2b, `grok_loop_*` captures)
  lands as the identical signature and is covered by the same fix.
  Founder decisions (2026-08-02): the stall guard is escalate-only — the
  integer budget is the ONLY terminator; `max_continuations` is a plain
  integer with no low ceiling (10/50-continuation experiments are
  legitimate); plan mode is excluded; and the mechanism must be
  harness-neutral — grok is where the evidence came from, not what the
  design is for.

## Decision

### 1 — Trigger: exactly the row-9 landing, nothing else

The Dev entrypoint relaunches the harness **in the same container** only
when a run lands on ADR-0018's row 9: process exit 0, fault predicate None,
no valid canonical result.json, no recoverable stray. Every other terminal
path — crash (10), auth (12), clone (13), MCP (14), harness fault (15),
turn budget (16) — classifies exactly as before, *including on a
continuation invocation*: each invocation's stream is judged by the same
`harness_fault`/`classify_nonzero_exit` pipeline, so the fault machinery
outranks the loop even with budget left (a continuation that produces
literally nothing is `empty_completion` → exit 15, not another nudge —
correlation can excuse a backend fault; a nudge cannot).

### 2 — Three operations, one policy knob

| Operation | Session | Workspace | Container |
|---|---|---|---|
| **Session resume** | resumed via CLI flag | kept | kept |
| **Workspace continuation** | new | kept | kept |
| **Attempt restart** (pre-existing backstop, unchanged) | new | new | new |

`continuation_policy` (global, Limits & traffic): `auto` (default) —
resume when the harness has a verified resume, escalate PERMANENTLY to
fresh after a zero-progress continuation; `resume-only` — never fresh;
when resume is unavailable this **stops** (fails as today: the operator
explicitly forbade fresh); `fresh-only`; `off`. `max_continuations`
(default 2, `ge=0`, deliberately no upper bound) is the ONLY terminator.
Zero progress = identical workspace fingerprint (`git rev-parse HEAD` +
`git status --porcelain` hash) across consecutive landings; a fingerprint
of None (git failed) means *unknown* and never escalates. Plan mode never
continues (read-only; its result.json is synthesized). The
`dev_timeout_minutes` watchdog is unchanged and needs no loop awareness —
a continuation killed mid-flight is a DEV_TIMEOUT like any long run.
Attempt machinery is untouched: continuations happen inside ONE run;
`attempt_of_step` and `counts_toward_attempts` never see them.

### 3 — Resume dialects are capture-verified registry facts

`RESUME_SPECS` (argv.py, beside `HARNESSES`) holds one `ResumeSpec` per
harness that has a committed `<harness>_resume_nudge` capture pair — the
only road in. Phase-0 measurements (all three CLIs compose headless
resume; none forks the session; none replays history):

| harness | composition | usage on a resumed terminal event |
|---|---|---|
| grok-build 0.2.117 | `-p P -r SID` | per-invocation → sum |
| claude-code 2.1.210 | `-p P --resume SID` | per-invocation → sum |
| codex 0.144.4 | `exec resume THREAD P` | **cumulative** → last-wins |

`usage_cumulative` is a per-harness fact, measured not assumed — a global
flag would have double-counted codex or under-counted the others.
`harness_resume_argv` mirrors `harness_argv` (same output format and
approval flags per arm, extras last) but has NO default-harness fallback:
an unverified resume flag on the wrong CLI is a crash, not a default. A
new harness gets workspace continuation for free the day it exists and
session resume by adding one `ResumeSpec` plus one rig capture
(`in_container.py --resume-prompt-file`).

### 4 — Nudges never restate the result.json shape

The per-type contracts carry required fields beyond outcome/summary
(REVIEW's `verdict`/`report_md`, EXECUTE's `pr_url`, ONBOARD's
decomposition payload); a reduced `{outcome, summary}` example would pass
the entrypoint's first-line check and then fail app-side finalization as a
post-hoc DEV_BAD_OUTPUT. So the nudge names the canonical path, the legal
outcome set, the "do NOT end your turn until the file exists" imperative,
a PR-duplication guard — and points back to the **Required output section
of the original mission instructions** for the exact shape. Resume mode:
that section is already in the session. Fresh mode: the ORIGINAL prompt
rides verbatim inside the nudge — which is also how ADR-0016's frozen
assembly (identifying prompt + playbook + skills block) survives: nudges
are a runtime continuation layer, not a fourth assembly layer.

### 5 — Aggregation: a zero-continuation run is byte-identical

Per-invocation token reports merge via `merge_token_reports` (sum across
chains and within non-cumulative chains; last-wins inside a cumulative
chain); session ids are tracked as chains (`record_session`/`export_sids`
— fork-tolerant, though no fork was measured); grok dump segments are
per-chain (a resumed export is cumulative and REPLACES its chain's
segment), claude/codex per-invocation; `merged_transcript_dump` labels
segments only when there are two or more. Every merge returns its single
input unchanged, so runs that never continue produce today's payloads
byte-for-byte. Each invocation's `out` is released as before — what
accumulates is bounded by the operator's budget.

### 6 — Surfacing

`continuations_used` rides every artifact payload (success and failure),
persists on the Run, appears in the Runs API list/detail, adds a
`continuations: N` line to the feed token report (above the `run:` footer,
only when > 0 — old reports render byte-identically), and lands on OTel as
`devcake.continuations` (Dev- and app-side). The live run terminal shows a
boundary line per relaunch: `[devcake] continuation N/M (resume|fresh):
<reason> — relaunching <harness>`.

### 7 — Doctrine amendments

`15-errors-and-retries.md` §2's closing invariant — "Retries of Dev work
are never in-place" — gains an explicit carve-out: a continuation is
in-place BY DESIGN and is not a retry of a failed attempt; it happens
before the attempt fails, and only on a clean exit with no fault, so a
crashed or faulted container still dies exactly as before. Exit 11 now
means "unfinished after the continuation budget" whenever the policy is
on. The misplaced-result freshness gate stays pinned to the FIRST launch,
so a stray written by continuation N−1 remains adoptable at landing N.

## Consequences

- A narrate-and-stop landing costs one nudge instead of the whole run; the
  late-run failure mode (hours of work, endgame missing) becomes cheap.
- Every relaunch resets the CLI's own `--max-turns`, so the effective turn
  budget is (continuations + 1) × max-turns — stated in the config help.
- The 20k relay-line flood cap is now shared across all invocations of a
  run; the fingerprint's blind spot (edits to an already-dirty file) can
  cost one unnecessary resume→fresh escalation, never a stop.
- grok stream drift observed while capturing (0.2.117 says `end_turn`
  where the 0.2.112 fixtures say `EndTurn`, plus new `available_commands`/
  `usage` event types): harmless today — nothing branches on stopReason
  (`GROK_FAULT_STOP_REASONS` is empty by design) and unknown event types
  are skipped — but it is the `08` §1 unpinned-CLI caveat in action.
- A run killed by the watchdog mid-continuation posts no artifacts, so its
  continuation count survives only in relay lines (as any killed run).
- Rollout: PR-1 = TURN_DISCIPLINE prompt epilogue + exit-11 terminal
  evidence; Phase 0 = resume capture pairs for all three harnesses;
  PR-2 = the loop, config, surfacing, docs.

## Related

- Implement: `images/common/devcake_dev/harness/continuation.py`,
  `harness/argv.py` (`RESUME_SPECS`, `harness_resume_argv`),
  `workspace/forensics.py` (`workspace_fingerprint`),
  `images/common/dev_entrypoint.py` (the loop), `app/devcake/config.py`,
  `domain/orchestrator/dispatch.py`/`mapper.py` (env),
  `domain/orchestrator/finalize.py` + `api/runs_service.py` (surfacing),
  `admin/spa` LimitsSection/configLabels.
- Evidence: `app/tests/fixtures/harness_streams/*_resume_nudge*` (the
  Phase-0 pairs), `grok_loop_*` (the trigger shape),
  `app/tests/test_entrypoint_continuation.py`.
- Operator: `11-admin-panel.md` (Limits & traffic), `07-dev-runtime.md`
  §§3-5, `08-harness-templates.md` §§1,5,6, `15-errors-and-retries.md`
  §§1-2.
