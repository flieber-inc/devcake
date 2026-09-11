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

- **Added — compose inside Dev containers.** The harness images ship a
  pinned podman-compose as the nested engine's compose provider, reached
  through `docker compose` and a `docker-compose` symlink alike, with the
  iptables binary netavark needs for compose-created networks and the
  provider banner silenced; the nested-engine probe records a compose step
  in its receipt, shown on the Dev Types panel, `devcake status`, an
  Overview warning and the Dev's prompt when it is red. The Dev's prompt
  section names the compose gaps (health-check waits, daemon socket, host
  name). Before this the images carried no compose at all.
- **Fixed — the nested engine inside Dev containers works on hosts that
  run AppArmor** (ADR-0023 addendum). Under Docker's default profile the
  rootless engine could not mount, and Ubuntu confined it further the
  moment it created a user namespace; nested `docker`/`podman` had never
  worked on such hosts, only on the rigs without AppArmor. The checkout
  now ships a Dev-container profile (`scripts/apparmor/devcake-nested`,
  compiled in CI) that the operator loads once (`devcake doctor` prints
  the two commands, never runs them); `devcake up` derives the profile
  name into `.env` — asking the daemon first, so a profile the host cannot
  apply is never named (a virgin host with no image yet falls back to the
  installed file compiled by the host's own parser) — the run DAG names it on both Dev steps, allows
  the one extra syscall the runtime needs, and launches with Docker's
  masked system paths removed. **Security posture, stated plainly:** on
  Ubuntu hosts the profile lifts the default restriction on unprivileged
  user namespaces for Dev containers (a Dev may hold capabilities inside
  namespaces it creates, which the rootless engine needs), and on every
  host a Dev can now read a few host-information files Docker normally
  masks; the profile re-denies them on AppArmor hosts, elsewhere they stay
  readable. docs/14 §6 has the full list. The host baker
  now runs the nested-engine probe after every harness bake and
  publishes the newest receipt: the Overview warns, the Dev Types panel
  and `devcake status` name the first red step, and a Dev on a red host
  is told in its prompt that containers are unavailable — runs still
  launch. `devcake up` archives the Dagu state volume under
  `.factory/backups/` (0600, named with the version it came from, newest
  three kept) when the checkout pins a Dagu the volume was not last used
  with — stack running or stopped; dagu is stopped for the copy and
  started again at once — a backup for rollback, not a migration; the
  command is printed when it cannot be taken. The baker re-measures the
  nested-engine receipt after a kernel or engine upgrade and asks the
  daemon every minute whether it still applies the named profile; when it
  no longer does, every surface says that no Dev container can start until
  the profile is loaded again or `devcake up` is run.
- **Changed — `devcake-cli` 0.1.9.** The host CLI carries the AppArmor
  derivation, the doctor check and the Dagu archive; a release checkout
  refuses `devcake up --release` under an older installed CLI, so upgrade
  the CLI first (`uv tool install --reinstall '.[mcp]'` from the checkout,
  or the PyPI release of the same version).
  Every Dev prompt now carries a short section saying what `docker` is
  inside the container.
- **Changed — Dagu 2.13.0 → 2.16.3.** The release that carries our
  upstream fix for container limits: the run DAG now writes them in
  Docker's own flat form instead of the nested workaround the old decoder
  needed. Also in the range: a step's containers are stopped on timeout,
  the docker group is created by the stock entrypoint, and the Dagu state
  layout was refactored — `devcake up` archives the Dagu volume before the
  re-pinned Dagu first starts (the "Fixed" bullet above).

## v0.6.4 (2026-09-10)

Patch release in the v0.6 "Kentucky Butter" line. `devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.6.4).

- **Fixed — routed discoveries reach the mission they were sent to, in
  full** (#462, ADR-0042 addendum). A leads delivery carried excerpts, and a long one carried
  nothing but a pointer to a file on the source mission that the
  recipient's Dev never receives. The delivery's record now carries every
  finding in full, with no inline ceiling; the head quotes an excerpt of
  each. The recipient's Dev reads them from its own activity folder.

- **Added — one place to read a mission's discoveries** (#462). The status
  comment's fold gains a Discoveries section: what the mission reported,
  with the record-file link, and every lead it received, in full. It is a
  view over the feed with no marker, no file token and no provenance line,
  so nothing that scans the feed counts it, the freshness check ignores
  it, and no Dev sees it. The status refresh now reads the feed once and
  the record push at the same boundary reuses that read.

## v0.6.3 (2026-09-10)

Patch release in the v0.6 "Kentucky Butter" line. `devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.6.3).

