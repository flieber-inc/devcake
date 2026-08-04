# ADR-0030 — Standalone DevCake: the auto-provisioned default board and the mission composer

**Status:** Accepted (2026-08-04)
**Amends:** `docs/19-thesis.md` §6 (the transcription carve-out), `docs/05-pmo-adapter.md` §9.4 (manual board setup becomes the external-Gitea path), `docs/16-roadmap.md` F4/M9 wording. Reverses the create-form deletion of PR #14 (`03a5af9`).

## Context

Two facts changed since PR #14 deleted the admin panel's create-mission form:

1. **The `gitea_issues` PMO adapter exists** (docs/05 §9) — a full, contract-tested `PMOPort` over the bundled Gitea's issue tracker. PR #14's argument was that a composer *duplicates Linear inferiorly* (no assignee, no relations, no attachments). That was right — against Linear. Against the bundled board, the "superior native UI" is an internal Gitea at `:3300` that the operator must separately log into; for the deployment whose only PMO **is** the bundled board, the argument inverts.
2. **The roadmap names the gap** (docs/16 F4): the zero-repo golden path needs no external forge credential, "though it still uses Linear". Linear was the last external dependency between DevCake and a fully standalone deployment: one machine, `docker compose up`, one AI-provider key.

Founder decision (2026-08-04, recorded verbatim in intent): make Gitea Issues a **first-class citizen** and support a **completely standalone DevCake** (only itself + an AI provider). Reversing PR #14 is judged "only net-positive… a low-hanging fruit that improves, without any real penalty, the experience and flexibility of the tool."

## Decision 1 — the doctrine amendment (transcription carve-out)

`docs/19-thesis.md` §6 holds: DevCake never **originates** intent. The composer does not breach that — the *operator* originates the intent; DevCake's form is a stenographer that transcribes it onto the board through the existing `PMOPort.create_mission`, keeping **no local record** (the PMO stays the single source of truth — INV-1 and ADR-0003 untouched). §6 gains one sentence making the distinction explicit: *DevCake may transcribe operator-originated missions onto the board; it still never originates one.* The mapper/decomposition limits are unchanged.

