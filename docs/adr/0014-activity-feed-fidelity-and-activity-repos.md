# ADR-0014 — Activity feed fidelity and per-mission activity repos

- **Status:** accepted (2026-07-18; founder decisions in audit review — implementation pending, phases below)
- **Context:** The step-conclusion feed post is inverted from its intent: the `{seq}_{TYPE}.md` attachment carries only the Dev's **last** harness message (plus outcome JSON) and the comment is a one-line pointer — while the full run transcript is never durably captured for Claude/Codex (the raw stream-json lives only in `dev_entrypoint.py` process memory and dies with the container; only Grok ships a full session via `grok export`). Separately, the activity folder a Dev receives is a Redis-borne snapshot with no history: nobody can later see what a Dev *actually* received at step N, and comment edits/deletions in the PMO silently rewrite the past. Mission repos exist only for zero-repo missions (ADR-0010), and the original "activity mirror" idea was discarded 2026-07-14 (docs/16 discarded list) — before the internal forge existed to make it cheap.

## Decision 1 — step-end feed contract: last message inline, full dump attached

At every step conclusion the feed receives ONE step comment with both halves swapped from today:

- **Comment body** = the Dev's last harness message, rendered as a `>`-blockquote (see Decision 2), preceded by the step line that carries the backticked `` `{seq}_{TYPE}.md` `` STEP_MARKER (docs/02 §8 seq derivation is untouched) and followed by the `devcake:v1` sentinel via the existing `_feed` choke-point. The finalize post is exempt from `FEED_INLINE_MAX` externalization; a pathologically long last message is truncated with a pointer to the attachment, which contains it anyway.
- **Attachment** `{seq}_{TYPE}.md` = the **full dump**: every assistant-visible text block the harness emitted across the run, in order, under the existing header (dev/turns/duration) plus the `## Outcome` JSON. "Text" means assistant text only — no thinking blocks, no tool payloads: dumps stay meaningful, greppable, and off the secret-echo path. Capture is per-harness in `dev_entrypoint.py`: Claude = text blocks from `stream-json` assistant events; Codex = agent-message items; Grok keeps `grok export`. Failure paths (exit 11 etc.) keep the same two-part shape. The full raw dump rides the existing chunked `run.artifacts` envelope (50 MB assembled cap — ample; text dumps are typically well under 1 MB).

Redaction stays at the finalize choke-point and now covers both halves.

## Decision 2 — feed-scan hardening (phase 0, blocking)

Inline model prose entering the feed is prose entering the **state machine**: two scans read raw bodies today and both are corruptible by a last message that merely *mentions* the wrong string — `_derive_seq` counts `STEP_MARKER` over `e.body` (a Dev writing "see `2_PLAN.md`" inflates the step counter), and the deliverable idempotency guard skips delivery if any body contains `{key}-deliverable.zip`. Therefore, **before** Decision 1 ships:

- Every feed scan applies `_unquoted` (`_derive_seq`, the deliver guard; conflict/sweep/sentinel scans already do).
- All model-authored text posted inline is `>`-blockquoted — quoting is thereby the single, already-established quarantine convention: markers inside quotes never count, for humans and Devs alike.

This is a standalone correctness fix (it hardens against humans quoting DevCake comments too) and lands as its own PR with tests proving a marker-shaped string inside a quoted last message changes nothing.

## Decision 3 — the activity folder: MISSION.md + a faithful feed mirror

The folder contract (docs/07 §2) becomes three parts, replacing "chronological index + externalized long bodies":

- **`MISSION.md`** — the brief, extracted from ACTIVITY.md's former header: key, title, kind/status/priority/URL, labels, and the **full description**. Every step playbook points at `MISSION.md` explicitly; it is the stable "what is this mission" file regardless of feed length. (Projects: MISSION.md is the whole brief; ACTIVITY.md keeps its "(projects carry no comment feed)" stub.)
- **`ACTIVITY.md`** — a **faithful mirror of the feed as seen in the PMO**: every post and reply appears inline with its full body (the 2048-char preview/`entry-*.md` externalization for feed bodies is removed — a human's long comment is inline material in the feed, so it is inline here; DevCake's own long posts were already externalized to attachments *at post time* and mirror as preview + attachment name, exactly as Linear shows them). Every attachment appears as `[attachment: name]` at its position in feed order. Provenance markers (🧑/🤖) and the reply structure (comment `parent` is now fetched and rendered as an indent/"↳ reply to" line) are preserved. Full dumps are **never** inlined — they are attachments and appear by name only.
- **Sibling files** — the bytes of **every** attachment in the feed, including the full dumps, under deduped names. Coverage widens to close the known holes: the mission **description** is scanned for asset URLs, and the PMO's **native attachment list** is queried where the vendor has one (Linear's issue `attachments` connection), not just comment-body regex hits.