- **Added — a mission that cannot start is now visible** (#460, ADR-0042
  addendum). A mission whose
  dispatch DevCake refuses or defers for a reason a person can act on,
  for longer than a threshold, is listed on the health endpoint, raised as
  an alert on the admin Overview with its age, and badged on its board
  card. The ticket gets one notice per distinct block and its status
  comment is created early with a "waiting to start since" line that says
  why and what would clear it. Waiting on a blocker never counts, the same
  block is never repeated, a different block notifies again, and neither
  the notice nor the status comment ever enters a Dev's context.

## v0.6.2 (2026-09-10)

Patch release in the v0.6 "Kentucky Butter" line — the activity repository
becomes the record of a mission's run (ADR-0043): every Dev now receives
the whole record of every mission upstream of its work, blockers included.
`devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.6.2).

- **Changed — a mission's activity repository is its record, kept current**
  (#454).
  The per-mission activity repository used to be written once, before a
  step launched. It is now refreshed at every run boundary: when a step
  closes, including failures, at completion, at a hand-off, when a pull
  request is closed unmerged, and when a conflict is routed back. The
  upstream folders a Dev cloned at dispatch stay in place. Two read
  operations on the internal forge let the record be read back (ADR-0043,
  stage 1 of 3).

- **Changed — a project's own run now leaves a record** (#457). DevCake used to
  suppress every write to a project, so the step that split a project,
  its transcript and its reasoning went to the observability stream
  only, and every child worked under a root whose folder held the brief
  and an empty feed. A project run's step card, its notices and its
  decomposition note now post as project updates, the transcript
  uploaded and linked, through the same chokepoint issues use (ADR-0043,
  stage 2 of 3).

- **Changed — a Dev's upstream folders are copies of the record, and
  finished blockers are among them** (#456). The upstream folders in a Dev's
  workspace were rebuilt from the vendor at every dispatch and covered
  the decomposition ancestors and the containing project only; a mission
  this one was blocked by contributed a repository clone and a short
  handoff excerpt, so an attachment on a blocker's ticket never reached
  the dependent. Each upstream folder is now a copy of that mission's
  activity repository, and every direct blocker that finished has one.
  The byte budget is decided from the record's sizes before anything is
  fetched; a mission that never ran is rebuilt from the vendor as before;
  an unreadable blocker is disclosed, never a reason to hold a dispatch
  (ADR-0043, stage 3 of 3).

## v0.6.1 (2026-09-10)

Patch release in the v0.6 "Kentucky Butter" line. `devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.6.1).

