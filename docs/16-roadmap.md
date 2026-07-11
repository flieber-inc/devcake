# 16 — Roadmap: Milestones M0–M7

> **Audience:** the implementing agent(s) and the founder. Each milestone leaves a demoable, committed system; exit criteria are mechanically checkable.
> Format per milestone: Goal · In scope (docs it implements) · Out of scope · Exit criteria · Demo.

## M0 — Compose skeleton + observability spine

**Goal:** all five services up, traced, healthy. **Implements:** `13` (compose), `12` §1 (pipeline), `11` shell.
**Out of scope:** any business logic.

Exit criteria — **all verified 2026-07-11 (M0 complete)**:
- [x] `docker compose up -d` from a fresh clone + `.env` → all services healthy.
- [x] `app` emits a stub `poll.cycle` trace visible in OpenObserve (verified via `_search?type=traces`).
- [x] Admin panel serves the three tabs; the Executor and Logs tabs' buttons open the Dagu and OpenObserve UIs in new browser tabs (confirmed decision: buttons, no iframes). Basic auth verified: 401 without credentials on both the SPA and `/api`.
- [x] Container stdout of `dagu`/`redis` searchable in OpenObserve (`container_logs` stream, via fluent-bit + fluentd logging driver).

**Demo:** open the admin panel, click through the three tabs, show the trace.

## M1 — Hello-world Dev

**Goal:** the full dispatch mechanics with a stub Dev. **Implements:** `09`, `13` §4–5 (`dev-run` DAG), `07` skeleton (entrypoint contract, exit codes), Dagu adapter.
**Out of scope:** real harnesses, PMO.

Exit criteria — **all verified 2026-07-11 (M1 complete)**:
- [x] `POST /api/v1/debug/dispatch-hello` → app triggers Dagu (non-secret params + client `dagRunId`) → a stub Dev container joins `devcake_default`, fetches its run spec via `runspec.get`, sends `run.started` + heartbeats + `run.artifacts` ("hello") over Redis → app consumes, writes a Run file with state `finished`.
- [x] One linked trace spans dispatch → container → finalize (verified: `mission.dispatch`, `dev.run`, `harness.exec`, `run.finalize` under one trace_id across `devcake-app` and `devcake-dev`).
- [x] Watchdog kill via the Dagu stop endpoint → Run `timed_out`, container force-removed, Dagu status `aborted`; duplicate trigger returns 409 `already_exists`; chunked artifact reassembly verified with a 718 KB payload.
- [x] Secrets probe: the fake run-spec secret never appears in Dagu's run API; `runspec.result` gone from Redis; the per-run ACL user revoked at finalization.

