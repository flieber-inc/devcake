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

Exit criteria:
- [ ] Against a seeded sandbox Linear team (fixture script committed under `scripts/seed_sandbox.py`), `GET /api/v1/missions` returns correctly derived Mission Types for **every row** of the derivation table (`02` §2), including conflict, SKIP, FAILED, and in-progress-without-label rows.
- [ ] The nine labels are auto-created in the sandbox team on startup, idempotently.
- [ ] Projects normalize (status/priority/labels) and derive per ADR-0006.
- [ ] PMO adapter contract tests 1–5, 8–10 (`05` §7) pass.

## M3 — Scheduler + real Dev runtime + ONBOARD

**Goal:** first real autonomous step. **Implements:** `04` (full), `07` (full), `08` (`claude-code` template), `03` §1 (all three ONBOARD paths *dispatched*; normal path *finalized* end-to-end).

Exit criteria:
- [ ] A Backlog issue in the sandbox team autonomously gains: `In Progress` status, a `1_ONBOARD.md` transcript, a token report comment **with real token counts and `total_cost_usd`**, and the `DEVCAKE-PLAN` label — no manual steps. Variant: an ONBOARD run that attaches an opportunistic `PLAN.md` lands on `DEVCAKE-EXECUTE` with the plan uploaded (`03` §1.2).
- [ ] The per-Mission-Type extra CLI args from `assignments` reach the harness invocation verbatim (verify `--max-turns` visibly bounds the ONBOARD session).
- [ ] Concurrency caps enforced: with per-type cap 1 and three eligible missions, exactly one Dev runs; priority order (incl. unset→Medium) decides which.
- [ ] Scripted crash test: kill the Dev mid-run → label untouched → re-dispatched next cycle with `attempt_of_step=2`; after 3 scripted failures → `DEVCAKE-FAILED` + comment.
- [ ] Scripted compare-and-transition test: change the mission's label mid-run by hand → finalization posts artifacts but applies no transition, posts the explanatory comment.
- [ ] Startup reconciliation: restart the app mid-run → run adopted, finalization completes.

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