- **Fixed — an issue placed in a project now sees the project's activity**
  (#452, ADR-0036 addendum). A Dev working an issue a person added to a project received nothing
  from the project: the upstream offer followed only the marker DevCake
  writes when it splits a mission, so the project's brief, its updates and
  its documents were invisible. The offer now ends at the project the
  issue belongs to, mirrored under the upstream folder like a
  decomposition parent, with the folder's relation and the project's
  title named in the activity banner. A project the board does not poll
  is read once; one that cannot be read is a named gap, never a silent
  omission.

Community surface added for public-repo hygiene (no LICENSE change in this
track): [`CONTRIBUTING.md`](CONTRIBUTING.md), [`SECURITY.md`](SECURITY.md).

## v0.6.0 (2026-09-09)

Minor release — "Kentucky Butter" opens the v0.6 line with the feed rebuilt
for the person reading it (ADR-0042). `devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.6.0).

- **Added — one status comment per mission, kept current** (#449). The first
  thing a reader sees: what is happening now, the pull request, the cost
  so far, and the step ladder with outcomes, durations and costs. It is
  created by whichever dispatch finds the feed without one, so a
  scheduled task starting at EXECUTE or a mission a person ordered into
  REVIEW gets one too, and it is edited in place at every step end, park,
  hand-off, merge event and completion. A view, never an ask: it points
  at the notice when a person is needed, counts for nothing, and the Dev's
  activity folder omits it.

- **Changed — one step card per step on the mission feed** (#448, on the port operation and primitives of #445, #446, #447). A step used
  to post its transcript, its answer (under an HTML marker one vendor
  renders as text), its token report and its discoveries as separate
  comments, the answer twice. Each step now posts one comment: a header
  line (outcome, step, duration, cost), the Dev's answer quoted and cut
  at a sentence boundary, what the result is and who acts next, the
  transcript, and one collapsed fold holding the record — token report,
  discoveries, run id and every marker the orchestrator scans. The Dev's
  activity folder is unfolded from that record and stays byte-identical;
  a golden test holds the pre-card feed and the card feed to the same
  folder. Adapters gained an edit-own-comment operation and a fold-syntax
  capability along the way.
- **Changed — messages to a person open with a fixed lead; receipts stop
  being comments** (#450). Every comment DevCake writes to a person — a hand-off,
  an awaiting-merge note, a give-up, a loop or freshness warning, a
  completion, a decomposition note, a re-review or conflict-resolve
  directive, a delivery of leads — now opens with `✋ Needs you.`,
  `⚠️ For the record.`, `ℹ️`, a directive lead or `📨 Leads from …`, says
  what happened in one sentence and what to do, and keeps the comment text
  of the previous build, markers included, in its collapsed fold as the
  record; the Dev's activity folder is unfolded from that record and stays
  byte-identical. Discovery routing receipts and the deliverable-archive
  note no longer post comments of their own: they are appended to the fold
  of the step card (or the completion notice) they belong to, and fall
  back to the old comment shape only when that edit is impossible. The
  HTML deliverable marker is retired for the `devcake:deliverable:v1`
  token.

## v0.5.17 (2026-09-09)

Patch release in the v0.5 "Java Lava" line. `devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.17).

- **Fixed — a mission gated on hundreds of finished siblings launches
  again** (#441). The prompt lists every finished blocker's closing note and
  every reference repository; on a board where one mission waited on a
  few hundred siblings, those two sections alone put a quarter-megabyte
  on the harness command line, the launch was refused three times as
  "prompt too large", and the mission was marked failed. Both sections
  are now built to a byte budget: the head of each list rides in the
  prompt, the rest is counted in one closing line that points at the
  workspace, which always holds every handoff (MISSION.md) and every
  clone. A prompt still past the budget is logged at dispatch with the
  size of each part, so the next such case is a log line, not a dig.

- **Fixed — a launch no longer costs a vendor's hourly budget when the
  mission has hundreds of blockers** (#442). Before launch, DevCake re-reads
  every blocker live. That read went one edge at a time, asked the
  peer boards before the mission's own, and ran twice per attempt, so a
  mission gated on a few hundred finished siblings spent about 2,700
  Linear requests per attempt, Linear locked all boards on that key for
  an hour, and the loop repeated when the lock lifted. The blocker set
  is now read in one batched walk (one query per hundred ids on Linear),
  the mission's own board is asked first, and the two pre-launch passes
  share the answers. The same mission now costs four requests.

## v0.5.16 (2026-09-08)

Patch release in the v0.5 "Java Lava" line. `devcake-cli` stays at 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.16).

- **Fixed — a receipt the app never received is pushed again.** The
  host baker writes a receipt locally, then into the app container;
  when that write landed while compose was recreating the app (a
  cached rebuild now finishes inside that window), the bake was marked
  failed and never retried, and the pin stayed "no receipt" although
  the image and its probe were fine. The baker now re-pushes any local
  receipt for the current digest the container lacks on every tick,
  and a failed container write no longer fails the bake.

## v0.5.15 (2026-09-08)

Patch release in the v0.5 "Java Lava" line. Ships with `devcake-cli` 0.1.8.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.15).