Field notes folded into `13` §4: Dagu API auth (basic mode), the `DOCKER_GID` repair hook, step-id charset, and `retry_policy: {limit: 0}` (Dagu auto-retry would fight DevCake's attempt counting).

## M2 — Linear read path + domain model

**Goal:** the world becomes visible. **Implements:** `02`, `05` (read side + label bootstrap), poll loop of `04` §1 (no dispatch).

Exit criteria — **all verified 2026-07-11 against the live sandbox team (M2 complete)**:
- [x] With `scripts/seed_sandbox.py` fixtures, `GET /api/v1/missions` derives **every row** of the derivation table correctly — incl. conflict, SKIP, FAILED, in-progress-without-label, awaiting-merge, and the opt-in gate (the team's pre-existing issues were correctly not adopted: the backlog-stampede protection observed live).
- [x] The nine labels auto-created idempotently on startup — in both Linear namespaces (team issue labels AND workspace project labels, a separate entity — `05` §5).
- [x] The project fixture normalizes (status/priority/project-labels) and derives per ADR-0006.
- [x] PMO adapter contract battery 1–5, 8–10 (`scripts/contract_tests_pmo.py`, run in-container against the sandbox): 8/8 pass.

Field notes folded into `05` §5: project labels are workspace-level (`projectLabelCreate`); Linear's ~10k query-complexity budget forces split queries.

## M3 — Scheduler + real Dev runtime + ONBOARD

**Goal:** first real autonomous step. **Implements:** `04` (full), `07` (full), `08` (`claude-code` template), `03` §1 (all three ONBOARD paths *dispatched*; normal path *finalized* end-to-end).

Exit criteria — **all verified 2026-07-11 against live Linear + live Claude Fable runs (M3 complete)**:
- [x] DEV-17 autonomously gained: `In Progress`, a `2_ONBOARD.md` transcript, a real token report (**$0.2239**, full cache-aware usage via `session_json`), and — via the **opportunistic-plan variant** — an uploaded `PLAN_2.md` attachment + `DEVCAKE-EXECUTE` (PLAN step skipped). The plain `DEVCAKE-PLAN` branch shares the same verified swap mechanism.
- [x] Extra CLI args flow from `assignments` through the run spec into the invocation (`--max-turns 15` delivered; the triage run used 9 turns).
- [x] Concurrency: cap 1 + three missions created in reverse priority order → dispatched urgent→high→low with zero overlap (perfect serialization observed).
- [x] Crash tests: mid-run `docker kill` → status restored, re-dispatched attempt 2 (DEV-22); three dispatch failures against a broken image → `DEVCAKE-FAILED` + comment at zero token cost (DEV-21); fast kill detection re-verified at **33 s** after fixing the first-heartbeat blind spot.
- [x] Compare-and-transition: human added `DEVCAKE-PLAN` mid-run (DEV-24) → artifacts posted, **no transition**, explanatory comment.
- [x] Startup reconciliation: app restarted mid-run → `adopted in-flight run … (dagu: running)` → finalization completed end-to-end (DEV-22).

Field lessons folded into docs 04/07/14/15: Claude Code refuses `--dangerously-skip-permissions` as root (Dev images run fully non-root); the **failure-symmetry rule** (dispatch-time status writes are reverted on failed attempts, else ONBOARD strands at derivation row 9); watchdog liveness references `last_heartbeat or started_at` + immediate first heartbeat; attempt counters restart via a give-up watermark.

## M4 — PLAN + EXECUTE + forge (GitHub)

**Goal:** code flows. **Implements:** `03` §§2–3, `06` (GitHub), `08` (`grok-build` template — invocation, auto-approval, plan mode, MCP syntax, and the `signals.json` token-totals extraction all verified on the pinned CLI v0.2.93).

Exit criteria:
- [ ] A `DEVCAKE-PLAN` mission autonomously: produces `PLAN.md`, uploads it, swaps to `DEVCAKE-EXECUTE`; then Main Dev implements on `devcake/{mission_key}`, opens a PR on the sandbox repo, label swaps to `DEVCAKE-REVIEW`, PR link posted.
- [ ] Token reports present for both steps (Claude with full usage+cost; Grok with `signals.json` totals per `08` §5 — and the `unavailable` fallback proven by fault injection).
- [ ] Re-running EXECUTE (simulated crash after push) reuses the branch, updates the same PR (idempotent `ensure_pr`), never force-pushes.
- [ ] Transcript > 50 KB uploads as an `.md` attachment (PMO contract test 6).

## M5 — REVIEW + full loop + failure taxonomy

**Goal:** the complete state machine. **Implements:** `03` §§1.1, 1.3, 4–5, `15` (full), `04` §5 watchdog timeout, decomposition + `DEVCAKE-TRACKING` sweep.

Exit criteria — scripted scenarios all green:
- [ ] **Approve path:** REVIEW → PR review comment with the concrete copy-pasteable approval footer → formal approval when a reviewer token is configured → with `auto_merge` on: merged (squash) **then** Done; with it off: `DEVCAKE-MERGE`, then the merge sweep marks Done after a human merges (and Canceled when the PR is closed unmerged) — Done never precedes the merge.
- [ ] **Reject path:** review report posted to feed + PR; label back to `DEVCAKE-EXECUTE`; second EXECUTE reworks the same branch; the 3rd rejection posts the loop warning to feed + PR with cumulative cost.
- [ ] **Trivial ONBOARD:** PR opened + `DEVCAKE-REVIEW` added (trivial path never skips REVIEW); the mission reaches Done only through the approve path above.
- [ ] **Decomposition:** high-complexity issue → standalone children with priorities + `DEVCAKE-CREATED`, original canceled; a `DEVCAKE-CREATED` child returning `decomposed` is rejected (depth limit). Project variant → children in project, `DEVCAKE-TRACKING`, auto-completed when children done.
- [ ] **Timeout:** a run exceeding a 2-minute test timeout is killed by the app watchdog and counted.

## M6 — Admin Config tab + credentials + GitLab

**Goal:** operable by a stranger. **Implements:** `11` (full), `08` §4 (credential modes incl. JSON upload + OAuth flows documented in-UI), `06` (GitLab), `14` §3.

Exit criteria:
- [ ] From an empty `/data`, a fresh operator configures everything through the UI alone (connection tests green, Dev Types created, assignments set) and reproduces the M3 demo without touching files.
- [ ] Credentials JSON upload lands 0600, mounts read-only, works end-to-end for at least one harness (subscription OAuth path).
- [ ] GitLab adapter passes the forge contract battery; the M4 demo passes on a GitLab sandbox repo.
- [ ] `auto_merge` and `adoption_mode` confirm dialogs + `DEVCAKE-SKIP` behavior verified in UI-driven tests; basic auth gates both the SPA and `/api` (unauthenticated requests get 401).

## M7 — Hardening + acceptance

**Goal:** v0 done. **Implements:** `14` §5 (redaction), `15` §6 (alerts), `12` §5 (dashboards), remaining reconciliation/reclaim paths, README quickstart truth-check.

Exit criteria:
- [ ] **Acceptance script** (`scripts/acceptance.py`): seeds sandbox Linear team + sandbox repo, then asserts the golden path — Backlog issue → ONBOARD → PLAN → EXECUTE → REVIEW → Done with approved PR — completes **unattended**, twice consecutively.
- [ ] Every invariant INV-1…6 (`00-overview.md` §4) is referenced by at least one automated test.
- [ ] Redaction filter proven: a planted fake secret in a transcript never reaches Linear or OTLP.
- [ ] Redis reclaim (XAUTOCLAIM) and poison-message paths exercised by tests.
- [ ] README quickstart verified on a clean machine.
- [ ] **CI economics:** the M1 stub-harness image is kept as a permanent fixture; CI runs the full acceptance *logic* against it (deterministic, free, fast). The real-model acceptance script is a manual pre-release gate, not a per-commit CI job.

## Post-v0 backlog

Webhook ingestion (Linear `watch()` receiver + tunnel guide) · multiple repositories / multi-team · additional PMO adapters (GitHub Issues, GitLab, Monday) · **priority-conditional Dev Type assignment** (e.g. Urgent missions route EXECUTE to Senior Dev — relaxes the strict 1 Mission Type → 1 Dev Type rule; deemed too much for v0) · **Scout Dev experiment** (route ONBOARD to a cheap-model Dev Type via the admin panel — zero code changes required; evaluate decomposition quality for a week before adopting) · admin panel OIDC/SSO (v0 has basic auth) · per-run scoped forge tokens & the rest of `14` §7 · OTel collector insertion · mid-run Dev→PMO write relay commands · SQLite `StatePort` swap if run history outgrows files.

*(Note: `auto_merge` was originally slated post-v0 but is a confirmed v0 requirement — it ships in M5/M6.)*
