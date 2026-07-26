# 16 — Roadmap: Milestones M0–M12

> **Audience:** the implementing agent(s) and the founder. Each milestone leaves a demoable, committed system; exit criteria are mechanically checkable.
> Format per milestone: Goal · In scope (docs it implements) · Out of scope · Exit criteria · Demo.

## M0 — Compose skeleton + observability spine

**Goal:** all five services up, traced, healthy. **Implements:** `13` (compose), `12` §1 (pipeline), `11` shell.
**Out of scope:** any business logic.

Exit criteria — **all verified 2026-07-11 (M0 complete)**:
- [x] `docker compose up -d` from a fresh clone + `.env` → all services healthy.
- [x] `app` emits a stub `poll.cycle` trace visible in OpenObserve (verified via `_search?type=traces`).
- [x] Admin panel serves the three tabs *(M0-era layout; current SPA is six pages — Overview, Missions, Runs, Repos, Config, Logs — `11-admin-panel.md`)*; the Executor and Logs tabs' buttons open the Dagu and OpenObserve UIs in new browser tabs (confirmed decision: buttons, no iframes). Basic auth verified: 401 without credentials on both the SPA and `/api`.
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

- **2026-07-12 — Traffic control (`adr/0007`):** Mission ordering via native `blocked by` relations (ONBOARD decomposition declares `blocked_by`; scheduler gate honors any relation, human-added included) · `DEVCAKE-NEEDS-HUMAN` hand-off label + `human_needed` outcome (tenth label) · intake pause toggle (`intake_paused` + admin Traffic control section) · comment-provenance sentinel `` `devcake:v1` `` with 🧑/🤖 markers in `ACTIVITY.md` · Relations Mapper service (`MAPPER` run kind: interval + manual trigger + Dev Type combobox + on/off in the admin panel). Requires a dev-image rebuild (new legal outcomes in the entrypoint).
- **2026-07-12 — Traffic-control hardening (`adr/0007` addendum), same day, post-adversarial-review:** app-side `LEGAL_OUTCOMES` trust boundary · branch-protection verification + out-of-pipeline-merge tripwire (docs/13 §8a, docs/14 §2) · paginated Linear reads + relations-page warnings · `gate_map` as an always-fresh poll artifact + dependency-cycle detection with header banner · hand-off evidence requirement + escalating warnings (never auto-park) · seeded `junior-dev` + `MapperService` (lock, post-success watermark, store-derived degradation) · quote-aware sentinel classification · project-update baton passes (verified live) · stateful pause banner with in-flight count · config deep-merge + mapper Dev Type delete guard.

- **2026-07-13 — Modularization (`adr/0008`):** the hexagonal layout is now real (`app/devcake/` with `domain/`, `ports/`, `adapters/`, `api/`; `domain/*` has zero runtime adapter imports) · pluggable PMO + forge adapter registries (`adapters/registry.py` — `system`/`forge` are registry-validated open strings, not literals) · config schema v2: plural `pmos:`/`repos:` with exactly-one enforced, v1→v2 migrated on load with a `config.yaml.v1.bak` backup · MissionRef-unified `PMOPort` + typed `ForgePort` DTOs (`PullRequest`, `ForgeDescriptor`, `mission_branch()`) · registry-fed admin Config tab (`GET /api/v1/connections/registry`) with config hot-reload on PUT.

- **2026-07-13 — v0 crystallization:** repo-wide cleanup before v0.1 work. Bug fixes (redaction-gap alarm on unreadable secrets files; datetime-safe attempt counting; `security.MASK` single-sourced; background-task death logging on config reload) · telemetry brought up to the "everything traced" invariant (spans now *cover* the PMO writes they name; new `ingress.handle`, `sweep.merge_retry`, `mapper.periodic`, `ingress.forged_drop`, `ingress.poison` spans — `12` §2 is the normative inventory) · **all legacy/compat surfaces removed** (founder decision): the old-image protocol dual-modes (`DEVCAKE_FORGE` discriminator, `forge_dialect()` fallback, pre-marker decomposition regex) AND the v1→v2 data migrations (config auto-migration, run-record secret scrub, Redis legacy scrubs). Consequences: app + dev images MUST rebuild in lockstep (`13` §8); a v1 `config.yaml` is refused at boot with hand-migration instructions; pre-v2 run records quarantine at boot (`10` §5) · `blocked_reasons` exposed in `/health`; meaningless `config_valid` dropped · dead `admin/site/` shell deleted · docs re-baselined against the code.