- **Fixed — one shared HTTP pool; the app no longer runs at its
  descriptor ceiling.** One connection pool per repository card held
  hundreds of idle keep-alive sockets on a large host, and every boot
  fan-out (the forge probe of every card, the mirror warm-up) then
  failed for minutes with "too many open files": git spawns refused,
  secret files read as corrupt, mirrors deleted and re-cloned, the
  health probe resetting behind the proxy. Every adapter now shares one
  bounded pool per timeout, closed once at shutdown; the compose `app`
  service raises its open-files limit; a mirror whose origin differs
  only in the authority user (a credential that failed to load, or was
  rotated) gets its remote URL updated instead of being rebuilt; and the
  secrets reader logs the actual reason a file was unreadable.
- **Fixed — "Prune unused Dev images" reports its outcome.** The host
  baker rebuilds its status every tick, so a prune's result was visible
  for one tick and the panel, polling every ten seconds, almost never
  showed it — the images were removed, the button looked dead. The last
  prune's outcome now rides every status with its time; the modal shows
  it, says when the baker cannot act (not running, or the checkout moved
  since the app was baked) and that the request waits in the inbox;
  `devcake status` prints the last prune; the baker logs each prune.
  Ships as `devcake-cli` 0.1.8.

## v0.5.14 (2026-09-08)

Patch release in the v0.5 "Java Lava" line. Ships with `devcake-cli` 0.1.7.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.14).

- **Changed — `devcake up` never bakes a Dev image.** `--bake` accepts the
  control plane only (app, admin, hello; app-test for CI) and refuses a
  harness target or `all` with the remedy; `--release` implies the
  control-plane bake instead of the full matrix, which had a host
  rebuilding six house-pin harness images it never dispatches on. Dev
  images are the host baker's alone, and a tag move is a bake order it
  honours itself. Ships as `devcake-cli` 0.1.7.

## v0.5.13 (2026-09-08)

Patch release in the v0.5 "Java Lava" line. `devcake-cli` stays at 0.1.6.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.13).

- **Fixed — the discovery steward no longer dies on a large backlog.**
  Its prompt rides the harness command line as one argument, and the
  package's findings section grew with the pending set until it crossed
  the kernel's per-argument ceiling; every run then died at launch and
  was recorded as a dead run. The package is now built to a prompt
  budget (open members first, then findings source by source, then
  finished members as room allows, with a line naming what was left
  out), the run carries only the sources it serves and the rest stay
  pending for the next run, and the entrypoint refuses a prompt past the
  ceiling before launch as its own class (exit 17,
  `DEV_PROMPT_TOO_LARGE`) with the byte counts.

## v0.5.12 (2026-09-08)

Patch release in the v0.5 "Java Lava" line. Ships with `devcake-cli` 0.1.6.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.12).

- **Added — `devcake up --release [TAG]` and `devcake prune`.** A host
  re-pin is one command: fetch tags, check the newest release (or the
  one named) out, bake all, bring the stack up in the safe order, and
  remove stale control-plane images and dangling leftovers. It refuses
  before touching anything when the tree has modified tracked files,
  when the CLI runs from inside the checkout, or when the release ships
  a newer CLI than the one running. `devcake prune` does the tidy-up on
  demand; `--devs` asks the app for the host baker's Dev-image prune,
  the admin button's chokepoint — the CLI never removes a Dev image
  itself. Ships as `devcake-cli` 0.1.6.

## v0.5.11 (2026-09-08)

Patch release in the v0.5 "Java Lava" line. Ships with `devcake-cli` 0.1.5.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.11).

