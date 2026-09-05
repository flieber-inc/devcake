# Changelog

DevCake is pre-v1. Notable public changes, milestone exit criteria, and the
living engineering log live in **[`docs/16-roadmap.md`](docs/16-roadmap.md)** —
that file is the source of truth so history is not maintained twice.

This root `CHANGELOG.md` exists so FOSS and GitHub conventions have a stable
landing page. When maintainers cut numbered releases, release notes can be
added here without copying the full roadmap.

## Unreleased (pre-v1)

See the living log and open candidates in
[`docs/16-roadmap.md`](docs/16-roadmap.md).

Community surface added for public-repo hygiene (no LICENSE change in this
track): [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md).

- **Added — a backend activity bar.** A discreet, collapsible strip under
  every admin page shows what the app is doing right now — the poll cycle
  and segment, a mirror sync with its progress, a forge sweep, a dispatch,
  a finalize, a steward launch, a wait for tracker quota, a settings save
  waiting out the poll cycle — each with its duration, or how long the app
  has been idle and what ran last. A phase past twice its natural bound is
  marked overdue; a board whose poll segment was skipped for a tracker's
  quota reserve reads as waiting, not frozen. Fed by a new cheap
  `GET /api/v1/activity`; phases are registered by the same context managers
  that open the tracing spans, never inferred.

## v0.5.5 (2026-09-05)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.5).

- **Changed — a repository card's branch is blank by default and means
  the repository's own default; a wrong pin is loud** (#394; ADR-0024 addendum).
  The mirror asks the repository which branch its HEAD names before every
  sync and verifies the branch arrived before moving the mirror's HEAD, on
  pinned cards too: a pin the repository does not have fails that card's
  sync with both names, and missions on it defer until the card is fixed —
  previously the sync went green over a dangling HEAD and Devs received an
  empty clone. The Dev's environment and playbook and branch protection use the
  resolved branch; the claims writer clones a blank card's repository at
  its own HEAD. An empty repository with no branches yet bootstraps on
  `main` for a blank card (a pin keeps its name). Repositories gains a Branch field
  with a **Discover** button and a section action that fills every card
  from the repositories' HEADs (blank fields and missing pins; existing
  pins are kept). The provision step refuses an empty checkout of a
  non-empty repository. Branch-protection probes cover work repos only.
  **Upgrade note:** cards pinned to a branch their repository lacks stop
  dispatch on their board until corrected (Discover all + Save); nothing
  is migrated for you. A settings bundle exported after this change
  carries blank branches that an older app refuses to import.

## v0.5.4 (2026-09-04)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.4).
`devcake-cli` 0.1.2 ships with it (`devcake status` prints the request
budgets).

