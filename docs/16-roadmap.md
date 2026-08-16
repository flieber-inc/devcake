# 16 — Roadmap

> **Audience:** the implementing agent(s) and the founder. Each milestone leaves a demoable, committed system; exit criteria are mechanically checkable.
> Format per milestone: Goal · In scope (docs it implements) · Out of scope · Exit criteria · Demo.
>
> **How this document is layered** (oldest → newest within each layer):
>
> | Layer | Sections | Status |
> |---|---|---|
> | **1. Milestone era** | M0–M12 (+ M7.1, F1–F5) | **Frozen** — planned work with exit criteria; the product was built this way |
> | **2. Closed releases** | v0.1 FINAL, v0.2 FINAL, feature stream through the v0.2 tag | **Frozen** — release narratives + inventory of what fed those tags |
> | **3. Living log** | After v0.2 only | **Open** — shipped since the last closed cut, residuals, candidates, deferred |
>
> Layers 1–2 are history. Layer 3 is the only place new work is appended.
> Candidates (e.g. harness platformization H1–H5) are not a committed sprint
> queue unless the founder picks them up.
>
> **Status vocabulary** (whole file): **built** = merged, full suite + CI green ·
> **live-verified (date)** = exercised on the live stack with evidence ·
> **⏳ live-pending** = built, live end-to-end still owed ·
> milestone `[x]` checkboxes mark exit criteria verified at that milestone's close.
>
> **Where we are (2026-08-04):** layers 1–2 closed through tag **v0.2**;
> release tags through **v0.2.5 "Hummingbird"** on `main`. The product loop
> (poll → dispatch → harness → finalize → forge/PMO) is operational with three
> harness templates (`claude-code`, `grok-build`, `codex`, `pi`, `opencode`, `qwen-code`), multi-PMO /
> multi-repo / internal Gitea, skills (ADR-0016), settings profiles/export
> (ADR-0013), fault classification + backend brake (ADR-0018/0026), mandatory
> source mirrors + provisioned workspaces (ADR-0024/0025), in-container
> continuation (ADR-0022), and the 2026-08 evaluation fix campaign (spend
> discipline, TOCTOU guards, composition-root tests, ops hardening — entries
> below). Unit suite is on the order of **~1400+** tests (1421 counted
> 2026-08-04; do not treat any single count as a permanent claim).
> Ongoing append-only tracking: [Living log (after v0.2)](#living-log-after-v02).

## History — Layer 1: Milestone era (M0–M12)

## M0 — Compose skeleton + observability spine

**Goal:** all five services up, traced, healthy. **Implements:** `13` (compose), `12` §1 (pipeline), `11` shell.
**Out of scope:** any business logic.

Exit criteria — **all verified 2026-07-11 (M0 complete)**:
- [x] `docker compose up -d` from a fresh clone + `.env` → all services healthy.
- [x] `app` emits a stub `poll.cycle` trace visible in OpenObserve (verified via `_search?type=traces`).
- [x] Admin panel serves the three tabs *(M0-era layout; current SPA is seven pages — Overview, Missions, Runs, Repositories + PMO (under Adapters), Config, Consoles — `11-admin-panel.md`)*; the Executor and Logs tabs' buttons open the Dagu and OpenObserve UIs in new browser tabs (confirmed decision: buttons, no iframes). Basic auth verified: 401 without credentials on both the SPA and `/api`.
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
- [x] The nine labels auto-created idempotently on startup — in both Linear namespaces (team issue labels AND workspace project labels, a separate entity — `05` §5). *(Historical count: the managed set grew to ten with `DEVCAKE-NEEDS-HUMAN`, `adr/0007`.)*
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

**Goal:** code flows. **Implements:** `03` §§2–3, `06` (GitHub), `08` (`grok-build` template — invocation, auto-approval, plan mode, MCP syntax, and the `signals.json` token-totals extraction all verified on the observed CLI v0.2.93; the installer itself was and remains unpinned).

> **Version note (2026-07-25):** the v0.2.93 statements in this section are what M4 verified *then*, and stand as milestone history. They are no longer the current picture — grok was re-captured at **0.2.112**, where the `end` event carries a full input/output usage split inline, so `signals.json` is no longer the only token source (and its own survival at 0.2.112 is itself unverified). `08-harness-templates.md` §1/§5 is the current record; read it before relying on any grok shape stated here.

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
- [x] Config tab CRUD live: Vite/React/Tailwind SPA renders real config; PMO/forge connection tests green from the UI ("✓ team DEV: 9/9 labels" — the managed set of the day; ten today, "✓ github reachable"); saves flow UI→PUT→YAML with hot apply; Dev Type cards with prompts/MCP-warning/credentials; assignment matrix with extra-args + harness-change dialog. *Fresh-operator-from-empty-`/data` run deferred to M7 acceptance (destructive on the live volume).*
- [x] **GUI OAuth wizard** (founder request): device-code flow runs in a Dagu-spawned harness container, streams the URL + code over Redis into a React modal (observed live: `accounts.x.ai/oauth2/device` + code rendered), polls to completion; the storage tail (file 0600, session completed, `DEV_AUTH` breaker cleared) synthetic-verified. Sessions are in-memory: an app restart orphans a pending wizard (dialog reports it; just retry).
- [x] Credentials JSON upload endpoint verified (0600 under `/data/secrets/{dev_type}/`, delivered per-run via runspec — the bind-mount wording predates the M1 runspec redesign); subscription OAuth end-to-end proven for Grok since M4.
- [x] **GitLab verified live (2026-07-11)**: with the operator's sandbox (`gitlab.com/fidecastro/devcake-test`, protected `main`), mission DEV-35 ran the full autonomous lifecycle — ONBOARD → EXECUTE (Grok driving `glab`, forge-aware clone auth + MR playbook) → MR!1 → REVIEW approve → **auto-squash-merge on the protected branch** → Done. Config hot-switched GitHub↔GitLab through the admin API both ways (forge factory reload).
- [x] `auto_merge` + `adoption_mode` confirm dialogs verified by browser automation (incl. Cancel preserving state); `DEVCAKE-SKIP` precedence verified throughout M3–M5; basic auth 401s on both SPA and `/api` (M0, re-checked).

## M7 — Hardening + acceptance

**Goal:** v0 done. **Implements:** `14` §5 (redaction), `15` §6 (alerts), `12` §5 (dashboards), remaining reconciliation/reclaim paths, README quickstart truth-check.

Exit criteria — **all verified 2026-07-11. M7 complete — golden path / pre-release evidence.**
(The v0 crystallization tag is **2026-07-13** — see below. 2026-07-11 is first golden-path pass evidence, not the v0 tag.)
- [x] **Acceptance:** `scripts/acceptance.py` ran the golden path unattended **2/2** (DEV-36, DEV-37): fresh Backlog issues → autonomous triage/implementation/review → PRs #6 and #7 merged → Done, with 3 transcripts + 3 token reports each and REVIEW never skipped.
- [x] INV-1…6 each referenced by named automated tests (`app/tests/`, 26 tests at M7 close: derivation table, ACL isolation + forged-auth drop, compare-and-transition + failure restore, token-report-always, playbook binding rules, redaction). *(The suite has since grown with every post-v0 round: 207 tests as of the 2026-07-13 crystallization.)*
- [x] Redaction live-proven: a planted `ghp_…` token and a per-run Redis password reached Linear as `«REDACTED»`; the filter wraps every PMO- and forge-bound write.
- [x] Reclaim exercised (startup XAUTOCLAIM at M3 + re-proven post-drill); poison path implemented + hermetically tested (5 deliveries → `devcake:dead` + ack).
- [x] README quickstart corrected against reality (images build, DOCKER_GID, OAuth wizard); full clean-machine walk remains a release ritual.
- [x] Tutorials re-walked and corrected post-M6 (OAuth wizard as primary login, Config-tab reality).
- [x] **CI economics:** `scripts/ci_suite.sh` = the full unit suite + stub-harness pipeline smoke, deterministic and model-free (~1 min); `scripts/acceptance.py` is the manual, token-spending pre-release gate.
- [x] **Fresh-`/data` operator drill** (carried from M6): volume backed up, wiped, first boot re-seeded config + dev types from env, ensured labels idempotently, health all green with empty secrets awaiting the operator's OAuth click; backup restored intact and CI green after the round trip.
- [x] OpenObserve **DevCake dashboard** provisioned via API (cost/hour by dev type, runs by outcome, failure signals); alert provisioning ships in `scripts/provision_oo.py` (activates when `OO_ALERT_WEBHOOK` is set).

## M7.1 — Post-v0 shipped

- **Docs — product security contract alignment:** `docs/14-security.md` rewritten as the normative contract (dedicated host, adult-operator prompt trust, supply-chain primary mitigation, warnings vs gates); cascaded into `00`, `17`, `README`, `13`, `01`, tutorials, and satellite docs. Claims must not exceed `14`.

- **2026-07-12 — Traffic control (`adr/0007`):** Mission ordering via native `blocked by` relations (ONBOARD decomposition declares `blocked_by`; scheduler gate honors any relation, human-added included) · `DEVCAKE-NEEDS-HUMAN` hand-off label + `human_needed` outcome (tenth label) · intake pause toggle (`intake_paused` + admin Limits & traffic section) · comment-provenance sentinel `` `devcake:v1` `` with 🧑/🤖 markers in `ACTIVITY.md` · Relations Steward service (`STEWARD` run kind: interval + manual trigger + Dev Type combobox + on/off in the admin panel). Requires a dev-image rebuild (new legal outcomes in the entrypoint).
- **2026-07-12 — Traffic-control hardening (`adr/0007` addendum), same day, post-adversarial-review:** app-side `LEGAL_OUTCOMES` trust boundary · branch-protection verification + out-of-pipeline-merge tripwire (docs/13 §8a, docs/14 §2) · paginated Linear reads + relations-page warnings · `gate_map` as an always-fresh poll artifact + dependency-cycle detection with header banner · hand-off evidence requirement + escalating warnings (never auto-park) · seeded `junior-dev` + `StewardService` (lock, post-success watermark, store-derived degradation) · quote-aware sentinel classification · project-update baton passes (verified live) · stateful pause banner with in-flight count · config deep-merge + steward Dev Type delete guard.

- **2026-07-12 — Harness registry (admin authoritative):** found live — changing a Dev Type's harness in the admin panel didn't change what ran (dispatch used the stored `docker_image`; harness selection was image-baked). Reworked: `app/devcake/harness.py` registry is the single source of truth (image + credential requirements + OAuth flow per `harness_template`); `DevType` slimmed (no stored image/credential config; legacy YAML keys dropped on next save); dispatch sends `DEVCAKE_HARNESS` in the run spec (overrides the baked ENV); OAuth became per-Dev-Type (`POST /oauth/dev-types/{name}/start` — fixes credentials landing in the first same-harness Dev Type's dir); Dev Type card shows the derived image + live credential checklist (`GET /harnesses`, enriched `GET /dev-types`).

- **2026-07-13 — Modularization (`adr/0008`):** the hexagonal layout is now real (`app/devcake/` with `domain/`, `ports/`, `adapters/`, `api/`; `domain/*` has zero runtime adapter imports) · pluggable PMO + forge adapter registries (`adapters/registry.py` — `system`/`forge` are registry-validated open strings, not literals) · config schema v2: plural `pmos:`/`repos:` with exactly-one enforced, v1→v2 migrated on load with a `config.yaml.v1.bak` backup · MissionRef-unified `PMOPort` + typed `ForgePort` DTOs (`PullRequest`, `ForgeDescriptor`, `mission_branch()`) · registry-fed admin Config tab (`GET /api/v1/connections/registry`) with config hot-reload on PUT.

- **2026-07-13 — v0 crystallization:** repo-wide cleanup before v0.1 work. Bug fixes (redaction-gap alarm on unreadable secrets files; datetime-safe attempt counting; `security.MASK` single-sourced; background-task death logging on config reload) · telemetry brought up to the "everything traced" invariant (spans now *cover* the PMO writes they name; new `ingress.handle`, `sweep.merge_retry`, `steward.periodic`, `ingress.forged_drop`, `ingress.poison` spans — `12` §2 is the normative inventory) · **all legacy/compat surfaces removed** (founder decision): the old-image protocol dual-modes (`DEVCAKE_FORGE` discriminator, `forge_dialect()` fallback, pre-marker decomposition regex) AND the v1→v2 data migrations (config auto-migration, run-record secret scrub, Redis legacy scrubs). Consequences: app + dev images MUST rebuild in lockstep (`13` §8); a v1 `config.yaml` is refused at boot with hand-migration instructions; pre-v2 run records quarantine at boot (`10` §5) · `blocked_reasons` exposed in `/health`; meaningless `config_valid` dropped · dead `admin/site/` shell deleted · docs re-baselined against the code.

- **2026-07-14 — ISSUES_LIST hardening + build overhaul:** the 38-item ISSUES_LIST review closed out (finalize-stall watchdog backstop, label-swap write-path pagination, all OO alerts backed by real spans, dismissable `/health` `security_warnings`, `domain/reconcile.py` extraction) · orchestrator god module split into the `domain/orchestrator/` package (ISSUES #36) · Docker Bake build system merged (bake-only images via `docker buildx bake all`, multi-stage Dockerfiles, GHA bake CI — collaborator contribution).

- **2026-07-14 — RunBootstrap + secondary ports (`adr/0008` follow-up, PR #1):** `ExecutorPort` / `StatePort` / `MessagingPort` / `RunFinalizer` Protocols under `ports/` · deep `domain/run_bootstrap.py` owns the dispatch spine (ACL → auth digest → durable `StatePort.save` → `ExecutorPort.start`) for all four flavors (hello, mission, steward, OAuth) · `RunManager.set_finalizer` breaks the concrete `mission_mgr` late-wire cycle · tests at `tests/test_run_bootstrap.py` · docs/01 §3 + docs/04 §3.1 re-baselined.

## v0.1 — feature specifications (F1–F5) + milestones M8–M12

*(Consolidated + triaged 2026-07-14, revised the same day after a devil's-advocate round, then recast as milestones. Standing premise: **there are no deployments** — safecontract was the founder's own test — so v0 parity is explicitly NOT preserved: schema v3 breaks wholesale, shims and fallback modes are deleted rather than deprecated. F1–F5 below are the feature specifications, in implementation order — agnosticism before multiplicity, multiplicity before the internal fallback forge's zero-repo payoff, GUI config last since every prior feature adds config surface it must cover. M8–M12 are the implementation plan in the M0–M7 format: each leaves a demoable, committed system with mechanically checkable exit criteria. The four hardening items triaged into v0.1 are folded into M8 (PR #1, ISSUES #13, #29) and M12 (ISSUES #30).)*

### v0.1 feature specifications

**F1 — Forge-agnosticism hardening.** Nothing forge-specific outside `adapters/` — DevCake must be completely forge-agnostic, and the known violations get corrected first, ahead of everything else. Residuals (audited 2026-07-14): `config.py` defaults (`forge: "github"`, `token_env: "GITHUB_TOKEN"`, github.com-specific URL validation) → derive from the adapter registry/descriptor; the `api/main.py` read-only-token security-warning copy hardcodes GitHub/GitLab wording → registry-fed; `ports/forge.py` default `git_email` (github noreply) → per-descriptor. The CI tripwire asserts on **behavior, not strings**: no `adapters.github`/`adapters.gitlab` imports outside the registry, all defaults resolve through descriptors (a forge-name literal grep is at most a secondary check with an explicit allowlist — comments and docstrings legitimately name forges). `ForgeCapabilities` is deliberately *not* designed here — it gets extracted from real divergence during F4; designing capability negotiation before a third forge exists is speculation.

**F2 — PMO port completion: multi-PMO, additive.** Segregate Linear fully and make DevCake PMO-independent; one instance oversees N≥1 PMO systems at once. Schema v3 is a **wholesale redesign, not a validator relaxation**: `pmos:`/`repos:` become instances-with-identities (operator-chosen instance names), `MissionRef` carries PMO-instance provenance end-to-end, and `mission_branch()` prefixes the instance name (`LINEAR-DEV-17`) so identifiers can never collide across PMOs; the singular `config.pmo`/`config.repo` shims are deleted on day one — no deprecation period. Formalize the **PMO capability contract** any candidate system must satisfy: (a) inputs map straightforwardly to Missions as the unit of work; (b) labels or an equivalent concept assign Mission Steps; (c) traffic control via clear blocked_by relations/dependencies; (d) a reliable activity feed for moving/storing files and data. Encode it as a documented port contract + conformance battery (grow `scripts/contract_tests_pmo.py` into the adapter acceptance gate), plus `PMOPort.cancel_mission()`. **Scope fence (founder decision 2026-07-14): no new PMO adapters ship in v0.1** — Linear stays the only adapter; additivity is proven by running **two Linear instances** (e.g. two sandbox teams) on one DevCake. Cross-PMO `blocked_by` is explicitly unsupported in v0.1 — PMO instances are independent; federation is its own project.

**F3 — Any number of repos, including zero.** An instance configures 0..N repos (schema v3); per-mission resolution assigns **0 or 1** configured repo per mission (the mechanism — assignment config vs. mission metadata — is a **founder decision**), with the resolved forge adapter + credentials wired per-run into the runspec. A mission resolving to zero repos routes to F4's internal fallback forge — downstream (EXECUTE/REVIEW/PR mechanics) never sees a repo-less mission. N-repos-*per-mission* is explicitly out of scope (deferred — cross-repo atomicity is its own project). Ships paired with F4: the zero-repo dispatch path stays gated until the fallback exists. Absorbs the old "multi-instance runtime wiring" item.

**F4 — Internal fallback forge: bundled Gitea.** Gitea joins `docker compose` as a long-lived service. Any mission that resolves to no configured repo gets a repository auto-created on the internal Gitea at intake (per-Mission, reused across attempts and rework — the PR-reuse mechanics from M4 carry over), plus a per-Mission machine user with user-scoped tokens and a collaborator grant on that repo (Gitea does not mint repo-scoped tokens; `14` §2). Devs receive it as a *perfectly ordinary forge repo*: EXECUTE & REVIEW run their standard PR mechanics on non-code artifacts with zero special-casing — simultaneously the strongest live test of F1's forge-agnosticism and the substrate for the **non-developer workload testing planned for v0.2**. **Because the fallback forge may not be observed by the end-user at all, deliverables must flow back to the PMO:** when a mission on the internal forge reaches its REVIEW-approved merge, the changed files are packaged (zip of the merged change set) and attached to the PMO activity feed (attachment-first policy) — the PMO stays the one place the user looks. Requires the Gitea `ForgePort` adapter (registry entry + `ForgeDescriptor` dialect); support for external/user-supplied Gitea instances comes free. `ForgeCapabilities` is extracted here, from observed three-forge divergence. Gitea admin credentials are a stack bootstrap secret (`.env`, F5 exception). Open **founder decisions**: internal-repo retention/GC after mission completion; whether internal repos surface in the admin panel. Bonus: the zero-repo golden path needs no external forge credential, though it still uses Linear and real model credentials. *(Superseded 2026-08-04: ADR-0030's auto-provisioned default board removes the Linear dependency too — the standalone path needs only model credentials.)* *(Replaces the earlier "internal Mission worktree / activity mirror" design — see Discarded.)*

**F5 — All operator config via the admin GUI, single-mode.** Everything the operator supplies — PMO instances, repos, credentials, API keys — passes through the admin UI (Configuration for PMO/model settings; Repositories for forge settings), secret *values* included. Env-var indirection is **deleted, not kept as a fallback** (founder decision: one mode, no dual sources of truth — the original PAT-paste incident was indirection confusion). Secrets are stored 0600 under `/data/secrets/`, registered with the redaction layer, never echoed back; the ✓/✗ `env-check` pattern extends to stored-secret status. **Exceptions stay in `.env`:** stack bootstrap secrets only (Dagu, OpenObserve, nginx admin auth, Gitea admin) — what's needed before the GUI itself is up. A standing `security_warnings` breadcrumb marks the posture ("GUI-stored secrets behind basic auth — revisit before exposing beyond localhost") so the decision cannot silently ride into a first real deployment.

## M8 — Forge-agnosticism + hardening spine

**Goal:** the codebase provably forge-agnostic; the in-flight refactor landed; standing security warnings closed. **Implements:** F1; lands PR #1 (`refactor/run-bootstrap-and-secondary-ports`, rebased + fixed 2026-07-14); ISSUES #13, #29.
**Out of scope:** any new adapter; `ForgeCapabilities` (extracted in M11, once a third forge exists to reveal real divergence).

Exit criteria — **all verified 2026-07-14 (M8 complete)**:
- [x] The audited residuals corrected: `config.py` forge/token-env/URL-validation defaults resolve via the adapter registry/descriptor (`registry.DEFAULT_FORGE`, descriptor `token_env_default`); the warning copy is config-derived (`security.security_warnings`); git identity comes per-descriptor (`git_email` required on the port; GitLab dropped the verbatim-github noreply); descriptor `pr_noun` feeds the EXECUTE feed wording.
- [x] CI tripwire green and meaningful (`tests/test_agnosticism.py`): AST import-graph scan (vendor adapters importable only by the registry — forge AND PMO ids, registry-derived), defaults provably resolve through descriptors, warning copy config-fed, secondary string-literal scan with an explicit allowlist. Verified to bite on planted violations.
- [x] PR #1 merged (squash `b1007c3`, 2026-07-14): `ExecutorPort`/`StatePort`/`MessagingPort`/`RunFinalizer` are Protocols; full suite green (243 tests + `ci_suite.sh` + isolated boot smoke).
- [x] Devs emit telemetry through an inserted OTel collector (a new long-lived compose service on `devcake_runtime`) holding the OO service-account credentials; Dev runspecs carry NO OO credential (`OTEL_EXPORTER_OTLP_BASIC` deleted, `domain/oo_auth.py` deleted); `oo-root-creds` gone from `/health` (ISSUES #13). Live-verified: hello run's `dev.run`/`harness.exec` spans in OO via the collector from a credential-free container. Honest limits recorded in `14` §2 Zone B (unauth OTLP forgeable on the runtime network; OSS OO role separation advisory — live-probed).
- [x] Base image digests pinned across all Dockerfiles AND compose services; `scripts/check_image_pins.py` gate wired into `ci_suite.sh` + GHA CI; bake CI green (ISSUES #29). Grok curl|bash installer recorded as the one auditable exception.

**Demo:** `scripts/ci_suite.sh` green including the tripwire; `/health` clean of `oo-root-creds`; a golden-path run whose Dev traces arrive in OO without root credentials.

## M9 — Multi-PMO port (schema v3)

**Goal:** DevCake is PMO-independent; one instance oversees N≥1 PMO instances. **Implements:** F2.
**Out of scope:** new PMO adapters (post-v0.1); cross-PMO *auto-federation* (DevCake never creates cross-instance edges or missions **of its own volition** — *honoring* native peer edges shipped 2026-07-28, ADR-0009 amendment; operator-transcribed missions via the composer are ADR-0030's carve-out, not federation); webhooks.

Exit criteria:
- [x] Schema v3 (instances-with-identities) boots — live stack hand-migrated per docs/10 §3 and running on it (2026-07-14); one GENERIC stale-schema refusal covers v1/v2/old-version files and PUT bodies; the singular shims are gone and the suite is migrated (258 tests).
- [x] Instance provenance end-to-end: `Mission.instance` stamped by the (instance-bound) adapter at normalization + `Run.pmo_ref`; `mission_branch(instance, key)` → `devcake/LINEAR-DEV-17`; run ids `{INSTANCE}-{key}-…` (ACL users/containers/reply streams inherit distinctness); colliding identifiers proven distinct hermetically (`test_ids`/`test_multi_instance`). *(Amendment vs the F2 wording: `MissionRef` itself stays a 2-tuple — every consumer is an instance-bound adapter, so a third field is dead weight; provenance rides on Mission/Run. ADR-0009, plan finding M3 — founder sign-off with this milestone.)*
- [x] The PMO capability contract (a–d) documented (docs/05 §0); `contract_tests_pmo.py` = the adapter acceptance gate (per-instance selection, exit-code verdict, tests 11–13: cancel idempotency, marker fidelity, attachment round-trip) — **12/12 live** against the sandbox team; `PMOPort.cancel_mission()` implemented and covered.
- [ ] **Additivity proof:** two Linear instances on one DevCake. Architecture live-verified as far as one workspace allows (2026-07-14): a second instance hot-added via config PUT → both reported per-instance in `/health`, per-instance label bootstrap attempted, honest per-instance degradation (bad team red, good team green, app healthy), then removed; manager-per-instance + FinalizerRouter + cross-instance dedupe hermetically tested. **Blocked on founder actions:** (1) the sandbox Linear workspace refuses a second team (`teamCreate` → 403, plan limit) — needs a plan upgrade or a second workspace + API key; (2) no working forge token in `.env` (GITHUB_TOKEN empty, GITLAB_TOKEN 403 even on the sandbox project) — completions can't run until a token is rotated in.

**Demo:** one admin panel overseeing two Linear teams; two missions with colliding identifiers complete with instance-prefixed branches.

## M10 — Repo multiplicity (0..N per instance, 0-or-1 per mission)

**Goal:** repos become plural and per-mission-resolved. **Implements:** F3.
**Out of scope:** N repos per mission (deferred); zero-repo dispatch (arrives with M11's fallback).

Exit criteria:
- [x] Per-mission repo resolution decided (**founder decision 2026-07-14: config default per PMO instance + `` `devcake-repo:<name>` `` description-marker override**) and implemented (`domain/repo_routing.py`) — **STICKY once a run exists**: the latest run's `repo_ref` wins; any conflicting marker/default edit GATES with a human-action reason instead of re-routing (PR-reuse invariant preserved). The resolved repo's adapter + credentials wire per-run into the runspec at request time (`runspec_secret_payload` by `run.repo_ref`; nothing secret at rest).
- [x] Two configured repos on different forges in one instance route spec env, dialect, and tokens per run — hermetically proven (`test_repo_routing.py`, real GitHub+GitLab adapters via `ForgeRuntime.rebuild`). *Live two-merged-PRs demo pending the same founder blocker as M9's additivity proof (no working forge token in `.env`).*
- [x] Forge health/breaker state is per repo (`domain/forge_runtime.py`: `apply_health`/`latch` per name, `repo:<name>` breaker keys in `/health`) — a latched breaker on repo A never stops repo B (scheduler gates per candidate). Live-verified: the pre-existing 403 on the configured repo latched exactly `repo:main`. Resolution-failure contract per call-site class: sweeps skip with visible `blocked_reasons`, review/finalize fail the run cleanly, dispatch/runspec refuse.
- [x] Zero-repo missions derive correctly and are visibly gated (schedule surfaces the reason; `repos: []` is valid config) pending M11.

**Demo:** one DevCake, one GitHub repo + one GitLab repo, two missions → two merged PRs, one on each forge.

## M11 — Internal fallback forge (bundled Gitea)

**Goal:** zero-repo missions run fully autonomously on an internal forge; the non-code workload substrate for v0.2 is ready. **Implements:** F4.
**Out of scope:** the non-developer workload testing itself (v0.2).

Exit criteria:
- [x] Gitea is a bundled compose service (`gitea/gitea:1.27.1-rootless` since the 2026-08 bump, digest-pinned): live-verified healthy from the stack, admin bootstrapped via the `docker-setup.sh → migrate → admin-create` wrapper (GITEA_ADMIN_* stack secret), container logs in OO. The wrapper ordering + idempotency were live-probed first (M11.0).
- [x] The Gitea `ForgePort` adapter passes the forge contract battery — **13/13 live** (`scripts/contract_tests_forge.py --forge gitea`, wired into `ci_suite.sh`); an external/user-supplied Gitea is just a `RepoInstance(forge="gitea", …)`. All divergences (client-side head filter, APPROVED event, overloaded 405, boolean mergeable, whitelist-officialness) were live-verified and encoded.
- [x] Zero-repo dispatch un-gated (`resolve_repo_live`): a repo-less mission gets an auto-created internal repo at intake (per-Mission idempotent, reused across attempts/rework; the resolver re-registers it after an app restart), routed downstream exactly like an external repo — the F1 payoff. *(Full ONBOARD→…→Done runs on live model tokens; the machinery is hermetically tested + the API path live-verified end-to-end.)*
- [x] **PMO delivery** (`domain/orchestrator/deliver.py`): on the approved merge (all three Done sites — review auto-merge + both sweep paths), the changed files are zipped (binary-safe, removed-files excluded, size-capped with a MANIFEST) and attached to the PMO feed; failure never un-Dones the mission. Hermetically tested; `pr_files`/`file_content` live-verified 13/13.
- [x] **Isolation (INV-2 spirit) — live-verified:** mission A's write token → own repo 200, → mission B's repo **404**; A's read token reads 200 but writes **403**. Mechanism (honest): per-mission machine user + write/read scoped token pair in a private org (Gitea tokens are user-scoped — ADR-0010, docs/14 §5).
- [x] `ForgeCapabilities` extracted from the observed GitHub/GitLab/Gitea divergence (`mergeable_tristate`, `self_approval_blocked`, `branch_protection_read`, `pr_list_head_filter`); review's conflict-vs-handoff classification branches on `mergeable_tristate`, not forge identity.
- [x] Founder decisions recorded (ADR-0010): internal repos **retained indefinitely** + a manual admin **Clear** button (`DELETE /api/v1/internal-repos/{name}`, refuses while a live run uses the repo); a **read-only** internal-repos admin card with a Gitea UI link.

**Demo:** a repo-less Linear issue → mission completes on the internal forge; the zip lands in the Linear feed; the internal PR sits merged in Gitea. *(Blocked on the same founder items as M9/M10: no working forge/model token in `.env` for a full live model run — the machinery is proven hermetically + via direct API drills.)*

## M12 — Single-mode GUI config + v0.1 release gate

**Goal:** operable by a stranger without touching `.env` beyond bootstrap; acceptance parity; docs re-baselined; v0.1 tagged. **Implements:** F5; ISSUES #30.
**Out of scope:** OIDC/SSO (deferred; the `security_warnings` breadcrumb covers the posture).

Exit criteria:
- [x] All operator config flows through the admin UI (`SecretField` write-only inputs; forge settings live on Repositories, not Configuration); secrets stored 0600 under `/data/secrets/connections/` and `/data/secrets/harness/`, redaction-registered, never echoed back (live-verified and regression-tested: `GET /config` carries no secret-bearing fields, legacy `*_env` fields, or planted secret values; `secrets-check` returns presence + `updated_at` only — no value-derived fingerprint). ✓/✗ stored-secret status in the UI.
- [x] Env-var indirection removed (schema v4: `*_env` fields deleted; properties read the store, no `os.environ` fallback); `.env` reduced to stack bootstrap secrets (Dagu, Redis, OpenObserve root+ingest, nginx admin auth, Gitea admin, DOCKER_GID); the dismissable `gui-secrets-basic-auth` breadcrumb ships. Live stack hand-migrated v3→v4 and running on stored secrets.
- [x] Fresh-`/data` operator drill documented GUI-only (`docs/tutorials/operator-drill.md`): empty volume → configure everything through the admin UI → external-repo + zero-repo missions → assert `.env` untouched beyond bootstrap. *(The drill stays manual — it is the stranger-operability test.)*
- [x] `scripts/acceptance.py --forge` covers GitHub, GitLab, and **Gitea** (the zero-repo internal-forge lane: asserts the deliverable zip in the PMO feed + the merged internal PR via `GITEA_ADMIN_*`, with no external **forge** credentials; it still uses Linear and real model credentials; ISSUES #30). Tester credentials come from the shell/`.env`, never DevCake's stored secrets. *(Full live model runs remain gated on the founder token blocker; the machinery is proven by the contract batteries + hermetic tests.)*
- [x] Docs re-baselined (10 §3 v4 shape, 14 §3 the GUI secret store + honest limits, ADR-0011); **v0.1 tagged.**

**Demo:** stranger-operability walkthrough — fresh clone, bootstrap `.env`, everything else via the GUI; one external-repo mission and one zero-repo mission both reach Done.

---

## History — Layer 2: Closed releases (frozen)

Numbered milestones stop at M12. What follows are **immutable release records**
(how the v0.1 and v0.2 tags were cut) plus a frozen feature inventory of major
landings that fed those cuts. Do not append new work here — use
[Layer 3](#living-log-after-v02).

## v0.1 — FINAL (tagged 2026-07-15)

**The true v0.1 is the tag at the end of the three rounds below** — M8–M12 plus the full audit-fix round and the live-test/UX rounds, consolidated (the interim working tags v0.1.1/v0.1.2 were folded in; the 2026-07-14 pre-audit tag was a release candidate in hindsight). Final state: 347 unit tests, ci_suite green (pin gate, gitea battery 13/13, stub smoke), live Linear connection green, all SPA flows browser-verified.

### v0.1 hardening round — audit fixes + Config/Sidebar UX (2026-07-14)

A six-agent skeptical audit of the whole v0→v0.1 range confirmed 8 open bugs, ~8 claim mismatches, and ~20 risks; all were fixed in a 15-commit round (audit ids A1–A29 in the commit messages), plus four founder-requested UX features:

- **Correctness/security (P0):** a permanent PMO error (revoked key) no longer starves every later instance's poll segment (`/health` gains `poll_degraded`); `ForgeRuntime.rebuild` preserves internal-forge registrations (config/secret saves no longer fail in-flight zero-repo runs); the admin internal-repo Clear sticks (terminal missions never re-provision); the secrets-check path-traversal oracle is closed and all secret endpoints validate input (DELETE is a real delete; harness keys are revocable); the CI harness smoke can actually fail and the image-pin gate discovers files, scans `COPY --from`, and enforces `pull_policy: never`.
- **Correctness (P1):** dispatch honors `DEVCAKE_TAG`; `review:awaiting_merge` is a registered swap marker; the FinalizerRouter's orphan-fail path tears down ACL users/reply streams; Linear label reads paginate everywhere (fail-loud ceilings); the Gitea token probe is tri-state (no re-mint on transient, both tokens checked); mission ownership persists across restarts (`/data/state/mission_owner.json`), never releases under an active run, and `main`/`sys` are reserved PMO instance names.
- **Routing (founder decisions):** a changed instance `default_repo` no longer gates in-flight missions — sticky wins silently; malformed `devcake-repo:` markers gate instead of silently routing to the default; decomposition children inherit the parent's repo marker.
- **Hygiene:** battery token cleanup (15 leaked admin tokens revoked), fail-loud `provision_oo`, honest zip MANIFEST attribution, GitLab ref encoding, redaction-scan caching, Gitea bootstrap fails loud + admin password rotation syncs from `.env`, OpenObserve left the runtime network (the collector bridges), and the fabricated `-k "not live"` changelog line was removed from this file.
- **UX (v0.1.1 features):** per-Mission-Type **prompt templates** (stored under `/data/config/prompt_templates/`, safe `{var}` rendering — no format-string escaping — with a Config-page Prompts section, per-type active selection, and health warnings on missing templates); a **Repositories sidebar page** sharing one unified config draft with Configuration (cross-page Save review, single nav guard); the sidebar services grid grew a **Gitea light** (3×2); Overview's Services stat became the **Devs fleet card** (green available / pulsing blue running / red broken, server-computed `credentials_ready`); a save-time warning when `default_repo` changes while runs are in flight.

Suite at that round's close: 334 unit tests; full `ci_suite.sh` (pin gate, gitea battery 13/13, stub-harness smoke) green; SPA flows verified in a real browser. Known deferred: a sweep tool for Gitea svc users orphaned by pre-fix Clears (below), and registering the public `devcake` Docker Hub org (manual founder action — squatting hazard).

### v0.1 live-test round — fixes, multi-repo PMOs, reference repos, workflow presets (2026-07-15)

Fixes from the founder's first post-v0.1.1 live pass, plus the multi-repo design decided the same day:

- **CRITICAL regression fixed:** the A12 label-pagination change nested a paginated labels connection under `teams(filter:)` and blew Linear's ~10k query-complexity budget ("Query too complex", 15560) — every `_team` consumer was down. `_team` now splits into a cheap team-shell query + cursor-paged single-team label reads (live-verified green against the sandbox: 10/10 labels, 35 missions).
- **PMO repo SET (schema v4.1-shape):** `PMOInstance.default_repo` → ordered `repos: [..]` — first entry is the default for unmarked missions, markers must name a listed repo (unlisted gates), `[]` = per-mission internal repos; sticky-wins semantics preserved; stale `default_repo` shapes refused with a hand-migration hint. SPA: ordered toggle chips on the PMO card.
- **Repo-aware + multi-clone ONBOARD (founder decision — cross-repo work splits at triage):** multi-repo instances give ONBOARD every set repo as a shallow read-only sibling clone (per-repo read tokens via `extra_repos` in the runspec, non-fatal failures) and a `{repo_options}` playbook section stating the rule: cross-repo work decomposes into one child per repo, each with its own `devcake-repo:` marker + `blocked_by` ordering. EXECUTE/REVIEW keep the one-branch-one-PR contract. Dev images rebuilt lockstep.
- **Operator repos on the bundled Gitea:** repo cards offer "gitea (internal)" with a Create-repository modal — the repo lands in the separate `devcake-repos` org (never touched by the per-mission list/sweep) with its full card token set minted and stored automatically.
- **Reference repos (founder request, same day):** each PMO instance carries an ordered `reference_repos` list (multiple supported) — configured repo cards cloned READ-ONLY into **every** stage's workspace (external and internal-forge missions alike) as consultation material, each with its own read token. Disjoint from the routing set by validation; a `devcake-repo:` marker naming one gates ("read-only context, never a work target"); all four playbooks gain a `{reference_repos}` section naming the clones. SPA: a second chips row on the PMO card, mutually exclusive with the work-repo chips.
- **Smaller fixes:** Gitea UI quick link on Overview + a persistent Internal forge section (with the link) even when empty; a bulk "Clear data" action for internal repos; Connect-via-OAuth follows the DRAFTED harness (disabled until saved) — grok/codex flows verified (`codex login --device-auth` live-probed on the pinned CLI); claude-code cards explain the paste-token path.

## v0.2 — FINAL (tagged 2026-07-19)

The consolidation release: everything on `main` since v0.1.1, responding to an external skeptical review and successive adversarial review rounds. **All four hard release gates met** — the two live E2Es (ADR-0012 decomposition chain, ADR-0013 settings round-trip incl. a real Gitea volume restore), the Clear-Runs stop hardening, and the implementation audit/fix rounds recorded below. Residual documentation findings were corrected in the final cut; this record does not claim that review can make a moving codebase permanently drift-free.

- **Positioning (PR #16):** "Your board is the interface" replaces the walked-back "You never operate it"; normative when-to-use / when-not §1b; a real operator contract (`docs/18`) incl. the first consolidated secret-rotation procedure; roadmap status vocabulary (built / live-verified / ⏳).
- **Reliability (PR #18):** ruff `BLE001` enforced — every blanket `except Exception` narrowed or contract-justified inline (`docs/15 §7`); the grok-auth classifier trips the DEV_AUTH breaker on revoked creds (exit 12) instead of burning three attempts.
- **Structure (ADR-0015, PRs #21–#27):** the orchestrator binding façade is gone — `MissionManager` is DI + advisory state + verbs, implementation is module functions taking `mgr`; `api/main.py` went 1,837 → ~780 lines (composition root + ≤4-statement route forwards + service modules, AST-guarded); the admin ConfigPage god component became a 69-line dispatcher + section components. All behavior-preserving; a guard test is the "do not resurrect the god module" ratchet.
- **Clear-Runs concurrency (PRs #28, #30–#32, #34):** the hard one. A stop-then-**drain** wipe that stops every dispatch flavor at the true chokepoints — `RunBootstrap.launch` holds a `dispatch_lock` (every dispatcher funnels through it; an AST tripwire fails CI if that ever stops being true), `RunManager._kill_inner` guards its final save atomically, and a process-local **wipe generation** no-ops any stale save at the store layer (restamped for adopted runs on reconcile). Found incomplete by audit **three times** (D3 fixed ordering not concurrency; #30's poll-lock missed the oauth/steward/hello paths; #31's kill-guard left a phantom-record window) and closed each time at a deeper chokepoint. **Live-verified 2026-07-19:** clear-runs triggered while a Dev run was live → run stopped + drained, records wiped, **zero ghost runs after a forced poll, zero `AuthenticationError`, zero orphaned ACL users** — the ACL/SIGTERM race is closed end-to-end.

**What the audit taught, recorded so it isn't relearned:** the test suite stayed green through all three incomplete concurrency fixes — only adversarial multi-agent re-audit of each fix delta caught them. Fix concurrency at the chokepoint (launch / kill / save-generation), never per-call-site; prove a structure tripwire fires by planting a violation; run the suite in the Redis harness (`pytest_app.sh`), not bare pytest.

Final state: 663 unit tests + admin `check:ui` green; `ci_suite.sh` (pin gate, gitea battery 13/13, dispatch-hello smoke) green on the deployed stack; the live clear-under-load smoke passed. Trailers that landed with the initial cut or immediately after: **`profiles.mjs` in `check:ui`** (PR #36) and the missions-board 1280 layout fix (PR #37) — full suite **61** browser checks including profiles. Remaining non-gating trailer (⏳): a fresh-`/data` operator-drill re-run.

**Final-cut hygiene (2026-07-19):** compose mounts `./dagu/dags` **read-only** into Dagu (trusted launch code — `14` §5; live-verified: health + hello dispatch; non-fatal `.dag.index` write WARN). Documentation was reconciled with multi-connection runtime behavior (`00`, `06`), the RO DAG mount (`13`, `14`), the admin/config split, harness/model ownership, process-local maintenance locks, and the public-config secret-response regression. The release tag is placed on this final cut only after it lands, then treated as immutable.

### Feature stream through the v0.2 tag (frozen inventory)

The v0.1 / v0.2 FINAL sections above are **release narratives**. This inventory
is the chronological feature-level list of major landings that fed those cuts
(post-M12 through the **2026-07-19** v0.2 tag). It is **frozen** with Layer 2 —
do not append post-v0.2 work here.

> Ordered **oldest → newest**.

- **MCP plugins — core ports** (2026-07-17, PR #6): `DevType.secret_env` +
  live `mcp_setup_commands` runspec wire, referenced-missing-secret dispatch
  gate, exit-14 `DEV_MCP_SETUP` reporting, `tutorials/03-mcp-plugins.md`;
  connectors live out-of-repo (vendor segregation). **built** ·
  **live-verified (founder):** plugin install + registration path exercised
  end-to-end on the live stack (functional with operator-supplied plugin
  token; e.g. logs MCP).
- **Decomposition depth + fail-closed edge inheritance** (2026-07-18,
  ADR-0012, PR #10): depth-tagged markers, `max_decomposition_depth` +
  Traffic-control UI, strict inherited edges, scheduler family gate, lineage
  notes on canceled parents. **built** · **live-verified 2026-07-19**
  (sandbox chain DEV-128→…→DEV-132/133; depth limit + inherited edges).
- **Missions board** (2026-07-18, PR #11, rflpazini): Hermes-style kanban with
  steering comments + stop-run. **built** · deployed live 2026-07-18.
- **Config profiles + settings bundle** (2026-07-18, ADR-0013): versioned
  bundle over the four settings stores; named profile snapshots; apply =
  replace-the-world through config choke points. **built** ·
  **live-verified 2026-07-19**.
- **Settings export/import + setup-env + Gitea backup** (2026-07-18, ADR-0013
  part 2): encrypted export/import landing as profiles; generated `.env`;
  `backup_gitea.sh` / `restore_gitea.sh`. **built** ·
  **live-verified 2026-07-19**. **`profiles.mjs`** in `check:ui` (PR #36).
- **Activity-feed fidelity + per-mission activity repos** (2026-07-18,
  ADR-0014, PR #15): last-message-inline + full dump, `MISSION.md`,
  `activity-*` repos, quoting quarantine. **live-verified 2026-07-18**.
- **Missions "Poll now"** (2026-07-18, PR #14): INV-1-aligned poll CTA.
  **live-verified 2026-07-18**.
- **Clear-runs concurrency follow-up** (v0.2 cut, **live-verified 2026-07-19**):
  wipe generation (`store_gen`), force-remove via Dagu on soft-drain timeout,
  dispatch chokepoint AST tripwire. **built**. **Does not claim** multi-threaded
  store safety or host-level force-kill. (Release narrative: v0.2 FINAL above.)

---

## Living log (after v0.2)

**Layer 3 — the only open section.** Append here for work that lands **after**
the v0.2 tag (2026-07-19). This is not a second milestone series and not a
continuation of “since v0.1” (that window is closed in Layer 2).

| Subsection | What it is |
|---|---|
| **Shipped after v0.2** | Chronological log of merges on `main` after the closed cut |
| **Field evidence (self-reported)** | Production use reported by the operator — evidence class stated, deploy pinned |
| **Still open** | Residuals / demos still owed from Layers 1–2 — not new features |
| **Candidates** | Design we may invest in; **not** an ordered sprint queue |
| **Deferred / Discarded** | Longer-term or rejected ideas |

Honesty rule: a feature is not *done* until the live box proves it (status
vocabulary at the top of this file).

Versioning doctrine: below v1 the project carries **no backwards/legacy-compat
obligation** — an upgrade may require a wipe-and-re-onboard instead of a
migration. v1 is gated on a **pre-registered, receipted evidence run on public
material**, executed on the release candidate and reported in these docs;
until that run exists, field evidence below stays operator-self-reported.

### Shipped after v0.2

> Ordered **oldest → newest**. Starts the day after the v0.2 tag.

- **Skills philosophy + prompt assembly** (ADR-0016, 2026-07-20): three-layer
  composition; domain-only skills; `skills_required` + tri-state chips; role
  Dev Types (`judgment` / `implementer` / `steward`); registry `skills_dir`
  snapshotted onto the Run. Normative ADR + `app/devcake/skills/README.md`.
  **External skill repos SHIPPED 2026-08-13** (ADR-0016 addendum, founder
  ruling "just a forge adapter"): `<card>/<skill>` names serve read-only
  from the card's ADR-0024 mirror (no second cache), fail-closed via the
  existing gate's needed-set union, payload paths flattened so the
  container contract is untouched, `Run.skill_repo_heads` provenance,
  private repos via card tokens day one.
  **built**.
- **Admin skill/prompt Markdown View** (PR #45, post-ADR-0016): skill and
  prompt View render as Markdown in the SPA. **built**.
- **Pipeline handoff + PMO zip opt-in + setup/activity polish** (2026-07-21,
  ADR-0017): optional `attach_merged_changeset_to_pmo` (default off) for
  configured repos; always-on RO mounts of **done** direct blockers’ work
  repos (`Run.blocker_work` + `{blocker_repos}`); Overview setup accepts
  healthy internal forge or “I’ll work with the internal forge”; activity
  `.zip` attachments extracted under `{stem}/`. **built**.
- **Gitea Issues PMO adapter** (`gitea_issues`, 2026-07-21): first forge-issue
  family member — pure `PMOPort` on bundled or external Gitea (`team_key` =
  `owner/repo`). GitHub/GitLab Issues adapters, if ever, copy this profile.
  **built**.
- **OpenObserve ingest auto-provision** on app boot (telemetry ops; lands with
  the harness/fault campaign window). **built**.
- **Harness fault classification + model-backend brake** (ADR-0018; expanded
  2026-07-25…26): truthful in-band failure surfaces when experimenting with
  non-default models/backends (exits 15/16, structured `error_class` /
  `attempt_counted`, workspace forensics, misplaced-`result.json` recovery,
  store-derived per-Dev-Type throttle with excusal caps). **Scenario captures**
  under `app/tests/fixtures/harness_streams/` are verbatim CLI stdout (not
  hand-written JSON) for all three templates, asserted by
  `test_harness_captures.py`. Grok token extraction prefers the terminal
  `end` event (input/output split) with `signals.json` as fallback. Also:
  chunk-buffer admission fix, attempt-count injection fix, EXECUTE
  binding-rule clarification, local OpenAI-compatible backend recipes in
  `08` §8. **built** · unit suite green · ruff clean · bake app/admin/images.
- **Dev entrypoint package split** (PR #49, 2026-07-26): ADR-0018 fault domain
  plus harness argv/render/tokens and workspace helpers live under
  `images/common/devcake_dev/` (`domain/`, `harness/`, `workspace/`,
  `adapters/`); `dev_entrypoint.py` is the thin façade. Production and the
  capture rig already share `harness_argv`. **built** — the natural substrate
  for H1 (`HarnessDialect`) if that candidate is taken up.
- **Per-PMO intake toggles** (PR #50, 2026-07-26): each PMO instance can pause
  intake under the global `intake_paused` master switch; SPA health-driven;
  draft Save cannot undo instance toggles. **built**.
- **Clear secrets operator flow** (PR #54, 2026-07-26): section ⋯ on Dev
  Types / PMO / Repositories — multi-select presence-only inventory, context
  reorder, ConfirmDialog with default-on master intake pause, then
  `POST /secrets/clear` (pause-first, breakers, audit). **built**.
- **Security-contract reading aid** (PR #53) + roadmap/observability alignment
  (PRs #51–#52): docs/14 §0a buckets, deferred iron-proxy radar. **built**
  (docs).
- **Dev toolchain floor** (ADR-0023, 2026-08-02): capability floors baked
  into the shared Dev base — playwright's pinned headless Chromium + system
  libs (class A: user-space can download a browser but never apt its shared
  libraries), build-essential, Node in EVERY harness image (grok Devs could
  not `npm run dev` before), uv + PATH/`.npmrc` guarantees for root-free
  self-provisioning (class B), and the turn-saving conveniences + pandoc/
  poppler/pandas/openpyxl document-and-spreadsheet floor (class C, founder
  decision). Build-time smoke proves the floor AS uid 1000 (headless shell
  must launch). Out by doctrine: sudo, docker, DBs, vendor CLIs,
  LibreOffice. Base ~1.4 GB, shared layers. **built**. Skeptical-review fix
  round same day: `/opt/pw-browsers` dev-owned (as shipped, the env var +
  read-only dir bricked every non-baked browser — measured EACCES after
  the download; disposable container ⇒ writable is safe) + `tini` as PID 1
  (browser process trees vs a non-reaping entrypoint) + docs/ADR truth
  sweep incl. `14` §11 browser-injection radar + memory-budget notes.
  **built**.
- **Mandatory repo source mirror** (ADR-0024, 2026-08-03): a 27-repo prod
  stress instance re-cloned ~300 MB per run; now the app maintains bare
  mirrors on `devcake_mirrors` (heads+tags, gc.auto=0, per-sync HEAD; git +
  git-lfs = the app's first subprocess seam), sync is a FAIL-CLOSED dispatch
  precondition (no toggle — founder decision; reason on missions row via the
  shared gate dict; auth failures latch the repo breaker), Devs clone
  `file://` from the RO mount and get origin rewritten to the real forge
  (Dev-invisible; extras drop read tokens from the runspec). LFS = capability
  toggle (standalone file:// transfer probe-verified, 2 MB bit-exact at
  depth 1). Dagu bumped 2.10.5 → 2.11.3 as a severable rider (probes green;
  CORS/token-TTL breaking changes verified non-applicable; controller/LLM
  DAG features explicitly NOT adopted). **built**.
- **Provisioned workspaces** (ADR-0025, 2026-08-03): ADR-0024 mounted the
  whole mirror volume RO into every Dev, reopening the context-control problem
  (agent saw `/mirrors` bare-pack duplicates of its own repo + every other
  repo). Now one run is TWO dependent Dagu container steps sharing a per-run
  host-bind workspace: `provision` (trusted code, `/mirrors` RO, clones, exits)
  then `run_dev` (the agent, workspace ONLY — no `/mirrors`). The runspec is
  phase-scoped (provision gets no harness/model secrets); an app-written
  sentinel + a provision marker fence the daemon-root-autocreate and
  WS_HOST-drift edges; cleanup is best-effort with a sweep as the guarantee;
  `run.started` is hardened dispatched-only (also fixing a latent
  finalizing-revert); mirror clones are credential-stripped with `-c lfs.url`
  pinned; LFS posture runs in both phases. SUPERSEDES ADR-0024 §5's ambient
  mirror-read risk (agent ambient read surface now zero). No single-container
  fallback — the phase is mandatory and a missing/unknown one exits 20 loudly
  (the initial ship carried a monolithic branch for one commit, deleted the
  same day: pre-v1 does not carry rollback compat). **built**.
- **In-container run continuation** (ADR-0022, 2026-08-02): the
  narrate-and-stop landing (exit 0, `stopReason EndTurn`, no `result.json` —
  ~50% of long weak-model runs, hours lost late) is nudged instead of failed:
  session resume (capture-verified for all three CLIs, `RESUME_SPECS` with
  per-harness cumulative-usage facts) → fresh-session escalation on stall,
  budget-only termination (`continuation_policy` / `max_continuations`,
  Limits & traffic). Plus `TURN_DISCIPLINE` prompt epilogue, exit-11
  `evidence.terminal` instrumentation, `continuations_used` on
  Run/API/feed/OTel, rig `--resume-prompt-file` mode + six
  `*_resume_nudge*` capture fixtures. **built**.

- **External audit + evaluation fix campaign** (2026-08-03…04): Grok's
  tag-range audit (AUD-001…AUD-034; its report file was deleted from the
  root 2026-08-03 — **this entry is the AUD-ID ledger** code comments
  resolve against) landed as PR #81; the four-review critical evaluation
  (architecture / tests / docs drift / ops) then drove PRs #82–#87:
  - **ADR-0026 spend discipline** (#82): `attempt_reset` (strict
    `label-ops` default + `DEVCAKE-RETRY` gesture / `any-comment` /
    `unlimited` with cumulative-cost warnings) closing the
    chatty-integration attempt-reset hole; opt-in `brake_on_bad_output`;
    first-class Limits & Traffic UX. **Deliberate behavior change**
    (strict default) — release-noted. **built**.
  - **TOCTOU + parity** (#83): watchdog timeout/liveness kill branches
    re-read before killing; `_kill_inner` aborts save + restore when the
    state moved (the mover wins); the mirror gate's `needed_for` set is
    snapshotted on `run.mirror_repos` and the runspec serves mirrors from
    the snapshot only. **built**.
  - **Enforcement** (#84): `test_api_surface.py` (TestClient over the real
    app — 401/403 on every route; the auth middleware is finally
    load-bearing), `test_repo_structural.py` (+RO mounts in both pytest
    runners: /mirrors isolation, workspace binds, RUN_ID fence, no Dagu
    auto-retry, single-uvicorn-worker premise), CI contract lane (compose
    smoke stack + bundled Gitea runs BOTH batteries per PR), UI-suite
    honesty (15 tautologies → `checked()` predicates, helpers suites wired
    into check:ui). **built**.
  - **Ops hardening** (#86 + hotfix #87): redis aclfile + per-op `ACL SAVE`
    (dev users survive a redis-only restart — drill-verified; with an
    aclfile redis IGNORES --requirepass, measured, so the default user
    lives in the file, boot-regenerated from .env), `maxmemory 1gb +
    noeviction` flood backstop, `chmod 600 .env` enforced, bake-failure
    dagu-restart trap, `backup_data.sh`/`restore_data.sh`, vendored grok
    installer, ADMIN_PASSWORD ≥12 (**deliberate breaking change**,
    release-noted). #87 also REVERTED the attempted WS_HOST DAG
    precondition: Dagu 2.11.3 loads but never expands service env in
    `condition:` — every dev-run sat `dispatched` to the startup-grace
    kill (measured live); an inverse structural test bars unverified
    reintroduction. **built**.
  - **AUD disposition:** fixed in #81 (`df08c9a`): AUD-001/002 (workspace
    fail-closed real), 003 (stop-dagu-before-bake half; the DAG-precondition
    half was attempted in #86 and reverted in #87 — the daemon-autocreate
    residue is tracked debt again), 004 (tag lockstep), 005/006 (merge
    wedges), 007 (sweep budget), 008 (doc overclaim deleted), 010
    (tristate trust in sweep), 011 (fence `{6,64}`), 017 (costing bool
    guard), 018 (parse-cache clear). Fixed in this campaign: 015-adjacent
    kill/finalize races (#83), backup-pair gap (§ above), 029 (vendored
    installer). Still open, honestly: **AUD-009** (provision phase is
    honor-system — accepted, documented), **AUD-012** (relative WS_HOST via
    plain `docker compose up` — up.sh asserts, compose alone does not),
    **AUD-020** (hard-coded `devcake_mirrors` volume name — doc warning
    only), **AUD-022** (no Dev cgroup HostConfig — docs/14 §11 debt).

- **Post-evaluation campaigns** (2026-08-04…06, PRs #88–#112): docs truth
  sweep + the six architecture cleanups (#88–#89); ingress consumer survives
  a Redis restart (#90); **ADR-0027 failure taxonomy as data** (#91);
  **ADR-0029 TokenReport v1 + SQL-readiness** (#92); **ADR-0028
  composition-root factory** (#93); OAuth success stamps `ended_at` and never
  resurrects a terminal run (#94); harness-CLI + infra pin bumps (#95–#96);
  the SPA overhaul and **ADR-0030 default board + New Mission composer**
  (#97–#103); control-plane auth/backup/CI hardening (#104); the
  REPLY/DELIVERABLE marker generalization — core names no downstream
  environment (#105/#108); **ADR-0031 phase 1 — the Freshness Gate** on
  REVIEW's context-closing finalize (#109); **ADR-0032 mission handoff
  notes** — the closing narrative flows with the graph (#112); the
  **MAPPER → STEWARD rename** with persisted-state migrations (#111).
  **built** — hermetic suite green; these merges landed during the 2026-08
  GHA cache-service outage, so CI re-verification came only with the
  cache-export resilience fix (#113, `ignore-error=true` on `type=gha`
  cache-to across all three lanes); the live stack has **not** yet been
  redeployed onto them (R9 ritual + image rebake owed).
- **Discovery routing: SHIPPED hermetically — graduation smoke owed.** Both
  ADR-0033 halves landed 2026-08-13. Harvest: the optional `discoveries`
  result key (ONBOARD/EXECUTE/REVIEW; PLAN relays via a marked PLAN.md
  section), `DISCOVERY_<seq>.md` always attached as the step's deliverable
  + the marked source-feed comment, the `DEVCAKE-DISCOVERY` sweep-gate
  label (derivation-inert, AST-guarded), and the operator budget knobs
  (`AppConfig.budgets`, founder amendment to D7 — 0 = unlimited). Routing:
  the `stewarded` outcome rename (one duty-agnostic outcome), the STEWARD
  discovery flavor (curated family package, family work repos RO,
  propose-only routes, verbatim transport enforced structurally),
  `ELEVATED_MARKERS`' first member (`devcake:discovery-in:v1` — a routed
  finding trips an in-flight recipient's freshness re-review), the
  MISSION.md advisory block ("leads, not truths"), receipt-complete
  termination (`posted − receipted` converges; routed-nowhere batches get
  `to=-`), the per-PMO `discovery_routing` draft toggle, and the steward
  seed re-pin to Claude Opus (D10) with its `claude-opus` rate-card row
  (`builtin-v2`). Distinct from HANDOFF (ADR-0032): a handoff is the
  delivery method for discovery consequences that matter immediately
  downstream; `discoveries` is the family-wide memory the steward routes.
  Post-ship audit closed the leftover cracks: `to=-` is deliberate/clear-runs
  only, plus the never-healing ceiling case (raised to humans); genuinely
  transient failures (unreadable feed / failed post) stay pending; the
  numeric route budgets were **deleted** (addendum 14 — a spent budget
  could only hold or kill work; the `(source, step)` dedup and family
  size are the structural bounds); harvest checkpoints only after the
  marker is on the feed; family clones unpack `resolve_repo`'s
  `(name, reason)` tuple on the kick path; both steward flavors share one
  lock; intake pause gates the harvest kick; `scan_source` is `full=True`
  and fail-closed on truncation. **GRADUATED 2026-08-13** — the live
  multi-mission family smoke PASSED on the dev stack (R9-redeployed the
  same day, images+app lockstep): family `board:missions#1←(#2,#3)` on the
  managed zero-repo board, all-grok staffing. Receipts, end to end: #3's
  ONBOARD discovery harvested (`DISCOVERY_3.md` attached,
  `devcake:discovery:v1 step=3 n=1` marker, gate label added); the
  event-kicked discovery steward routed it (`stewarded`, receipt
  `devcake:discovery-routed:v1 step=3 to=…#2`, gate label retired);
  the delivery (`devcake:discovery-in:v1 src=…#3 step=3`) landed on #2
  MID-REVIEW → **freshness re-review 1/5 withheld the approve** and the
  re-review dispatched; #3's later steps re-harvested (steps 4–5), wave 2
  tripped **2/5**, wave 3 converged with a clean approve — the counting
  budget arithmetic and `(source, step)` dedup behaved exactly as
  specified, no runaway. The re-review runs' MISSION.md carried the
  advisory block ("leads, not truths…") with per-batch provenance —
  durable in the activity repo. Both missions completed; whole smoke
  ≈ $0.25 effective / 1.9M tokens. Two incidental finds, both fixed
  same-day: the zero-repo family gate wedge (#147, verified live BY this
  smoke) and stale per-dev-type grok credential copies (device-bound —
  heal via the OAuth device flow, never file copies).

- **Skeptical-audit intervention campaign** (ADR-0034 + 18 PRs #119–#136,
  2026-08-12): a seven-reviewer skeptical audit at `ccc6da9` found the
  codebase sound but with two structural weaknesses — guarantees enforced by
  *discipline* not *structure*, and singular processes with multiple
  parallel implementations. The campaign closed both. **Data-safety
  (Phase 0):** restores now verify-before-wipe with a kind marker
  (`scripts/lib/`), gitea_issues label pagination + fail-loud project refs +
  no truncated-rewrite destruction, crash-honest bundle apply (adds →
  commit → destructive) + credential-file migration + one assignment
  validation path (boot-refusal), the RETRY_TOKEN unquoted-scan fix.
  **Chokepoints (Phase 1, ADR-0034):** mission completion unified from four
  sites into `orchestrator/completion.py` (with the mis-scoped-except F4
  fix), the checkpoint step-key registry `orchestrator/steps.py` (derived
  swap/gate tables + AST guard + domain→adapters import ban), one
  repo-sourcing rule, the adapter toolkit (`_toolkit` + `http.forge_request`
  — the port's no-httpx-leak contract now enforced), promoted underscore
  APIs, CI single-path (DEVCAKE_TAG lockstep + shared bring-up derivation +
  hello pinned mirror), and the SPA↔backend cross-language contract fixture.
  **Robustness (Phase 2):** read-only cached-concurrent `/health` probe with
  a poll-cycle label once-latch, ingress finalize offload (slow finalizes no
  longer starve heartbeats), run-store lost-update fence + single-process
  contract ledger (docs/10 §6), entrypoint resilience (guarded first
  heartbeat, capture-truncation warning), the test-gap closure (grace-cycle schedule-skip + poll rotate site,
  live Redis `clear_redis`, AUD-016 WorkspaceStore bind, cycle_lock wiring),
  and minimal SPA concurrency (instance-qualified refs + self-healing
  projection + stale-response guards). **SIGTERM artifact-flush** ships in
  the post-#137 leftover train (unit + live Stop/SIGTERM on the operator
  stack). Still deferred: a `/health` warning on repeated exit-10s with
  auth-suspicious stderr (soft signal, deliberate backend change). **One
  accepted risk unchanged (founder ruling):** agent-container CPU/mem/pids
  limits stay deferred (ISSUES #20) — Dagu's container schema rejects them
  and app concurrency caps remain the only throttle, revisited when Dagu
  ships host-config support. Multi-admin config `If-Match`/versioning is
  ledgered as accepted single-operator scope (admin/spa/DESIGN.md).

### Field evidence (operator-self-reported)

Production use reported by the founder-operator, 2026-08. Evidence class:
**self-reported** — the work substrates are proprietary, so no receipts are
published; shapes only, no invented metrics. Each picture is pinned to the
deploy that ran it, and none of it credits ADRs merged after that deploy.

- **Multirepo legacy delivery** (deploy: **v0.3** `df08c9a`; Linear + GitLab;
  ~20-hour composed run): a ~500k-line legacy multirepo product took a
  brand-new customer-facing feature from scratch — one Linear project
  decomposed into eight issues, each routed to its own work repo (the 0-or-1
  rule, not N repos per mission), with merge requests opened on three GitLab
  repositories. Staffing: Claude Code (Fable) on ONBOARD/PLAN/REVIEW, Grok
  Build (Grok 4.5) on EXECUTE. Outcome: end-to-end delivery; not green on
  first merge — the follow-up adaptations needed were genuinely small.
- **Dual-board multi-PMO production** (same deploy and window): two Linear
  teams as two PMO instances on one stack — Development carried the multirepo
  delivery while Customer Success ran deep research on point customer tasks.
  Operator judgment: stable under concurrent dual-team production use. This
  is multi-instance on one dedicated host, not multi-tenant SaaS; the
  colliding-identifier completion below stays open.
- **Limited model + Gitea Issues corpus run** (deploy: **v0.2.5** `d3361f2`,
  post-ADR-0018 fault-classification hardening — before ADR-0026/0027
  existed): a local stack pointed the Grok Build harness at **Qwen3.6-27B**
  on a local vLLM OpenAI-compatible endpoint (`08` §8) and worked a **Gitea
  Issues** board over a large messy text corpus (~150 folders / ~3k files /
  ~2M lines of *input* material), extracting dialogue snippets. Run errors
  still occur and stay localized; fault classification and re-dispatch kept
  the loop productive. Operator judgment: works uncannily well for similar
  tasks on limited models and experimented harness pairings — field comfort
  with the recovery envelope, not a claim of zero failures or universal
  model support.

What these do **not** claim: injection-proofness (`14` §3); multi-tenancy;
zero harness failures; that every local model works; a controlled comparison
against bare CLI sessions (`19` §7); or anything about features merged after
the pinned deploys (ADR-0026/0027, the freshness gate, and handoff notes
among them).

### Still open (residuals)

Not new features — demos or proofs still owed from Layers 1–2 (milestones or
the v0.2 trailer list).

- **M9 additivity residuals**: dual-Linear production use is field-reported
  (Field evidence above) — the operational proof is no longer owed. Still
  open, specifically: a deliberate **colliding-identifier completion** across
  two instances (instance-prefixed branches, end to end) and the
  dual-workspace / dual-key sandbox ceremony. **⏳**
- **M10 live two-forge merged-PR demo** and **M11/M12 full live model golden
  paths** on the operator's box: machinery proven hermetically + contract
  batteries; live GitLab MRs are field-reported (Field evidence above),
  but the two-forges-in-one-instance demo and the token-spending golden
  paths remain **⏳**.
- **Dedicated skill sources (2026-08-14)**: external skills moved off
  repo cards onto their own `skill_sources` connections (ADR-0016
  addendum 2) — the `skills_subdir` facet is gone; the Skills page
  manages sources; read tokens ride the new `skill:` secret scope. **⏳**
- **Memory + Cron (PLAN_MEMORY)**: schema, sourcing, claims conveyor,
  merge guard, CronService, and admin surfaces are in the tree. **Not
  called shipped** until the throwaway-box A/B has receipts
  (`PLAN_MEMORY.md` §14–§15). **⏳**
- **Fresh-`/data` operator-drill re-run** after the post-v0.2 surface growth
  (profiles, skills, Gitea Issues, per-PMO intake, default board, composer,
  freshness, handoff): **⏳** non-gating trailer. Dual-team production and
  the corpus run do **not** substitute for the wipe-and-reconfigure
  stranger-operability ritual.
- **Field-evidence detail pass**: the pictures above are shape-only pending
  founder decisions on publishable detail (mission keys / MR numbers, exact
  dual-Linear topology, quotable numbers) and the field-derived host-sizing
  guidance for `13`/`18`. **⏳**

---

### Candidates — harness platformization (H1–H5)

**Status: H1 + H2 are committed** as the prerequisite of the
[2026-08-15 launch roster](#candidates--launch-roster-2026-08-15) (Pi,
OpenCode, Qwen Code). H3–H5 stay optional (H3 only when a real CLI needs
multi-env / settings-file auth). The H1–H5 design below is unchanged; the
roster is what picked the track up.

**Why it exists on the roadmap.** The control plane already has a deep harness
registry (`app/devcake/harness.py`: image, credentials, OAuth, `skills_dir`,
`default_model`). The Dev side has a package layout but still dispatches on
stringly `if harness == …` across argv, render, tokens, fault, dump, and
`dev_entrypoint.py` — with **unknown ids silently falling through to the Claude
path**. Adding a fourth template today is a vertical slice that is
intentionally expensive (docs/08 §9). Interest in more coding CLIs (qwen-code
and peers under consideration) makes that tax worth *documenting* as a possible
investment: fix the platform once, then each CLI is a dialect module + image +
captures — **if** we decide the Nth harness is worth building.

**Standing premise (do not invert):**

| Layer | Owns | Does *not* own |
|---|---|---|
| **App registry** (`HARNESSES`) | Image tag, credential requirements, OAuth flow, skills dir, default model | Stream parsers, argv templates, fault predicates |
| **Dev dialect** (image package) | Argv, live render, token extract, result text, transcript dump, fault / API-status classification | Secrets storage, Dev Type CRUD, dispatch spine |
| **Capture gate** | Verbatim CLI fixtures + intended verdicts | Hand-written “plausible” JSON as truth |

**What does *not* change when a new CLI lands** (reminder for implementers):
dispatch spine, Redis runspec, plan materialization to `PLAN.md`, skills
install into `skills_dir`, MCP free-text setup, SPA combobox driven by
`GET /harnesses`. **What always changes:** registry entry, config id set,
Dockerfile + Bake target, a dialect implementation, captures, docs/08.

#### H1 — `HarnessDialect` protocol (highest leverage)

**Goal:** one deep module seam for all harness-specific container behavior.
**Implements:** evolution of `images/common/devcake_dev/harness/` + ADR-0018
consumers; capture rig and production share the same dialect object (argv
already does).

Sketch of the seam (names indicative):

```text
HarnessDialect
  argv(prompt, *, plan_mode, model, extra, out_dir) -> list[str]
  render_line(raw) -> str | None          # live terminal / Dagu log
  parse_run(out, *, exit_code, ...) -> HarnessRunView
    # result_text, dump, token_report, fault, api_error_status
```

`DIALECTS: dict[str, HarnessDialect]` is fail-closed: unknown `DEVCAKE_HARNESS`
aborts with a clear error — **no Claude fallback**.

**Out of scope:** declarative JSONPath “configure a harness without code”;
out-of-repo plugin harnesses (app↔image remain lockstep — docs/13).

**Exit criteria:**
- [x] All three existing templates are pure dialect modules; `dev_entrypoint`
      has no `if harness ==` for parse/fault/dump/render/argv.
- [ ] Capture rig imports dialects only; no duplicated argv construction
      (argv goes through `harness_argv`; dump/last_message still branch, and
      unknown `--harness` still falls through to the Claude dump).
- [x] Planted unknown harness id fails closed (unit test).
- [x] Full harness capture suite green; `bake images` + entrypoint import smoke.

**Demo:** delete the Claude fall-through; suite still green; intentional
unknown-id run dies loudly.

#### H2 — Registry is the single source of harness ids

**Goal:** config schema cannot drift from `HARNESSES` keys.
**Implements:** `app/devcake/harness.py` + `config.py` `DevType.harness_template`.

Shipped: `DevType.harness_template` validates against `HARNESSES` keys
(no parallel Literal). A new template is one registry entry + dialect +
Bake target; config accepts the id automatically.

**Exit criteria:**
- [x] Adding a registry key alone is what config accepts (or a single shared
      id tuple imported by both).
- [x] Structure/unit test: every `HARNESSES` key is a valid `DevType.harness_template`
      and every accepted template has a dialect (H1) and a Bake image name.
- [x] SPA still loads ids from `GET /harnesses` (no hard-coded combobox list).

#### H3 — Credential registry extensions (only where declarative)

**Goal:** support CLIs whose headless auth is multi-env or settings-file shaped
without inventing per-Dev-Type special cases in dispatch.

Optional `Harness` fields (grow only when a real CLI needs them):

| Extension | Purpose |
|---|---|
| `credential_env` any-of (existing) | One of several keys is enough |
| `credential_env_all_of` (new) | e.g. API key **and** base URL required together |
| `config_files` / template materialization (new) | Entrypoint writes `~/.…/settings.json` from runspec or a stored secret blob |
| `oauth` (existing) | Device-code flows that produce an auth file |

**Out of scope:** putting argv or token-extraction logic in the app registry.

**Exit criteria:**
- [ ] Schema + `dev_type_status` / `credentials_ready` honor any-of vs all-of.
- [ ] At least one path materialises a config file into the Dev (secret blob or
      generated minimal settings) with 0600 semantics and redaction registration.
- [ ] Docs/08 §4 and docs/14 updated; no stronger security claims than `14`.

#### H4 — Capture campaign as the formal “harness ready” gate

**Goal:** “this harness is supported” means a measured fixture matrix, not a
docs paragraph.

Minimum matrix per template (healthy, empty completion, auth failure,
rate-limit / hard HTTP, tool-only work, plan mode, turn-budget or equivalent):

- verbatim stdout under `app/tests/fixtures/harness_streams/`
- sidecars for meta / stderr / dump where needed
- rows in `test_harness_captures.py` with **intended** verdicts (never rewrite
  expected class into the sidecar)

**Exit criteria:**
- [ ] Docs/08 §9 (or a short `08` annex) states the matrix as the gate.
- [ ] CI fails if a registered dialect lacks the minimum capture set (or an
      explicit `capture_exempt` with justification — `hello` only).
- [ ] Pin bumps that change stream shape require re-capture (documented).

#### H5 — Mechanical Bake / image scaffold

**Goal:** boring checklist becomes hard to miss.

Convention (script optional):

1. `images/Dockerfile` stage name = harness id  
2. `docker-bake.hcl` target + `images` / `all` groups  
3. `HARNESSES[id].image = f"devcake/dev-{id}:${DEVCAKE_TAG}"`  
4. `ENV DEVCAKE_HARNESS=<id>` in the image  

**Exit criteria:**
- [ ] Documented in docs/08 §9 and docs/13 image matrix.
- [ ] Optional `scripts/new_harness.sh <id>` scaffolds empty stage + bake +
      registry stub + empty dialect module (no fake stream logic).
- [ ] Tripwire or checklist test that every `HARNESSES` image has a bake target.

#### If pursuing new harness templates

If/when a fourth CLI is worth shipping, prefer landing H1–H2 first (or in the
same change set as the first new dialect) so the slice is repeatable:

- Each new CLI is: characterize headless contract → capture matrix → dialect
  module → registry + Bake → docs/08 tables → operator smoke.
- **Experimental on this track (not launch-supported):** `pi`, `opencode`,
  `qwen-code` (each a dialect + Bake target + capture matrix; resume stays
  off until a capture pair). Launch-supported harnesses remain
  `claude-code`, `grok-build`, `codex`. Cursor Agent is deferred (trigger
  in the roster note).
- **CLI candidates still under consideration** (not commitments): other
  agentic terminal CLIs. A candidate that is Claude-stream-adjacent may
  share helpers *after* captures prove it — never by silent alias.
  Multi-provider / settings.json auth is an H3 consumer.
- **Not a new harness:** pointing an existing template at a new model or a
  local OpenAI-compatible backend (`DevType.model`, secret_env, `08` §8).

**Explicit non-goals for this track:**

- One universal stream parser for all CLIs (codex / grok / claude proved
  dialects need real code).
- Plugin harnesses outside the monorepo.
- Expanding product scope (webhooks, SSO, etc.) under the harness banner.

**Suggested sequencing *if* this track is picked up:** H1 + H2 first (same PR
train if small); H4 codifies what ADR-0018 already started; H5 is cheap; H3
when the first multi-auth CLI is actually implemented. A first new template may
pay for H1–H2 in-tree rather than as pure refactor — prefer that over a
long-lived incomplete dialect API.

---

### Candidates — launch roster (2026-08-15)

**Status: committed campaign, not yet shipped. In-tree support for the
new PMOs and harnesses is experimental** until each increment's live
battery has been run on the operator stack (hermetic pytest is necessary
and not sufficient). Launch-supported remains Linear + Gitea Issues and
`claude-code` / `grok-build` / `codex`. Host CLIs characterize only;
production truth is the baked image / in-container adapter.

**Build (this campaign) — experimental until the gate passes**

| Kind | Names | Registry id | Gate |
|---|---|---|---|
| PMO | GitHub Issues, GitLab Issues | `github_issues`, `gitlab_issues` | live `contract_tests_pmo.py`: row 12 never skippable; row 8/13 skip iff `attachments_supported` is false; row 10 matches row 14 (`relations_supported`, probed from the live token — not hardcoded). No blanket “documented capability skip”. |
| Platform | `HarnessDialect` + registry-as-id-source | — | existing three capture batteries + **Grok Build live ONBOARD** |
| Harness | Pi, OpenCode, Qwen Code | `pi`, `opencode`, `qwen-code` | H4 capture matrix from the **baked** image + hello + ONBOARD + INV-5 report |

Copy the `gitea_issues` profile (docs/05 §9): issue-only, `open→backlog`,
`DEVCAKE-*` labels, `team_key=owner/repo`, `global_ids=False`. Separate
packages — do **not** add issue methods to `ForgePort`. Do **not** extract a
shared Issues port until the second forge-issue adapter exists (§9.6).

H1 lives in `images/common/devcake_dev/harness/` (Dev hexagon). Do not add
`ports/harness.py` on the app. `app/devcake/harness.py` stays image / creds /
OAuth / `skills_dir` only.

**Anticipate (no registry entry, no adapter)**

- **Cursor Origin** (`cursor-origin`, never `origin`) — waitlist-only as of
  2026-08. Intake against `ForgePort` (docs/06): standard git clone/push; PR
  findable by head branch; squash merge; formal approve as a second identity;
  mergeable tri-state; **HTTP** API for the app (MCP is Dev-side only);
  official CLI; token prefixes; `server_side_conflict_resolution` (Origin
  demoed agent-side conflict/CI repair — if present, do not trust
  `mergeable()==False` as our conflict). First experiment when a preview
  exists: GitHub adapter + `api_base` if they speak a GitHub-shaped API.
  Standalone PRs only in v1; stacks are a later design. See
  [Origin intake](#origin-intake-cursor-origin).
- **Jira Cloud** (`jira`) — ISSUES #35 first. See
  [feed fidelity](#issues-35--feed-fidelity-port-note). No adapter until a
  live md↔ADF (or sidecar) measurement exists. Cloud only; Data Center is a
  second product. Jira Project ≠ Linear Project (first adapter issue-only).

**Deferred (named, with trigger)**

- **Cursor Agent** (`cursor-agent`) — characterized (`agent -p --force
  --output-format stream-json`, `CURSOR_API_KEY`, `--plan`). Deferred because
  Grok Build already covers the SpaceX/xAI path and the Anysphere acquisition
  may fold the CLI. Trigger: the CLI is still a distinct headless product
  after the close, *or* a launch buyer needs `agent -p`.

**Scratched (do not resurrect without new evidence)**

- Monday.com — fails docs/05 §0 (b)/(c)/(d) (board-schema columns, same-board
  dependency column, poor backtick fidelity).
- Goose — general agent, not a coding-CLI peer of Grok Build.
- Prime Agent — 2026-08-05, built on Pi, self-modifying Continual Harness
  fights isolated receipted runs.

**GitHub Issues attachments — ruling (2026-08-15), not an open choice.**
There is no official public REST/GraphQL “upload file to an issue” API. The
web UI uses undocumented `uploads.github.com/user-attachments`. **C
(unofficial upload) is refused.** **A** (wait for an official API) is the
rejected alternative. **B** is the accepted residual:
`attachments_supported=False`; `comment_max_chars=65536`; the feed chokepoint
posts the full body as sequential `Part i of n` comments (never a truncated
dump, never a 422). The operator must see the residual (`operator_note` + live
health flags). Unofficial `uploads.github.com` remains refused.

**Spike evidence (2026-08-15, personal `fidecastro` on github.com +
gitlab.com — official APIs only).** Throwaway GitHub repo
`devcake-pmo-contract-gh-20260815-035138` (issues closed; `gh` token lacks
`delete_repo`) and two GitLab projects (deleted).

| Need | GitHub (measured) | GitLab (measured) |
|---|---|---|
| Replace-all labels | PUT `/issues/{n}/labels` works | PUT issue `labels=A,B` works |
| Marker `` `devcake:v1` `` | comment body **byte-exact** | note body **byte-exact** |
| Attachments | GET `/issues/{n}/assets` and `/attachments` **404**; comment octet-stream **400**. No official upload. | POST `/projects/:id/uploads` **201** (`url`, `full_path`, `markdown`). Web path + PAT → **403 HTML**. Download **is** `GET /api/v4/projects/:id/uploads/:secret/:filename` → **200** `application/octet-stream`. |
| Blocked-by | Works on a **personal** repo if `issue_id` is the global numeric `id` (not the number). Number → 404. Duplicate → 422 `already been taken` (treat as success). | `is_blocked_by` / `blocks` → **403** on free gitlab.com (`Blocked issues not available for current license`). `relates_to` works — **not** blocked-by. **Do not start `relations_supported=False`.** Unprobed means the domain gate will attempt `create_relation`; a 403 no-ops and latches the flag off. Health reports `unprobed` / `on` / `off`. |
| PR-as-issue | `/issues` includes PRs (`pull_request` key) — filter | Issues API does not list MRs |
| `pmo_id` | issue **number** for URLs/keys; dependency POST needs global **id** (adapter-internal lookup) | issue **iid** for paths; still `global_ids=False` (iid collides across projects) |

GitLab row 13 is admissible. GitLab row 14 runs only when the live probe
sets `relations_supported=True` (Premium / self-hosted EE); Free tokens
stay False and the operator is told child missions will not block each
other. GitHub attachments are **B** (see the ruling above).

#### Origin intake (Cursor Origin)

Reserved id `cursor-origin`. **Do not** add to `_forge_classes()` until every
row below has a measured answer.

| `ForgePort` need | What to measure on a preview |
|---|---|
| `get_pr_by_branch` | PR/MR (or equivalent) findable by head `devcake/{INSTANCE}-{key}` |
| `merge` | squash (or documented equivalent) + observed-merged |
| `approve` | second-identity review; `self_approval_blocked`? |
| `mergeable` | tri-state vs boolean; does an agent rewrite the branch first? |
| `default_branch_protection` | readable? which token scope? |
| HTTP vs MCP | app-side `ForgePort` **must** be HTTP; MCP-only = no adapter |
| CLI | official binary for EXECUTE `pr_instructions`; do not bake Graphite `gt` until they say so |
| Tokens | prefixes for redaction / SPA paste guard |
| Stacks / merge queue | v1 treats Origin as standalone PRs; stacking decomposition is a later design |

#### ISSUES #35 — feed fidelity (port note)

Docs/05 §0 (d) and docs/00 already say markdown-fidelity markers are a **port**
requirement, not adapter folklore. When a Jira (or other ADF/rich-text)
adapter is actually started, declare a strategy on `PMOCapabilities` — do
**not** implement the field in this campaign:

| Strategy | Where machine markers live | Use when |
|---|---|---|
| `raw` | Comment body, byte-stable | Linear, Gitea/GitHub/GitLab Issues |
| `transcoded` | Comment body after a **measured** md↔vendor round-trip | Only if every `` `devcake:…` `` marker survives live |
| `sidecar` | Vendor entity property / small attachment is source of truth; human comment is pretty text | Default for Jira |

Domain never grows `if system == "jira"`. Poll/derivation reads markers from
the strategy the adapter declares. Sidecar is the honest Jira default.

---

### Deferred (later / conditional)

- **2026-08 evaluation candidates** (recorded with the campaign entry above;
  file:line evidence in the evaluation ledger): **per-run Dev networks**
  (closes cross-Dev reachability incl. the post-ADR-0023 DevTools scenario —
  ICC-off measured broken: it severs Dev→Redis) · **per-run ingress
  streams** (closes the malicious-`MAXLEN` trim vector for good) · **brake
  cycle-skip backoff** so the ADR-0018 throttle arm acts at
  `max_concurrency: 1` (founder kept current design) · **composition-root
  decomposition** (`api/services.py:build_services` still constructs 15+
  graph objects in one function; ADR-0028 already removed the old import-time
  construction) ·
  **unifying the three failure-taxonomy encodings** (numeric exit /
  `error_class` string / reconcile's stderr regex) ·
  **cycle-lock harness-secret / credential-upload / OAuth-land writers**
  (connection PUT/DELETE/clear and config/profile apply already hold
  `poll_rt.lock`; harness keys are live-read at dispatch) ·
  **`dispatch.py` vertical split** (`domain/orchestrator/dispatch.py` is the
  ~1k-LOC gravity well left after the façade removal; split at public seams,
  tests at the seams only) ·
  **conftest.py / event-loop test hygiene** (module-import loop ownership) ·
  **`${VAR:-default}`-guarded volume name in dev-run.yaml** (AUD-020 —
  verify Dagu expansion support first; see the #87 lesson).
- **Local-backend operator recipe** — distill the field-exercised Qwen/vLLM +
  Grok Build pairing (`08` §8, Field evidence above) into a reproducible
  operator page: secret env, base URL, model string, known footguns. An
  experimented pairing made repeatable — not a supported-matrix claim.
- **Webhook ingestion** — PMO `watch()` / webhook `ChangeEvent` seam replacing
  polling (+ tunnel guide). Multi-PMO multiplies poll cost; strong candidate
  among deferred items, independent of any harness-platform work.
- **Additional PMO adapters** beyond the launch-roster pair (GitHub Issues
  + GitLab Issues are the 2026-08-15 campaign, above). Height / Shortcut /
  Plane remain Linear-class candidates. Jira Cloud waits on ISSUES #35.
  Monday.com is **scratched**. Copy the `gitea_issues` profile (pure
  `PMOPort`) for any future forge-issue sibling.
- **N repos per mission** — cross-repo atomicity (one PR per repo, set-
  approval, merge ordering); still capped at 0-or-1 work repo per mission.
- **Per-run scoped forge tokens** and the rest of `14` §7 — companion to
  internal per-mission tokens; revisit when threat model demands it.
- **Network egress allowlists / Dev egress proxy** (ISSUES #16 + radar) —
  optional Zone B defense-in-depth: default-deny outbound and/or
  **credential-injection MITM** so Devs hold proxy tokens rather than real
  model/forge keys. Candidate implementation class:
  [iron-proxy](https://github.com/ironsh/iron-proxy) (Apache-2.0; Hermes Agent
  already integrates a host-daemon form). **Not** a sandbox product rewrite and
  **not** a substitute for zone C branch protection (`14` §0–§3, §6, §11).
  Promote only after a time-boxed spike proves: (1) enforcement beyond DNS
  alone (nftables/TPROXY or measured non-bypass for our harness set), (2)
  coexistence with `devcake_runtime` (Redis, otel-collector, internal Gitea)
  without naive private-CIDR denials, (3) inventory of what still must be
  real secrets in-container (OAuth files, uncovered auth schemes, MCP
  `secret_env`), (4) no silent fallback to real keys when “enforced,” (5)
  operator docs that do not overclaim isolation. Default-off / ops overlay
  until then; opportunity cost is high (CA lifecycle, allowlists, Dagu spawn
  path, every TLS client in the Dev image).
- **Additional log-connector backends** (Loki; others on demand) in the
  standalone plugin repo <https://github.com/fidecastro/devcake-logs-mcp>
  (`LogBackend` seam; core MCP ports already shipped).
- **Priority-conditional Dev Type assignment** (e.g. Urgent EXECUTE → stronger
  Dev Type — relaxes 1 Mission Type → 1 Dev Type). The **instance** dimension
  shipped as ADR-0019 (per-PMO override rows); a condition language did not.
- **Per-instance doctrine knobs beyond assignments** — adoption mode and
  concurrency are still deployment-global; ADR-0019 lists them as open
  candidates if two boards ever need different intake/concurrency doctrine
  (until then: second deployment). **`auto_merge` is per-repo** (ADR-0020),
  not a per-PMO candidate.
- Admin panel **OIDC/SSO** (basic auth remains the dedicated-host story —
  `14`).
- **First-class OTel metrics layer** — when dashboards need pre-aggregation
  or long retention (`12` §4 still SQL-over-spans).
- **SQLite `StatePort` swap** — if run history outgrows files.
- **Public-release hygiene** — LICENSE, SECURITY.md, CONTRIBUTING, CHANGELOG,
  SBOM (ISSUES #38) if audience expands.
- **Internal-forge orphan sweep** — reconcile Gitea org repos/svc users vs
  `/data/secrets/internal_forge/mission-*.json` (pre-v0.1.1 Clear leak;
  leak path itself is fixed).

### Discarded (do not resurrect without new evidence)

- **Scout Dev experiment** — not engineering: assign ONBOARD to a cheap-model
  Dev Type from the admin panel as an ops experiment any week.
- **Mid-run Dev→PMO write relay commands** — superseded by F4 internal fallback
  forge mid-run persistence. Revisit only if humans need mid-run progress
  comments *in the PMO feed itself*.
- **Internal Mission worktree as an activity mirror** (original F5 shape) —
  superseded by F4. **Partially un-discarded 2026-07-18 by ADR-0014 D4:**
  per-mission `activity-*` repos are write-only records (app-pushed, deleted
  on Clear, never read back) — not the rejected read-back mirror.
- **Silent unknown-harness → Claude dialect fallback** — anti-pattern to
  remove under H1; do not reintroduce “maybe it’s Claude-shaped” shortcuts
  without captures.
- **Declarative-only harness plugins** (JSON argv + JSONPath tokens, no
  dialect code) — rejected: fault classification and stream churn need real
  code (ADR-0018 evidence).
- **One universal stream parser / harness SDK for all vendors** — same
  rejection; deep dialect modules yes, one parser no.

*(Note: `auto_merge` was originally slated post-v0 but is a confirmed v0
requirement — it ships in M5/M6. It gates the **app's** auto-merge of an
approved PR, not the Dev's ability to open PRs — see docs clarity 2026-07.)*
