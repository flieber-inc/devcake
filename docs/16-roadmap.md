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
- [x] `POST /api/v1/debug/dispatch-hello` → app triggers Dagu (non-secret params + client `dagRunId`) → a stub Dev container joins `devcake_runtime`, fetches its run spec via `runspec.get`, sends `run.started` + heartbeats + `run.artifacts` ("hello") over Redis → app consumes, writes a Run file with state `finished`.
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

Exit criteria — **all verified 2026-07-11 against live Linear + GitHub + live Grok/Claude runs (M4 complete)**:
- [x] DEV-18 ran the full chain autonomously: PLAN (Claude plan mode; entrypoint materializes `PLAN.md` + `result.json`, since plan mode is read-only) → plan uploaded → `DEVCAKE-EXECUTE` → Main Dev (Grok Build, OAuth subscription session) implemented on `devcake/DEV-18`, opened PR#3 → `DEVCAKE-REVIEW` + PR link posted. DEV-17 ran the direct-EXECUTE variant (opportunistic plan from M3) → PR#2 with a 23-test pytest suite.
- [x] Token reports for both harnesses: Claude full usage+cost (`session_json`); Grok totals via `signals.json` (`total: 22006` / `26892` posted to the feed).
- [x] Rework idempotency: swapping DEV-17 back to `DEVCAKE-EXECUTE` produced a run that reused the branch, reported the **same** PR#2 (still the only open PR for it, head sha unchanged), and swapped back to REVIEW. No duplicates, no force-push.
- [x] A forced 60 KB transcript took the attachment route: uploaded `.md` + short reference comment (and exposed a trailing-paren asset-URL regex bug, fixed).

Operational note: widening the dispatch gate immediately picks up ANY adopted mission already carrying a stage label — as designed, but remember it when seeding sandboxes (the M2 fixtures dispatched and one produced a PR before being parked).

## M5 — REVIEW + full loop + failure taxonomy

**Goal:** the complete state machine. **Implements:** `03` §§1.1, 1.3, 4–5, `15` (full), `04` §5 watchdog timeout, decomposition + `DEVCAKE-TRACKING` sweep.