- **Added — a loud alarm when a tracker rejects requests, and the request
  number where an interested person looks** (#392). Every request budget
  keeps a rolling one-hour count of the tracker's own rejections; the health
  payload derives `pmo_rate_limited` from it and the admin shows a critical,
  non-dismissable alert that clears itself an hour after the last rejection
  (a self-throttle stays the existing dismissable warning). Each PMO card
  shows a one-line readout of the connection's measured requests per hour
  against the credential's limit, with remaining, refill time and rejections
  on hover, and `devcake status` prints the same rows on the host.
- **Changed — a resume is visible to the Dev, and environment findings are
  no longer routed** (#391). When a mission's most recent run was a
  hand-off, the next run's brief opens with a pointer and its activity
  mirror with a banner naming the ask, the fact that a person released the
  hold, and how many human comments that run never saw; the playbooks state
  that a release without a comment answers the ask. Devs are told that
  limitations of their own run are not discoveries, the steward declines
  such findings, and every delivered finding carries a fingerprint so the
  same finding is never delivered twice to a recipient.
- **Fixed — a cross-repository decomposition routed every child to the
  default repository** (#390). The triage playbook asked for a backticked
  routing marker in each child's description, and the decomposition step's
  marker-neutralizer (which stops Dev prose from smuggling live markers)
  stripped exactly that, so the children landed on the board's first
  repository and their first run latched it. A child's repository is now a
  `repo` field of the decomposition manifest, validated against the
  instance's work repositories, and the app stamps the routing footer
  itself; a read-only ONBOARD run no longer latches the repository choice
  (a marker or default edit after an unmarked triage now takes effect at
  the first post-triage step).
- **Changed — the poll spends less per cycle** (#389; ADR-0003 amendment,
  ADR-0033 addendum). The cycle's board fetch is a snapshot that the
  tracking sweep, the dispatch-time ancestor offer and scheduled-task
  in-flight checks reuse instead of re-reading the tracker; a tracking
  project's children are read live only when the snapshot shows completion
  is possible. Labeled feeds (discovery routing, merge driving) are
  re-scanned only when the mission changed, DevCake wrote to the feed,
  five minutes passed, or a write is about to be made on the strength of
  the scan. Linear's project-label registry is cached per
  adapter. `/health` gains `pmo_demand` (what each cycle read and what the
  memo saved).
- **Added — PMO request budget** (#388; ADR-0040). Every issue-tracker
  adapter's wire call now runs through one vendor-neutral governor that
  reads the tracker's quota headers (Linear requests + complexity per user,
  GitHub, GitLab; Gitea after a proxy rejection), keeps a reserve for
  write-back work, paces the poll's reads instead of hammering the vendor,
  waits and retries once for finalize/dispatch after a definitive
  rejection, and never dead-letters a finalize for a rate limit. `/health`
  gains `pmo_budget` and `pmo_budget_warnings` (an advisory naming the poll
  interval that fits), the admin a dismissable warning.
  `DEVCAKE_PMO_BUDGET_OFF=1` keeps it observe-only for a first hour on a
  host.

## v0.5.3 (2026-09-02)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.3).

- **Fixed — a discovery batch could be closed as "run record cleared"
  one minute after it was posted** (#386). The harvest posts the discovery
  marker before the close wrote the run's result onto its record, and the
  poll-cycle sweep read a record without a result as cleared, writing a
  permanent `to=-` receipt; a sweep landing in that few-second window
  silently lost the batch. The close now writes the result onto the
  record before the harvest posts, and the sweep treats an existing,
  non-terminal record as in flight (held, nothing posted), reserving the
  unroutable disposition for an absent or terminal record (docs/03).
- **Fixed — `devcake-repo:` markers written as URL slugs gated every
  child of a multi-repo decomposition** (#385). The triage prompt lists each
  repository as card name, workspace folder and URL on one line, and Devs
  (humans too) reach for the folder or URL slug — whose hyphens make the
  marker unparseable, so the children never dispatched. A marker that is
  not a card name but equals exactly one work repository's URL slug now
  resolves to that card (an exact secondary key on operator config, never
  the default fall-through the marker doctrine forbids); zero or several
  matches still gate, and the reason lists every card with its slug.
  Decomposition inherits the parent's marker through the same resolver,
  and the triage prompt says which value is the marker.
- **Fixed — plan approval was invisible to the Dev and opaque to the
  human** (#384). On a board with Plan approval on, a careful triage returned
  `human_needed` to ask for approval and the person received a hand-off
  wall of text with no plan attached. The ONBOARD, PLAN and EXECUTE
  playbooks now carry `{plan_approval_rule}`, rendered while the board
  gates plans: triage learns that attaching a plan parks the ticket by
  itself and that `human_needed` is never the way to ask, planning leads
  with a reviewable summary, execution learns its plan was approved by a
  person. A custom planning-stage template without the placeholder is
  flagged on `/health` and at save time. The approval request itself now
  leads with what is being approved — the plan file and its outline — and
  then the two actions (docs/03 §2a).

## v0.5.2 (2026-09-02)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.2).

- **New — per-board plan approval** (#380). A PMO card toggle (**Plan
  approval**, `pmos[].plan_approval`, default off) that makes every fresh
  plan — from a PLAN run or attached at ONBOARD triage — park its mission
  under `DEVCAKE-NEEDS-HUMAN` next to `DEVCAKE-EXECUTE`, and every
  decomposition create its children already parked under
  `DEVCAKE-NEEDS-HUMAN`, so a person reads the plan (or the split) before
  any code is written. Approve by removing the label (or **Resume** on the
  Missions page), one ticket at a time; to change a plan first, add
  guidance as a comment and move the ticket back to `DEVCAKE-PLAN`; to
  change a split, edit or cancel the children in place. Reuses the
  hand-off label and recovery path; not counted as a hand-off (docs/03
  §2a).
- **Fixed — `/health` 500 (SPA "Backend unreachable") and aborted poll
  sweeps once an internal mission repo is registered** (#378). Internal
  (zero-repo) repos are synthesized with hyphenated names the operator-card
  pattern forbids; their token read-throughs went to the secrets store,
  whose name
  check raised — outside the branch-protection probe's try on `/health`,
  and outside `refresh_health`, where the cycle guard then dropped whole
  poll cycles whenever a breaker was latched. Such rows now carry a
  runtime-only `RepoInstance.internal` flag (they store no connection
  secrets, so the read-throughs answer `""`), the branch-protection walk
  skips them (an unactionable advisory) and maps any per-repo failure to
  `None` (docs/15 §7 probe contract). A breaker latched on an internal repo
  by a Dev-side `DEV_FORGE_AUTH` is no longer keyed on a row credential
  field (there is none): it re-probes on the registered service-token
  adapter and clears on ok instead of sticking until restart.
- **Fixed — `devcake up` could leave the host baker dead on the degraded
  (flock respawn) path** (#379). The respawn loop's lock fd was inherited
  by its children, so a stopped supervisor's orphans (the backoff `sleep`,
  the baker) kept the lock; the install slept a fixed 0.3 s and the
  successor
  gave up on the busy lock at once ("respawn supervisor died at launch").
  The handoff is now ordered and waited — supervisor first, then baker,
  each waited for with SIGKILL escalation (`DEVCAKE_BAKER_EXIT_WAIT`) — the
  loop closes its lock fd for every child, and a successor waits up to
  `DEVCAKE_RESPAWN_LOCK_WAIT` seconds for a predecessor still releasing it.
- **devcake-cli 0.1.1.** The CLI wheel carries the `devcake up` handoff
  fix above (the launcher now stops a degraded respawn supervisor first);
  PyPI installs upgrade with `uv tool upgrade devcake-cli`, checkout
  snapshot installs with `uv tool install .` after pulling (lockstep with
  the repo's `scripts/lib`), and an editable venv install
  (`uv pip install -e .`) already tracks the tree.

## v0.5.1 (2026-08-31)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.1).

- **Fixed — skill-source mirror lifecycle** (#373). A skill source with an
  empty Branch field failed every sync (`symbolic-ref` refuses the empty
  ref) despite the card promising "empty = the repository's default": the
  sync now resolves the remote's HEAD symref (anchored on the exact HEAD
  target line, verified to exist post-fetch; probe errors keep their own
  stderr so auth failures latch the breaker). Skill-source removals AND
  renames now handle the mirror like repo cards, a rename target is never
  deleted by a same-Save removal, and `default_branch` normalizes at the
  model — a repo card refuses an empty branch outright (its value feeds
  the container env and merge prompts), so empty-means-default stays a
  skill-source-only contract.
- **New — repo-backed skill sources** (#374,
  [ADR-0039](docs/adr/0039-repo-backed-skill-sources.md)). A skill source
  may declare `backed_by: <repo card>` instead of a URL: no mirror, no
  sync, no token of its own — reads serve from the backing card's mirror,
  freshness rides that card's sync in the one dispatch gate (shared by the
  steward gate), and the connection probe delegates to that card while
  honoring the source's own branch pin. Sharing is declared config data,
  never runtime URL inference; the backing card must be configured, is
  refused deletion while cited, and follows renames.
- **New — token copy between connections** (#375). "Copy tokens between
  connections…" behind the ⋯ menu on the Repositories and PMO pages: one
  card's stored tokens land on selected siblings slot for slot (write /
  read-only / reviewer), families are same forge **and same host**, a
  repo's write token can seed that host's `*_issues` board key, and the
  target list renders from a server `dry_run` — values never ride a
  request or response, and one `secrets_copied` audit event records names
  only.
- **New — fetch external skills from the catalog** (#376). The Fleet →
  Skills ⋯ menu gains "Fetch skills from external sources", sharing the
  refresh chokepoint with Skill sources' "Update now" — per-source failure
  reasons, never a green ✓ over a failed fetch.

## v0.5.0 "Java Lava" (2026-08-31)

First release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.0).

- **New — the `devcake` host CLI** (#360, #363–#365, #369, #370). One
  installable, agent-operable command replaces the bring-up script:
  `devcake up / down / status / doctor / setup / baker run`, with sealed
  exit codes, `--json` receipts on every verb, `.env` bootstrap secrets
  auto-generated by default, non-interactive first-setup (Dev roster, PMO
  and repo wiring with secrets via env/file/stdin, settings-bundle import),
  and a preflight doctor that prints one-time remedies and never runs sudo.
  `up.sh` is removed — the CLI is the only bring-up path
  ([ADR-0038](docs/adr/0038-devcake-cli-scope-command-surface-and-agent-operability.md)).
  A new operator skill at
  [`.claude/skills/devcake-ops/SKILL.md`](.claude/skills/devcake-ops/SKILL.md)
  teaches any capable agent to install and manage a deployment; the README
  quickstart leads with it. `devcake-cli` publishes to PyPI via trusted
  publishing on `cli-v*` tags.
- **New — branch protection as a product surface** (#362, #366, #368).
  Playbooks carry a code-owned hard rule that Devs never merge, approve, or
  push to the default branch, pinned by tests; branch protection is a
  documented deployment requirement; and the app can now **apply** a derived
  protection baseline to any work repo — GitHub, GitLab, and Gitea — from
  the Repositories page (per-repo or bulk, confirm-gated, audited). The
  shape comes from the target repo itself: its own discovered CI checks,
  approvals only when a distinct reviewer token is stored, and existing
  stricter rules are never weakened.
- **New — admin UI round** (#331–#346, #349–#359, #367). Dark theme aligned
  with the brand; navigation regrouped into Connections / Fleet / Settings
  with per-page save semantics; the Prompts page rebuilt (sections instead
  of a workflow switcher, slim per-type rows, a template manager with
  rendered and editable source, duplicate-to-edit for built-ins); a
  first-setup wizard staffing Executor / Judge / Steward on an empty
  roster; skills store ↔ skill sources cross-links; adapter management
  matured (longer names, rename everywhere including the managed board,
  complete deletion, dev cloning, pagination fixes); per-PMO mission-type
  prompt overrides; honest interim token/cost placeholders on the Runs page
  (merged into one centered cell on running rows) plus a TEAM hover detail;
  and the cost rate card ships empty with pointers to vendor price pages.
- **Fixed — host baker hardening** (#347–#348, #352–#353). The baker runs
  under a real supervisor on both platforms — systemd user unit on Linux,
  launchd on macOS, a loud flock-respawn fallback elsewhere — with a
  single-instance lock, rotation-safe liveness verification, and
  displacement of non-cooperating leftover bakers at install.
- **Docs** (#358, #369). Documentation-drift sweep across the runbook and
  tutorials; the README quickstart rewritten around the CLI and the
  ask-your-agent setup path.

## v0.4.3 (2026-08-25)

Patch release in the v0.4 "Hummingbird" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.4.3).

- **Fixed — fresh installs on macOS / Docker Desktop** (#313–#319).
  `DOCKER_GID` is derived from the in-container view of the Docker socket;
  the host baker no longer sweeps a keep-set published mid-reconcile,
  verifies its own liveness after launch, and gains `--foreground-baker`;
  `./up.sh --bake` proves dispatch with a hello smoke before reporting
  success; OpenObserve's password policy is validated up front; baker
  launch failures ship diagnostics to OpenObserve and the admin alerts.
  Docker Desktop guidance lives in [`docs/13-deployment.md`](docs/13-deployment.md) §8b.
- **Security — GitHub Security tab at zero** (#320–#329). The CodeQL
  path-injection class is closed by a shared path-confinement helper
  applied across the operator-facing stores and dispatch, with dispatch
  reading credential files through the secrets-store port; CodeQL runs as
  advanced setup from a SHA-pinned in-repo workflow with a model pack for
  the redaction chokepoint; the six remaining alerts are documented false
  positives with a proof table in [`docs/14-security.md`](docs/14-security.md) §12,
  dismissed citing the packet. Dependabot's two alerts closed via the
  transitive `postcss` bump, and automated security fixes are enabled.

## v0.4.2 (2026-08-20)

Patch release in the v0.4 "Hummingbird" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.4.2).

- **Fixed — the baker starts on hosts without pydantic** (#310). The host
  baker runs on the operator's bare system python, and its import chain
  reached pydantic through the harness registry, so fresh macOS and minimal
  Debian installs crashed it at startup. The dependency is cut and a
  regression test now blocks any third-party import from re-entering the
  baker's host-side closure. Affected hosts need no cleanup: pull and re-run
  `./up.sh`.
- **New — nested-engine rig receipts** (#311).
  `scripts/harness_probe/nested_probe.sh` replays the dev-run pipeline's
  exact rootless-podman runtime contract against a locally baked harness
  image and writes a per-rig receipt naming each step's verdict; see
  [`docs/13-deployment.md`](docs/13-deployment.md).

## v0.4.1 (2026-08-20)

Quickstart instructions comment cleanup only — no functional change. Note
this tag predates the v0.4.2 baker fix.

## v0.4 — Hummingbird (2026-08-19)

DevCake's first public release. Full notes on the
[GitHub release](https://github.com/flieber-inc/devcake/releases/tag/v0.4);
history and receipts in [`docs/16-roadmap.md`](docs/16-roadmap.md).