- **2026-07-14 — ISSUES_LIST hardening + build overhaul:** the 38-item ISSUES_LIST review closed out (finalize-stall watchdog backstop, label-swap write-path pagination, all OO alerts backed by real spans, dismissable `/health` `security_warnings`, `domain/reconcile.py` extraction) · orchestrator god module split into the `domain/orchestrator/` package (ISSUES #36) · Docker Bake build system merged (bake-only images via `docker buildx bake all`, multi-stage Dockerfiles, GHA bake CI — collaborator contribution).

- **2026-07-14 — RunBootstrap + secondary ports (`adr/0008` follow-up, PR #1):** `ExecutorPort` / `StatePort` / `MessagingPort` / `RunFinalizer` Protocols under `ports/` · deep `domain/run_bootstrap.py` owns the dispatch spine (ACL → auth digest → durable `StatePort.save` → `ExecutorPort.start`) for all four flavors (hello, mission, mapper, OAuth) · `RunManager.set_finalizer` breaks the concrete `mission_mgr` late-wire cycle · tests at `tests/test_run_bootstrap.py` · docs/01 §3 + docs/04 §3.1 re-baselined.

- **2026-07-12 — Harness registry (admin authoritative):** found live — changing a Dev Type's harness in the admin panel didn't change what ran (dispatch used the stored `docker_image`; harness selection was image-baked). Reworked: `app/devcake/harness.py` registry is the single source of truth (image + credential requirements + OAuth flow per `harness_template`); `DevType` slimmed (no stored image/credential config; legacy YAML keys dropped on next save); dispatch sends `DEVCAKE_HARNESS` in the run spec (overrides the baked ENV); OAuth became per-Dev-Type (`POST /oauth/dev-types/{name}/start` — fixes credentials landing in the first same-harness Dev Type's dir); Dev Type card shows the derived image + live credential checklist (`GET /harnesses`, enriched `GET /dev-types`).

## v0.1 — feature specifications (F1–F5) + milestones M8–M12

*(Consolidated + triaged 2026-07-14, revised the same day after a devil's-advocate round, then recast as milestones. Standing premise: **there are no deployments** — safecontract was the founder's own test — so v0 parity is explicitly NOT preserved: schema v3 breaks wholesale, shims and fallback modes are deleted rather than deprecated. F1–F5 below are the feature specifications, in implementation order — agnosticism before multiplicity, multiplicity before the internal fallback forge's zero-repo payoff, GUI config last since every prior feature adds config surface it must cover. M8–M12 are the implementation plan in the M0–M7 format: each leaves a demoable, committed system with mechanically checkable exit criteria. The four hardening items triaged into v0.1 are folded into M8 (PR #1, ISSUES #13, #29) and M12 (ISSUES #30).)*

### v0.1 feature specifications

**F1 — Forge-agnosticism hardening.** Nothing forge-specific outside `adapters/` — DevCake must be completely forge-agnostic, and the known violations get corrected first, ahead of everything else. Residuals (audited 2026-07-14): `config.py` defaults (`forge: "github"`, `token_env: "GITHUB_TOKEN"`, github.com-specific URL validation) → derive from the adapter registry/descriptor; the `api/main.py` read-only-token security-warning copy hardcodes GitHub/GitLab wording → registry-fed; `ports/forge.py` default `git_email` (github noreply) → per-descriptor. The CI tripwire asserts on **behavior, not strings**: no `adapters.github`/`adapters.gitlab` imports outside the registry, all defaults resolve through descriptors (a forge-name literal grep is at most a secondary check with an explicit allowlist — comments and docstrings legitimately name forges). `ForgeCapabilities` is deliberately *not* designed here — it gets extracted from real divergence during F4; designing capability negotiation before a third forge exists is speculation.

**F2 — PMO port completion: multi-PMO, additive.** Segregate Linear fully and make DevCake PMO-independent; one instance oversees N≥1 PMO systems at once. Schema v3 is a **wholesale redesign, not a validator relaxation**: `pmos:`/`repos:` become instances-with-identities (operator-chosen instance names), `MissionRef` carries PMO-instance provenance end-to-end, and `mission_branch()` prefixes the instance name (`LINEAR-DEV-17`) so identifiers can never collide across PMOs; the singular `config.pmo`/`config.repo` shims are deleted on day one — no deprecation period. Formalize the **PMO capability contract** any candidate system must satisfy: (a) inputs map straightforwardly to Missions as the unit of work; (b) labels or an equivalent concept assign Mission Steps; (c) traffic control via clear blocked_by relations/dependencies; (d) a reliable activity feed for moving/storing files and data. Encode it as a documented port contract + conformance battery (grow `scripts/contract_tests_pmo.py` into the adapter acceptance gate), plus `PMOPort.cancel_mission()`. **Scope fence (founder decision 2026-07-14): no new PMO adapters ship in v0.1** — Linear stays the only adapter; additivity is proven by running **two Linear instances** (e.g. two sandbox teams) on one DevCake. Cross-PMO `blocked_by` is explicitly unsupported in v0.1 — PMO instances are independent; federation is its own project.

**F3 — Any number of repos, including zero.** An instance configures 0..N repos (schema v3); per-mission resolution assigns **0 or 1** configured repo per mission (the mechanism — assignment config vs. mission metadata — is a **founder decision**), with the resolved forge adapter + credentials wired per-run into the runspec. A mission resolving to zero repos routes to F4's internal fallback forge — downstream (EXECUTE/REVIEW/PR mechanics) never sees a repo-less mission. N-repos-*per-mission* is explicitly out of scope (deferred — cross-repo atomicity is its own project). Ships paired with F4: the zero-repo dispatch path stays gated until the fallback exists. Absorbs the old "multi-instance runtime wiring" item.

**F4 — Internal fallback forge: bundled Gitea.** Gitea joins `docker compose` as a long-lived service. Any mission that resolves to no configured repo gets a repository auto-created on the internal Gitea at intake (per-Mission, reused across attempts and rework — the PR-reuse mechanics from M4 carry over), plus a per-Mission machine user with user-scoped tokens and a collaborator grant on that repo (Gitea does not mint repo-scoped tokens; `14` §2). Devs receive it as a *perfectly ordinary forge repo*: EXECUTE & REVIEW run their standard PR mechanics on non-code artifacts with zero special-casing — simultaneously the strongest live test of F1's forge-agnosticism and the substrate for the **non-developer workload testing planned for v0.2**. **Because the fallback forge may not be observed by the end-user at all, deliverables must flow back to the PMO:** when a mission on the internal forge reaches its REVIEW-approved merge, the changed files are packaged (zip of the merged change set) and attached to the PMO activity feed (attachment-first policy) — the PMO stays the one place the user looks. Requires the Gitea `ForgePort` adapter (registry entry + `ForgeDescriptor` dialect); support for external/user-supplied Gitea instances comes free. `ForgeCapabilities` is extracted here, from observed three-forge divergence. Gitea admin credentials are a stack bootstrap secret (`.env`, F5 exception). Open **founder decisions**: internal-repo retention/GC after mission completion; whether internal repos surface in the admin panel. Bonus: the zero-repo golden path needs no external forge credential, though it still uses Linear and real model credentials. *(Replaces the earlier "internal Mission worktree / activity mirror" design — see Discarded.)*

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
**Out of scope:** new PMO adapters (post-v0.1); cross-PMO `blocked_by`; webhooks.

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
- [x] Gitea is a bundled compose service (`gitea/gitea:1.24.7-rootless`, digest-pinned): live-verified healthy from the stack, admin bootstrapped via the `docker-setup.sh → migrate → admin-create` wrapper (GITEA_ADMIN_* stack secret), container logs in OO. The wrapper ordering + idempotency were live-probed first (M11.0).
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

## v0.2 — FINAL (tagged 2026-07-19)

The consolidation release: everything on `main` since v0.1.1, responding to an external skeptical review and successive adversarial review rounds. **All four hard release gates met** — the two live E2Es (ADR-0012 decomposition chain, ADR-0013 settings round-trip incl. a real Gitea volume restore), the Clear-Runs stop hardening, and the implementation audit/fix rounds recorded below. Residual documentation findings were corrected in the final cut; this record does not claim that review can make a moving codebase permanently drift-free.

- **Positioning (PR #16):** "Your board is the interface" replaces the walked-back "You never operate it"; normative when-to-use / when-not §1b; a real operator contract (`docs/18`) incl. the first consolidated secret-rotation procedure; roadmap status vocabulary (built / live-verified / ⏳).
- **Reliability (PR #18):** ruff `BLE001` enforced — every blanket `except Exception` narrowed or contract-justified inline (`docs/15 §7`); the grok-auth classifier trips the DEV_AUTH breaker on revoked creds (exit 12) instead of burning three attempts.
- **Structure (ADR-0015, PRs #21–#27):** the orchestrator binding façade is gone — `MissionManager` is DI + advisory state + verbs, implementation is module functions taking `mgr`; `api/main.py` went 1,837 → ~780 lines (composition root + ≤4-statement route forwards + service modules, AST-guarded); the admin ConfigPage god component became a 69-line dispatcher + section components. All behavior-preserving; a guard test is the "do not resurrect the god module" ratchet.
- **Clear-Runs concurrency (PRs #28, #30–#32, #34):** the hard one. A stop-then-**drain** wipe that stops every dispatch flavor at the true chokepoints — `RunBootstrap.launch` holds a `dispatch_lock` (every dispatcher funnels through it; an AST tripwire fails CI if that ever stops being true), `RunManager._kill_inner` guards its final save atomically, and a process-local **wipe generation** no-ops any stale save at the store layer (restamped for adopted runs on reconcile). Found incomplete by audit **three times** (D3 fixed ordering not concurrency; #30's poll-lock missed the oauth/mapper/hello paths; #31's kill-guard left a phantom-record window) and closed each time at a deeper chokepoint. **Live-verified 2026-07-19:** clear-runs triggered while a Dev run was live → run stopped + drained, records wiped, **zero ghost runs after a forced poll, zero `AuthenticationError`, zero orphaned ACL users** — the ACL/SIGTERM race is closed end-to-end.

**What the audit taught, recorded so it isn't relearned:** the test suite stayed green through all three incomplete concurrency fixes — only adversarial multi-agent re-audit of each fix delta caught them. Fix concurrency at the chokepoint (launch / kill / save-generation), never per-call-site; prove a structure tripwire fires by planting a violation; run the suite in the Redis harness (`pytest_app.sh`), not bare pytest.

Final state: 663 unit tests + admin `check:ui` green; `ci_suite.sh` (pin gate, gitea battery 13/13, dispatch-hello smoke) green on the deployed stack; the live clear-under-load smoke passed. Trailers that landed with the initial cut or immediately after: **`profiles.mjs` in `check:ui`** (PR #36) and the missions-board 1280 layout fix (PR #37) — full suite **61** browser checks including profiles. Remaining non-gating trailer (⏳): a fresh-`/data` operator-drill re-run.

**Final-cut hygiene (2026-07-19):** compose mounts `./dagu/dags` **read-only** into Dagu (trusted launch code — `14` §5; live-verified: health + hello dispatch; non-fatal `.dag.index` write WARN). Documentation was reconciled with multi-connection runtime behavior (`00`, `06`), the RO DAG mount (`13`, `14`), the admin/config split, harness/model ownership, process-local maintenance locks, and the public-config secret-response regression. The release tag is placed on this final cut only after it lands, then treated as immutable.

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

## Post-v0.1 backlog

> **Status vocabulary** (applies below; the honesty rule: a feature is not
> *done* until the live box proves it): **built** = merged, full suite + CI
> green · **live-verified (date)** = exercised on the live stack, evidence
> noted · **⏳ live-pending** = built, live end-to-end still owed. Milestone
> checkboxes `[x]` above mark exit criteria verified at that milestone's close.

### Shipped post-v0.1

- **Harness fault classification + model-backend brake** (ADR-0018): truthful
  in-band failure surfaces when experimenting with non-default models/backends
  (exits 15/16, structured `error_class`/`attempt_counted`, workspace forensics,
  misplaced-`result.json` recovery, store-derived per-Dev-Type throttle with
  excusal caps). Scenario captures cover all three harnesses. Also: chunk-buffer
  admission fix, attempt-count injection fix, EXECUTE binding-rule clarification.
  **built** · unit suite green · ruff clean · bake app/admin/images.


- **Pipeline handoff + PMO zip opt-in + setup/activity polish** (2026-07-21, ADR-0017): optional `attach_merged_changeset_to_pmo` (default off) for configured repos; always-on RO mounts of **done** direct blockers’ work repos (`Run.blocker_work` + `{blocker_repos}`); Overview setup accepts healthy internal forge or “I’ll work with the internal forge”; activity `.zip` attachments extracted under `{stem}/`. **built** · unit suite green · bake app/admin/images.
- **MCP plugins — core ports** (2026-07-17, PR #6): `DevType.secret_env` +
  live `mcp_setup_commands` runspec wire, referenced-missing-secret dispatch
  gate, exit-14 `DEV_MCP_SETUP` reporting, `tutorials/03-mcp-plugins.md`;
  connectors live out-of-repo (vendor segregation). **built** · ⏳ live plugin
  round pending the founder-owned `LOGS_MCP_GIT_TOKEN` PAT.
- **Decomposition depth + fail-closed edge inheritance** (2026-07-18,
  ADR-0012, PR #10): depth-tagged markers, `max_decomposition_depth` +
  Traffic-control UI, strict inherited edges, scheduler family gate, lineage
  notes on canceled parents. **built** (493 tests at merge) ·
  **live-verified 2026-07-19** (sandbox chain DEV-128→DEV-130/131→DEV-132/133:
  inherited edges across two generations of canceled blockers — the dependent's
  gate named children, then grandchildren + a SKIP'd sibling as dead blocker,
  and cleared on completion; depth 1/2 markers; project containment held both
  generations; lineage notes; at-limit refusal → NEEDS-HUMAN hand-off at the
  default limit AND after a live Traffic-control flip to 1; unlimited flip
  accepted).
- **Missions board** (2026-07-18, PR #11, rflpazini): Hermes-style kanban with
  steering comments + stop-run; pre-merge review fixed priority-validation
  500, stop-of-finalizing 409, `create_mission` 502. **built** · deployed live
  2026-07-18 (UI suite green).
- **Config profiles + settings bundle** (2026-07-18, ADR-0013): ONE versioned bundle format over the four settings stores; named profile snapshots (A + B) with save/apply/rename/delete, apply = replace-the-world through the config choke points (409 while runs active, rollback-by-reapply, diff preview with rotation warnings), scrubbed-error hardening, settings audit events on `events.jsonl`, and the `#/config/profiles` admin section. **built** (530 tests at merge) · **live-verified 2026-07-19** (save → drift → apply round-trip restored the drifted setting; divergence flag + last-applied breadcrumb observed; audit events confirmed).
- **Settings export/import + setup-env + Gitea backup** (2026-07-18, ADR-0013 part 2): single-file export (source = current or a profile; sections A/B/C; scrypt+AESGCM encrypted by default, plaintext behind explicit acknowledgment; optional skill embedding; audited), stateless import that **lands as a profile** (apply remains the one world-swap path), section C as a generated ready-to-place `.env` download, `scripts/backup_gitea.sh`/`restore_gitea.sh` for full-fidelity internal-forge backups, and the Export…/Import… transfer UI. **built** (548 tests at merge) · **live-verified 2026-07-19** (encrypted export with zero plaintext token shapes → passphrase-gated preview → import landed as a profile → generated `.env`; `backup_gitea.sh`/`restore_gitea.sh` real round-trip with gitea stopped — repos and skill store intact after volume replacement). **`profiles.mjs` UI suite shipped** (PR #36, part of the 61-check `check:ui` battery).
- **Activity-feed fidelity + per-mission activity repos** (2026-07-18,
  ADR-0014, PR #15): last-message-inline + full-dump flip, `MISSION.md`
  faithful mirror, per-mission `activity-*` repos swept on Clear, quoting
  quarantine, `executed_trivially` removed. **live-verified 2026-07-18**
  (missions DEV-126/DEV-127 on the live sandbox).
- **Skills philosophy + prompt assembly (ADR-0016, 2026-07-20):** three-layer
  composition (identifying prompt + mission playbook + optional Required
  soft-force block); skills are domain-only, additive, consult-optional by
  default; `skills_required` + tri-state Dev Type chips; role Dev Types
  (`judgment` / `implementer` / `mapper`); builtin skill catalog overhaul;
  admin View. Normative ADR + `app/devcake/skills/README.md` + docs 02/03/08/11/14.
- **Missions "Poll now"** (2026-07-18, PR #14, rflpazini): INV-1-aligned poll
  CTA; "New mission" dropped from the board. **live-verified 2026-07-18**.
- **Clear-runs concurrency follow-up** (post independent review of #30–#32):
  wipe generation (`store_gen`) so in-flight finalize cannot resurrect runs or
  keep posting after clear; force-remove pass via Dagu when soft drain times
  out (then wipe; `ok:false` if still undrained — no host docker.sock);
  dispatch chokepoint AST tripwire; SelectionChips unavailable tooltip; docs
  honesty (dual locks for full wipe including OO; not “poll is the only
  dispatcher”). **built** · **live-verified 2026-07-19** (clear-under-load:
  stop+drain while a Dev was live — zero ghost runs, zero orphaned ACL users;
  see Clear-Runs concurrency note above). **Does not claim** multi-threaded
  store safety or host-level force-kill.

### Deferred (post-v0.2)

- **Webhook ingestion** — a PMO `watch()`/webhook `ChangeEvent` seam replacing polling (+ tunnel guide). Deliberately sequenced *after* F2: the multi-PMO port reshapes the exact surface the seam attaches to. Top candidate once v0.1 ships — multi-PMO instances multiply polling cost.
- **Additional PMO adapters** (GitHub Issues, GitLab Issues, Monday) + the **markdown-fidelity adapter refactor** (ISSUES #35). **Gitea Issues** (`gitea_issues`) shipped as the first forge-issue family member (local board on bundled or external Gitea; pure `PMOPort`, not `ForgePort`). GitHub/GitLab Issues should copy that profile.
- **N repos per mission** — cross-repo missions (one PR per repo, set-approval semantics, merge ordering) are a distributed-atomicity project of their own; v0.1 caps per-mission resolution at 0-or-1.
- **Per-run scoped forge tokens** & the rest of `14` §7 — natural companion to F4's per-mission repo tokens; revisit once that machinery exists.
- **Network egress allowlists / reduced sandbox-bypass** for non-EXECUTE stages (ISSUES #16).
- **Additional log-connector backends** (Loki; others on demand) — live in the standalone plugin repo <https://github.com/fidecastro/devcake-logs-mcp> behind its own `LogBackend` seam, zero core impact. The core MCP-plugin ports (`DevType.secret_env` + the previously-dead `mcp_setup_commands` runspec wire, dispatch gate, exit-14 reporting) shipped 2026-07-17; plugins install per Dev Type at run time (`08` §7, `tutorials/03-mcp-plugins.md`).
- **Priority-conditional Dev Type assignment** (e.g. Urgent missions route EXECUTE to Senior Dev — relaxes the strict 1 Mission Type → 1 Dev Type rule).
- Admin panel **OIDC/SSO** (v0 has basic auth).
- **First-class OTel metrics layer** — conditional: earns its keep when dashboards need pre-aggregation or long retention (v0 aggregates via SQL over span attributes — `12` §4).
- **SQLite `StatePort` swap** — conditional: if run history outgrows files.
- **Public-release hygiene** — conditional: if audience expands. LICENSE, SECURITY.md, CONTRIBUTING.md, CHANGELOG, SBOM (ISSUES #38).
- **Internal-forge orphan sweep** — admin tool reconciling Gitea org repos/svc users against `/data/secrets/internal_forge/mission-*.json` (svc users leaked by pre-v0.1.1 Clears are not garbage-collected; the leak itself is fixed).

### Discarded (2026-07-14, with rationale — do not resurrect without new evidence)

- **Scout Dev experiment** — not an engineering item: routing ONBOARD to a cheap-model Dev Type requires zero code changes and can be run from the admin panel any week as an ops experiment. *Ops note: assign ONBOARD to a cheap-model Dev Type, watch decomposition quality for a week, adopt or revert.*
- **Mid-run Dev→PMO write relay commands** — superseded by F4: the internal fallback repo gives Devs mid-run persistence with diff capture, without opening a live write channel to the PMO. Revisit only if humans need mid-run progress comments in the PMO feed itself.
- **Internal Mission worktree as an activity mirror** (the original F5 shape: per-Mission git mirror of the `activity/` folder structure, folder-scoped keys, "fewer PMO API calls") — superseded 2026-07-14 by F4's internal fallback forge. The mirror duplicated PMO state and bought a cache-coherence problem against the single-source-of-truth invariant; its real payoff (PR mechanics for non-code artifacts) survives intact in F4 without any mirroring. If diff capture of human activity-feed edits is ever wanted, it's a run-record snapshot diff, not a git server. **Partially un-discarded 2026-07-18 by ADR-0014 D4:** per-mission `activity-*` repos in `devcake-repos` are write-only *records* (app-pushed per step, deleted on Clear, never read back) — not the rejected read-back mirror; the internal forge made them cheap and the PMO stays the single source of truth.

*(Note: `auto_merge` was originally slated post-v0 but is a confirmed v0 requirement — it ships in M5/M6.)*