What stays out of scope, deliberately: DevCake initiating missions on its own signal (a failing metric, a stale dependency), conversational mission refinement, and cross-instance mission creation (docs/16 M9's "never of its own volition" survives with sharper wording).

## Decision 2 — the auto-provisioned default board (this PR)

Whenever the bundled provisioner is configured (`GITEA_ADMIN_PASSWORD` set), boot — and every config reload, opportunistically — ensures:

- **Org `devcake-pmo`, repo `missions`** (adopt-don't-refuse). A **third** org, deliberately: the mission lifecycle sweeps walk `devcake-internal` and clear-runs deletes `activity-*` in `devcake-repos`; a separate org makes the board structurally unreachable by every sweep.
- **Issue dependencies enabled under admin** — the board PAT is a write collaborator and cannot PATCH the repo; `blocked_by` (capability c, docs/05 §0) needs the tracker flag.
- **Service user `devcake-board`** with a repo-scoped PAT (`write:issue` + `write:repository`), liveness-checked by `token_last_eight` and re-minted only on definitive death (the recreated-volume self-heal). The PAT lands in the ordinary secret store as `pmo-board.json`. Credential separation (docs/05 §9.1) holds: board PAT ≠ forge tokens ≠ `GITEA_ADMIN_*`, and the admin credential never leaves the app (ADR-0010).
- **A persisted, managed `PMOInstance`** named **`board`** (reserved in `_pmos_valid`; the name prefixes branches and run ids like any instance, ADR-0009). The PMO side remains an ordinary `gitea_issues` instance through `make_pmo` — nothing wraps the forge adapter (docs/05 §9's hard rule; the F1 import tripwire still holds).

**Why a persisted row, not runtime synthesis:** the RepoInstance-synthesis precedent (docs/02 §9) rides a per-dispatch seam; PMO instances have none — `build_managers`, the poll loop, `/health`, the per-PMO intake endpoint (which *persists* into `config.pmos`), and the SPA all key off the config list. A synthesized instance could not even be paused. The cost of persistence is survival across wholesale list replaces, paid once in `reconcile_managed_pmos` (config.py), called at **both** world-swap choke points — `apply_config_patch` (before the removed-instance secret cleanup, or a PUT omitting the row would delete `pmo-board.json`) and `apply_bundle` (**profiles saved before this feature carry no board row** — without reconcile, a profile apply silently deletes the instance and its PAT). Identity fields stay canonical; `repos`/`reference_repos`/`assignments`/`intake_paused` stay operator-owned. A stray `managed: true` on any other incoming row is stripped (not refused — refusing would block legitimate cross-stack bundle imports).

**Deletability:** not deletable while the provisioner is present (the next boot would resurrect it; the SPA hides Remove and reconcile re-injects). With `GITEA_ADMIN_*` absent the row becomes deletable — an undeletable red card on a torn-out Gitea would be worse doctrine. Caveat: admin-env absence does not prove the Gitea *service* is absent; in that configuration the row keeps working on its stored PAT.

**Operator adoption:** an operator who manually configured `devcake-pmo/missions` pre-feature (docs/05 §9.4) would collide with the duplicate-target validator — provisioning detects the targeting instance and adopts it (no managed row injected, logged).

**First-run consequence:** a fresh bundled stack boots with a working PMO. The Overview checklist's "Connect a PMO" self-satisfies; the remaining step — give a Dev Type credentials — *is* the standalone story. The sidebar `pmo` dot goes green on fresh stacks and **red when the bundled Gitea is sick**, Linear-only operators included: honest red is doctrine (docs/11 preamble).

## Decision 3 — the New Mission composer (follow-up PR, same ADR)

- Writes to **any configured PMO** (founder decision) through `POST /api/v1/missions` → `PMOPort.create_mission`; **attachments in v1** (per-file caps from `capabilities().attachment_max_bytes`; uploads after create; ≥1 success posts one linking feed comment — Linear attachments are invisible unless referenced from a post). Submission guarded by a ConfirmDialog whose copy is assembled from the actual request (founder safety requirement).
- **Partial failure = 200-with-warnings, no rollback:** the mission exists; a 502 invites duplicate retries, and the port deliberately has no delete — auto-canceling what the operator just asked for would destroy intent. The remedy is disclosure with a live link.
- Title/description are **redacted before the port call** — the deleted dialog's "redacted before it's stored" copy was never true; the resurrected endpoint makes it true.
- Adoption gate honesty: in `opt_in` mode the composer offers "start work on next poll" (default on → the `DEVCAKE` label rides `create_mission`); never `DEVCAKE-CREATED` (the decomposition family-gate label). In `opt_out` the toggle would be a lie and is hidden.

## Consequences

- The standalone golden path is real: compose up → add a model key → New mission → merged PR, no external account anywhere.
- Failure modes all map to existing patterns: Gitea down at boot → best-effort lifespan block logs and continues, board probes red, healed at next boot/reload; PAT revoked → liveness re-mint; board repo deleted in the Gitea UI → recreated empty (operator action; issues are gone, stated honestly); volume wipe → full re-provision; `clear_secrets` can delete the PAT by operator selection → self-heals at reload.
- Zero-PMO boot remains a *defined* state (ADR-0009) — it is simply no longer the default on bundled stacks.
- The board adds one manager polling local Gitea every ~30 s — negligible.
- Rejected alternatives: runtime-synthesized instance (unpausable, N parallel rosters); refusing bundles that carry managed rows (blocks legitimate imports); deep-linking to Gitea's create form instead of a composer (defeats the second-surface purpose of the Missions tab; the operator would maintain a second login for the bundled board).