- **Fixed — the discovery drain no longer starves itself.** Before
  dispatching a steward run, the drain re-read every pending source of
  the family live, bypassing the feed memo. On a large family that was
  about a hundred tracker reads per cycle; the shared credential hit the
  request budget's critical floor part-way, the next read was refused,
  the drain deferred and restarted the same family a cycle later — two
  thousand requests an hour and no dispatch. The drain now decides on
  the same memoized, witnessed scan the sweep made this cycle, and a
  refused read resumes next cycle instead of restarting.
- **Fixed — a release re-pin no longer leaves every Dev Type waiting for
  a receipt.** `devcake up` replaces the host baker before the app is
  recreated, so the incoming baker is the one that claims the bake
  order the app publishes at boot; the outgoing baker used to claim it
  and the incoming one then dropped the previous tag's receipts with
  nothing to rebuild, freezing dispatch until a Dev Type was re-saved.
  The baker also treats a tag move as a bake order: a dropped receipt
  whose pin still has an image under another tag is rebaked under the
  new one. `devcake status` prints each harness template's staffing and
  names the remedy when pins wait with an idle baker. Ships as
  `devcake-cli` 0.1.5.


## v0.5.10 (2026-09-07)

Patch release in the v0.5 "Java Lava" line. `devcake-cli` stays at 0.1.4.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.10).

- **Changed — the release pin lives in the checkout, not in `.env`.** A
  `VERSION` file at the repo root names the release tag `devcake up`
  bakes and runs images under; cutting a release bumps it together with
  the changelog, and CI refuses a drift between the two. `devcake up`
  resolves the tag as process env (development builds) > `VERSION` >
  `latest`, writes it into `.env` for plain `docker compose`, and no
  longer reads `.env` for it — a value set there by hand is rewritten and
  reported, never obeyed. Deploying a release is `git checkout vX.Y.Z &&
  devcake up --bake all`. Ships as `devcake-cli` 0.1.4.
- **Changed — four hardenings after the demand campaign.** `devcake status`
  and `devcake doctor` report a drift between the checkout's `VERSION` and
  the tag the stack was brought up under, with the remedy; a `Release pin`
  workflow refuses a `v*` tag whose name differs from `VERSION`. `/health`
  gains `discovery_drain` per instance and `discovery_drain_warnings`, an
  advisory (and a dismissable Overview warning) when missions hold discovery
  leads and no steward run has routed them for six hours while routing is
  on and intake is not paused — the silent stall a back-off cannot catch.
  The deferred-merge window now hands off after the second admitted probe
  in a row past the window that does not merge, so one transient forge
  error is never terminal. The bounded wait every critical boundary call
  takes is one port constant instead of a literal repeated in five places.
- **Changed — one thread per step on the board.** Where the tracker
  threads comments (Linear), each step's token report and discovery
  harvest are posted as replies to that step's transcript comment
  instead of as further top-level entries, so a mission's feed reads
  one entry per step with its bookkeeping folded under it. The feed
  material is unchanged: same bodies, markers and order, the answer and
  every hand-off stay top level, scans read the same entries, and the
  Dev's activity mirror is byte-identical to a flat tracker's. Trackers
  without threads (the forge-issue adapters) post exactly as before.
- **Fixed — audit rows never carry a raw actor label.** Both audit
  writers pass the request's actor through the redactor before the
  row is written, so a misused header cannot park a secret shape in
  the audit file. Belt-and-braces on top of the boundary allowlist.

## v0.5.9 (2026-09-07)

Patch release in the v0.5 "Java Lava" line. `devcake-cli` stays at 0.1.3.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.9).