`get_activity` grows a depth parameter: the activity-folder builder walks the full comment history (fail-loud if the vendor truly truncates); the four marker-scan call paths keep today's shallow window to hold Linear GraphQL cost flat.

## Decision 4 — carrier: per-mission activity repos under `devcake-repos`, deleted on Clear

Every mission gets **its own repo** `activity-{instance}-{key}` in the operator org `devcake-repos` (founder decision: one repo per mission for browsability — not a single archive repo; the `activity-` prefix is the sweeper's discriminator against operator repos and the skill-store, whose names cannot contain hyphens). Properties:

- **App-written only**, via the existing Contents-API multi-file-commit wrapper (skill-store pattern): before every step dispatch, the freshly built folder (MISSION.md, ACTIVITY.md, siblings) is committed as one commit "step {seq} {TYPE} dispatch". Unprotected `main`; no per-mission machine users or token pairs — Devs clone with **one shared read-only service credential** (`devcake-activity-ro`, minted at boot beside the existing service accounts).
- **Cloned into `/workspace/activity`** in every Dev run — full history, not `--depth 1` (history *is* the payload: `git log -p ACTIVITY.md` in-container shows the mission's evolution, including pre-edit states of comments later edited or deleted in the PMO — the one thing a rebuild-from-feed can never recover). Every existing prompt reference to `/workspace/activity/` stays valid verbatim.
- **Availability fallback:** provisioning/push failure never gates dispatch — it audits loudly and the run proceeds; the entrypoint falls back to today's Redis `activity.get` materialization into the same path when the clone is unavailable. Gitea down degrades to current behavior, never to a halt.
- **Deleted on Clear**, alongside the mission's runs: the PMO is the single source of truth (ADR-0003) and the repo is a disposable operational record of what went inside the Devs — the same wipeable-advisory posture as the runlogs. Clear's confirm copy names the extra loss honestly: repo history includes pre-edit feed states the PMO no longer shows. `devcake-repos`' invariant is restated: *operator repos and the skill-store* are never swept; `activity-*` repos are swept with Clear.
- **Zero-repo missions are unchanged as work goes**: they keep their protected `devcake-internal` work repo (ADR-0010) and receive the activity repo read-only like every other mission. Record and deliverable never share a repo.

**Rejected — single per-instance archive repo** (`missions/{key}/` paths): fewer repos and free cross-mission grep, but Clear becomes a history-rewriting partial delete instead of a repo drop, and it loses the founder's one-repo-per-mission browsing model. Cross-mission introspection can ride later as an opt-in reference clone. **Rejected — activity in the work repo / `devcake-internal`**: protected `main` blocks app pushes (no push whitelist exists, deliberately), would mix record with deliverable, and would re-tangle Clear with ADR-0010's keep-indefinitely work-repo retention. **Relation to ADR-0013's Gitea rejection:** that rejection was about *settings* — boot-critical, secret-bearing, needing coherence with live files. Activity repos are none of those: advisory, feed-derived, write-once-per-step, never read back by the app.

## Consequences

- **Security posture (docs/14):** feed content and full dumps become durably greppable by every future Dev via Gitea instead of living only in wipeable payloads and PMO attachments. Redaction at the feed/finalize choke-points is the only scrubbing; a human pasting a secret into Linear now lands it in a repo until Clear. Accepted under the single-operator trust model; docs/14 gains the asset row.
- The activity payload builder becomes push-then-clone instead of pure Redis; the Redis path remains as the documented degraded mode. A future optimization (roadmap): build incrementally against the repo's last-pushed state instead of re-downloading every asset each step.
- Docs drift to ride the implementation PRs: docs/02 §8 (quoting + hardened scans), docs/03 §7, docs/05 §3/§4 (deep fetch, native attachments), docs/07 §1/§2/§5 (folder contract, MISSION.md, clone-first materialization), docs/16 (un-discard note pointing here), docs/14.
- **Phases:** 0 = Decision 2 (standalone PR, blocking); 1 = Decision 1 (capture + flip); 2 = Decision 3 (folder contract); 3 = Decision 4 (repos + Clear + fallback). Each phase is independently shippable; 1–3 change what Devs and operators see and should be live-verified on the Linear sandbox per Always Works™.