Exit criteria — **all verified 2026-07-11 (M5 complete; live where organic, direct-fired where the model wouldn't misbehave on demand)**:
- [x] **Approve paths, all three:** auto_merge ON → merged **then** Done (DEV-18/PR#3, DEV-25/PR#4, DEV-17 after conflict rework); auto_merge OFF → `DEVCAKE-MERGE` + APPROVED-BY-DEVCAKE marker + copy-pasteable footer (DEV-17 first pass); merge sweep verified on both branches — merged→Done (direct-fired against a genuinely merged PR) and closed-unmerged→Canceled (DEV-26/PR#5, live). Bonus live finding: **auto-merge conflict fallback** correctly lands on `DEVCAKE-MERGE` + explanation instead of a hollow Done (DEV-26).
- [x] **Reject path:** direct-fired (organic reviews kept approving genuinely good work): report to feed + PR, label back to EXECUTE, loop warning with **cumulative cost** at round 3 — which caught and fixed a real crash (Run never persisted token_report). Live rework loop proven via DEV-17's conflict cycle: EXECUTE reused the branch, re-REVIEW, auto-merge, Done.
- [x] **Trivial ONBOARD:** DEV-25 ran the whole chain live: trivial verdict → typo-fix PR#4 → `DEVCAKE-REVIEW` (never skipped) → approve → auto-merge → Done.
- [x] **Decomposition:** project variant fully organic — 5 standalone children created in-project with priorities + `DEVCAKE-CREATED` + `DEVCAKE` + provenance footers, `DEVCAKE-TRACKING` applied, and the tracking sweep auto-completed the project once children finished. Issue variant + depth limit direct-fired (children + cancel; grandchild refused, child parked). Honest note: the "high-complexity" *issue* fixture was legitimately judged cohesive by the model and routed plan→execute — decomposition triggering is model judgment; the machinery is what we verify.
- [x] **Timeout:** verified at M1 (15 s) and M3 (wall-clock kill); not re-run.
- [x] **En route:** give-up → `DEVCAKE-FAILED` on the project after 3 attempts, human label-removal restarting the attempt watermark, and the `DEV_AUTH` breaker implemented (not live-fired — an M7 fault-injection target).

## M6 — Admin Config tab + credentials + GitLab

**Goal:** operable by a stranger. **Implements:** `11` (full), `08` §4 (credential modes incl. JSON upload + OAuth flows documented in-UI), `06` (GitLab), `14` §3.

Exit criteria — **verified 2026-07-11 in a real browser except where noted (M6 functionally complete)**:
- [x] Config tab CRUD live: Vite/React/Tailwind SPA renders real config; PMO/forge connection tests green from the UI ("✓ team DEV: 9/9 labels", "✓ github reachable"); saves flow UI→PUT→YAML with hot apply; Dev Type cards with prompts/MCP-warning/credentials; assignment matrix with extra-args + harness-change dialog. *Fresh-operator-from-empty-`/data` run deferred to M7 acceptance (destructive on the live volume).*
- [x] **GUI OAuth wizard** (founder request): device-code flow runs in a Dagu-spawned harness container, streams the URL + code over Redis into a React modal (observed live: `accounts.x.ai/oauth2/device` + code rendered), polls to completion; the storage tail (file 0600, session completed, `DEV_AUTH` breaker cleared) synthetic-verified. Sessions are in-memory: an app restart orphans a pending wizard (dialog reports it; just retry).
- [x] Credentials JSON upload endpoint verified (0600 under `/data/secrets/{dev_type}/`, delivered per-run via runspec — the bind-mount wording predates the M1 runspec redesign); subscription OAuth end-to-end proven for Grok since M4.
- [x] **GitLab verified live (2026-07-11)**: with the operator's sandbox (`gitlab.com/fidecastro/devcake-test`, protected `main`), mission DEV-35 ran the full autonomous lifecycle — ONBOARD → EXECUTE (Grok driving `glab`, forge-aware clone auth + MR playbook) → MR!1 → REVIEW approve → **auto-squash-merge on the protected branch** → Done. Config hot-switched GitHub↔GitLab through the admin API both ways (forge factory reload).
- [x] `auto_merge` + `adoption_mode` confirm dialogs verified by browser automation (incl. Cancel preserving state); `DEVCAKE-SKIP` precedence verified throughout M3–M5; basic auth 401s on both SPA and `/api` (M0, re-checked).

## M7 — Hardening + acceptance

**Goal:** v0 done. **Implements:** `14` §5 (redaction), `15` §6 (alerts), `12` §5 (dashboards), remaining reconciliation/reclaim paths, README quickstart truth-check.

Exit criteria — **all verified 2026-07-11. M7 complete — v0 SHIPPED.**
- [x] **Acceptance:** `scripts/acceptance.py` ran the golden path unattended **2/2** (DEV-36, DEV-37): fresh Backlog issues → autonomous triage/implementation/review → PRs #6 and #7 merged → Done, with 3 transcripts + 3 token reports each and REVIEW never skipped.
- [x] INV-1…6 each referenced by named automated tests (`app/tests/`, 26 tests: derivation table, ACL isolation + forged-auth drop, compare-and-transition + failure restore, token-report-always, playbook binding rules, redaction).
- [x] Redaction live-proven: a planted `ghp_…` token and a per-run Redis password reached Linear as `«REDACTED»`; the filter wraps every PMO- and forge-bound write.
- [x] Reclaim exercised (startup XAUTOCLAIM at M3 + re-proven post-drill); poison path implemented + hermetically tested (5 deliveries → `devcake:dead` + ack).
- [x] README quickstart corrected against reality (images build, DOCKER_GID, OAuth wizard); full clean-machine walk remains a release ritual.
- [x] Tutorials re-walked and corrected post-M6 (OAuth wizard as primary login, Config-tab reality).
- [x] **CI economics:** `scripts/ci_suite.sh` = 26 tests + stub-harness pipeline smoke, deterministic and model-free (~1 min); `scripts/acceptance.py` is the manual, token-spending pre-release gate.
- [x] **Fresh-`/data` operator drill** (carried from M6): volume backed up, wiped, first boot re-seeded config + dev types from env, ensured labels idempotently, health all green with empty secrets awaiting the operator's OAuth click; backup restored intact and CI green after the round trip.
- [x] OpenObserve **DevCake dashboard** provisioned via API (cost/hour by dev type, runs by outcome, failure signals); alert provisioning ships in `scripts/provision_oo.py` (activates when `OO_ALERT_WEBHOOK` is set).

## Post-v0 shipped

- **2026-07-12 — Traffic control (`adr/0007`):** Mission ordering via native `blocked by` relations (ONBOARD decomposition declares `blocked_by`; scheduler gate honors any relation, human-added included) · `DEVCAKE-NEEDS-HUMAN` hand-off label + `human_needed` outcome (tenth label) · intake pause toggle (`intake_paused` + admin Traffic control section) · comment-provenance sentinel `` `devcake:v1` `` with 🧑/🤖 markers in `ACTIVITY.md` · Relations Mapper service (`MAPPER` run kind: interval + manual trigger + Dev Type combobox + on/off in the admin panel). Requires a dev-image rebuild (new legal outcomes in the entrypoint).
- **2026-07-12 — Traffic-control hardening (`adr/0007` addendum), same day, post-adversarial-review:** app-side `LEGAL_OUTCOMES` trust boundary · branch-protection verification + out-of-pipeline-merge tripwire (docs/13 §8a, docs/14 §2) · paginated Linear reads + relations-page warnings · `gate_map` as an always-fresh poll artifact + dependency-cycle detection with header banner · hand-off evidence requirement + escalating warnings (never auto-park) · seeded `junior-dev` + `MapperService` (lock, post-success watermark, store-derived degradation) · quote-aware sentinel classification · project-update baton passes (verified live) · stateful pause banner with in-flight count · config deep-merge + mapper Dev Type delete guard.

- **2026-07-13 — Modularization (`adr/0008`):** the hexagonal layout is now real (`app/devcake/` with `domain/`, `ports/`, `adapters/`, `api/`; `domain/*` has zero runtime adapter imports) · pluggable PMO + forge adapter registries (`adapters/registry.py` — `system`/`forge` are registry-validated open strings, not literals) · config schema v2: plural `pmos:`/`repos:` with exactly-one enforced, v1→v2 migrated on load with a `config.yaml.v1.bak` backup · MissionRef-unified `PMOPort` + typed `ForgePort` DTOs (`PullRequest`, `ForgeDescriptor`, `mission_branch()`) · registry-fed admin Config tab (`GET /api/v1/connections/registry`) with config hot-reload on PUT.

- **2026-07-12 — Harness registry (admin authoritative):** found live — changing a Dev Type's harness in the admin panel didn't change what ran (dispatch used the stored `docker_image`; harness selection was image-baked). Reworked: `app/devcake/harness.py` registry is the single source of truth (image + credential requirements + OAuth flow per `harness_template`); `DevType` slimmed (no stored image/credential config; legacy YAML keys dropped on next save); dispatch sends `DEVCAKE_HARNESS` in the run spec (overrides the baked ENV); OAuth became per-Dev-Type (`POST /oauth/dev-types/{name}/start` — fixes credentials landing in the first same-harness Dev Type's dir); Dev Type card shows the derived image + live credential checklist (`GET /harnesses`, enriched `GET /dev-types`).

## Post-v0 backlog

Webhook ingestion — a PMO `watch()`/webhook `ChangeEvent` seam replacing polling (+ tunnel guide) · multi-instance **runtime** wiring — the config schema is already plural (`pmos:`/`repos:`); what's missing is per-mission adapter resolution + per-instance wiring · additional PMO adapters (GitHub Issues, GitLab, Monday) · `PMOPort.cancel_mission()` · forge capabilities negotiation (`ForgeCapabilities`) · a local forge (Gitea) adapter · `ExecutorPort`/`StatePort` formalization (the `dagu/`, `files/`, `redis/` adapters are already packaged under `adapters/`; their ports are not yet Protocols) · **priority-conditional Dev Type assignment** (e.g. Urgent missions route EXECUTE to Senior Dev — relaxes the strict 1 Mission Type → 1 Dev Type rule; deemed too much for v0) · **Scout Dev experiment** (route ONBOARD to a cheap-model Dev Type via the admin panel — zero code changes required; evaluate decomposition quality for a week before adopting) · admin panel OIDC/SSO (v0 has basic auth) · per-run scoped forge tokens & the rest of `14` §7 · OTel collector insertion · mid-run Dev→PMO write relay commands · SQLite `StatePort` swap if run history outgrows files.

*(Note: `auto_merge` was originally slated post-v0 but is a confirmed v0 requirement — it ships in M5/M6.)*