- **Changed — fewer routine tracker reads on large boards, and two stalls
  removed** (#417). The feed-scan memo's safety rescan is now a PMO capability
  (`updated_at_tracks_comments`): on a tracker whose item `updated_at`
  moves with every comment (Linear), a labelled mission's feed is re-read
  only when the mission changed or DevCake wrote to it, so the discovery
  and merge sweeps no longer re-read every labelled feed every few
  minutes; a settings Save keeps the memo unless the PMO card itself
  changed. The deferred-merge window always makes one more admitted probe
  after it elapses (and the merge itself when the forge says ready) and
  hands off only if that did not merge, so cycles whose reads the request
  budget refused can no longer spend the window with zero attempts; the sweeps' write-backs (a merged
  completion, a closed-PR cancellation, a conflict route, a hand-off, a
  tracking completion) now run as critical-class calls, so the budget's
  reserve covers a mission's outcome while the poll's own reads are being
  refused. Three dead steward runs in a row on an instance pause that
  instance's periodic service and discovery drain for three steward
  intervals and then admit one run (a success clears it, a failure re-arms
  it) instead of stopping until an operator clicks Run now — and one
  board's deaths no longer pause another board's steward. Boards that had
  raised the poll interval for quota can lower it again (`13-deployment.md`
  upgrade notes).
- **Changed — labelled feeds are re-read only when they changed** (#418). On
  Linear and Gitea Issues, one team-wide feed-changes read per poll cycle
  (ids and times, never text) tells the orchestrator which memoized feed
  scans are still good, so a mission whose tracker record moved for a
  label, status, or relation edit no longer costs a feed read; only a feed
  with a newer entry is re-read, and once. Adds `feed_changes_since` and the
  `feed_delta` capability to the PMO port; GitHub and GitLab Issues keep
  today's per-mission reads. Two new `pmo_demand` counters
  (`feed_delta_reads`, `feed_scan_memo_kept`) show the witness at work.
- **Changed — the merge sweep remembers each parked mission's pull
  request** (#419). The branch-to-pull-request lookup is a stable fact once the
  pull request exists, so after the first cycle a parked mission costs the
  forge one state read per cycle instead of two. A number the forge no
  longer knows is looked up again; a merged or closed answer on a
  remembered number is confirmed by a live lookup before the mission is
  completed or cancelled, so a newer pull request on the same branch wins
  exactly as before.

## v0.5.8 (2026-09-06)

Patch release in the v0.5 "Java Lava" line. Ships with `devcake-cli` 0.1.3.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.8).

- **Added — `devcake mcp`, an operator MCP server whose tools are the API
  itself** (#413, #415; ADR-0041 in #412). A coding agent beside the operator can now read and act on a
  deployment through the Model Context Protocol: `devcake mcp` speaks MCP
  over stdio and turns every admin-API operation into a tool, derived at
  startup from the app's own API description (published under the
  authenticated prefix at `/api/v1/openapi.json`), so a new route is a
  new tool with no second coding. `--read-only` keeps GET operations;
  secret-bearing and wipe routes opt out at their definition; mutations
  carry the intent header and an actor label the audit rows record. The
  SDK is an optional extra (`devcake-cli[mcp]`); the base CLI stays
  dependency-free. Every API route now carries a one-line docstring, which
  is its tool description. The README and the operator skill tell an
  operator how to hand the tools to their agent. Ships as `devcake-cli`
  0.1.3.
- **Changed — the README leads with agent-assisted setup and operations,
  and the operator skill is agent-neutral** (#411). The README opens with a
  prompt for the operator's coding agent and explains fresh context per
  run, work, reference and memory repositories, skills, staffing and
  scheduled work in a third of its former length. The operator skill
  moved to `skills/devcake-ops/SKILL.md` (the old `.claude/skills` path
  links to it), teaches the product model and an evidence-based
  diagnosis loop, and names the activity endpoint for "frozen or
  waiting".

## v0.5.7 (2026-09-06)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.7).

- **Fixed — Run now on a scheduled task says why it created nothing** (#409).
  While intake is paused (globally, or on every board the task would
  fire on) Run now answers 409 "intake is paused …", and a task whose
  last ticket is still open answers 409 with that reason, instead of a
  quiet empty result the operator had to guess at. The pause stays
  absolute: Run now is not a back door around it.
- **Fixed — grok runs report their cache writes and the CLI's own cost** (#403).
  The grok token report was written against a CLI that emitted neither;
  newer CLIs report a cache-creation count under `usage` and a per-model
  `costUSD` under `modelUsage`, and the mapper ignored both, so the Runs
  page showed cache writes as not extracted and every grok run as an
  estimate only. Both are now carried when present: a reported zero of
  cache writes stays zero (the vendor's number), a missing cost stays
  absent, never zero.
- **Fixed — a review finishing on a ticket a human stopped no longer
  overrides the stop** (#400). A REVIEW run that completes on a ticket
  that was cancelled, or tagged with the skip label, while it ran treats
  that as an external stop even when the review label is still on the
  ticket: the report posts, nothing is merged and no status or label is
  written. A merge the run itself recorded is no longer reported as an
  out-of-pipeline merge on replay; the tripwire still fires whenever
  that receipt is missing.
- **Fixed — a decomposition survives a restart between the board write
  and its local checkpoint** (#401). Children already created on the
  board count as a committed split even when the depth limit was lowered
  before the restart, with the manifest, part and title checks still
  deciding whether replay is safe; a skip label stops a pending split
  before any further child or relation is written.
- **Fixed — the backup and restore helpers work again** (#406). The
  helper container mounted the payload scripts over `/lib`, hiding the
  shell's dynamic loader, and BusyBox tar read the archive writer's
  directory options differently from GNU tar. Payloads now mount at
  `/devcake-scripts` on a digest-pinned Debian slim image with GNU tar;
  `DEVCAKE_BACKUP_IMAGE` overrides the helper (the older variable is
  still accepted; a custom image must carry GNU tar). Disposable CI now
  runs a real restore drill: refuses wrong-kind and corrupt archives
  without touching the destination, restores both volumes, compares
  hashes, modes, ownership and symlinks, boots the restored stack and
  finishes a new dispatch on it.
- **Fixed — Save & leave completes the navigation after a retry** (#407).
  When a save failed, Retry persisted the draft but never left the page;
  the requested navigation now stays pending while the results dialog
  offers a retry, a successful retry completes it, and closing the
  dialog cancels it.
- **Engineering** — mission dispatch and the steward prepare repository
  context through one shared function with a typed result (#402), guarded
  by a focused mypy contract gate on the cache port (#408); a CI matrix
  installs the built CLI wheel and sdist into clean environments and
  exercises the installed commands (#405); the token-copy browser check
  re-reads fetch-decorated options instead of racing them (#399).

## v0.5.6 (2026-09-05)

Patch release in the v0.5 "Java Lava" line.
[Release notes](https://github.com/flieber-inc/devcake/releases/tag/v0.5.6).

- **Added — a backend activity bar** (#396). A discreet, collapsible strip under
  every admin page shows what the app is doing right now — the poll cycle
  and segment, a mirror sync with its progress, a forge sweep, a dispatch,
  a finalize, a steward launch, a wait for tracker quota, a settings save
  waiting out the poll cycle — each with its duration, or how long the app
  has been idle and what ran last. A phase past twice its natural bound is
  marked overdue; a board whose poll segment was skipped for a tracker's
  quota reserve reads as waiting, not frozen, and a dead poll loop reads
  stalled. Fed by a new cheap `GET /api/v1/activity`; phases are registered
  at the same chokepoints as the tracing spans, never inferred.
- **Changed — DevCake's own writes invalidate the mirror freshness
  window** (#397). With `repo_mirror.sync_max_age_seconds` above zero, dispatches
  reuse a recent mirror sync; now a run finishing on its work repository,
  a mission completing on a merged pull request (merged by the app or
  found merged), a claims push to a notebook, or a Clear pruning the
  claims drops that repository's freshness so the next dispatch resyncs
  regardless of the window — also when the write lands while that
  mirror's sync is in flight (that sync does not count as fresh). The
  window covers passive staleness only, and a Dev's mid-run pulls always
  go to the real forge.
- **Fixed — a settings save that waited out a long poll cycle came back
  as a proxy 504 although the app applied it** (#397). The admin proxy now waits
  up to five minutes on API calls, so a slow-but-successful write is
  reported as what it was.

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
  [`skills/devcake-ops/SKILL.md`](skills/devcake-ops/SKILL.md)
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
