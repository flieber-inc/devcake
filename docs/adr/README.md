# ADR Index

> The corpus's navigation layer (2026-08 truth sweep — 26 ADRs with prose-only
> supersession had outgrown `grep -i supersed`). One row per ADR; the
> **Status** column is the ledger. "Amended"/"partially superseded" rows carry
> the change inline in the ADR itself — this table names where to look.
> New ADR ⇒ new row; superseding an ADR ⇒ update BOTH its row and its header.

| # | Title | Status |
|---|---|---|
| [0001](0001-redis-streams-for-dev-callback.md) | Redis Streams for the Dev → App channel | Accepted. Notes its own vaporware honestly (`devcake-relay` never shipped). ACL durability added 2026-08 (docs/09 §1a) |
| [0002](0002-file-based-persistence.md) | File-based local persistence | Accepted. **Amended 2026-08-04**: the backup sentence is superseded by docs/13 §8 (mirrors + workspaces exist now) |
| [0003](0003-pmo-as-single-source-of-truth.md) | PMO as single source of truth | Accepted — INV-1's home |
| [0004](0004-label-namespace-and-versioning.md) | Label namespace and versioning | Accepted |
| [0005](0005-no-lock-atomicity-via-pmo-state.md) | No-lock atomicity via PMO state | Accepted. The single-process premise is now test-enforced (`test_repo_structural.py`) |
| [0006](0006-projects-always-decompose.md) | Projects always decompose | Accepted |
| [0007](0007-mission-ordering-and-human-handoff.md) | Mission ordering + human hand-off | Accepted |
| [0008](0008-pluggable-pmo-and-forge-adapters.md) | Pluggable PMO and forge adapters | **Partially superseded** — three decisions struck in-body (schema v4, ADR-0009, multi-PMO) |
| [0009](0009-manager-per-pmo-instance.md) | One MissionManager per PMO instance | Accepted, **amended** (cross-instance blocker resolution) |
| [0010](0010-internal-fallback-forge.md) | Internal fallback forge (bundled Gitea) | Accepted |
| [0011](0011-gui-only-secret-store.md) | GUI-only secret store | Accepted. Store is plaintext-at-rest by design (0600 files; docs/14 §4) |
| [0012](0012-decomposition-depth-and-edge-inheritance.md) | Decomposition depth + edge inheritance | Accepted |
| [0013](0013-settings-bundle-profiles-and-export.md) | Settings bundle, profiles, export | Accepted |
| [0014](0014-activity-feed-fidelity-and-activity-repos.md) | Activity feed fidelity + activity repos | Accepted |
| [0015](0015-orchestrator-module-functions-and-api-composition.md) | Orchestrator module functions + composition root | Accepted, **amended by ADR-0028** (the composition root moved behind `build_services()`; the route-forward ratchet is unchanged). Close-out inventory drifted (the guard test allows seven residual forwards, the prose said nine — the ratchet is the truth) |
| [0016](0016-skills-and-prompt-assembly.md) | Skills and prompt assembly | Accepted |
| [0017](0017-blocker-work-ro-mounts-and-optional-pmo-zip.md) | Blocker work "RO mounts" + optional PMO zip | Accepted, **amended** (cross-instance). Read the body, not the title: the mechanism is an RO **token** + prompt contract in ordinary writable clone dirs — falling back to the write token when no `token_ro` is configured — not a filesystem mount |
| [0018](0018-harness-fault-classification-and-backend-brake.md) | Harness fault classification + backend brake | Accepted. The exit-11 "known gap" is now an operator toggle (ADR-0026) |
| [0019](0019-per-pmo-assignment-overrides.md) | Per-PMO assignment overrides | Accepted |
| [0020](0020-per-repo-merge-doctrine.md) | Per-repository merge doctrine | Accepted |
| [0021](0021-app-side-estimated-cost-and-operator-rate-card.md) | App-side estimated cost + rate card | Accepted |
| [0022](0022-in-container-run-continuation.md) | In-container run continuation | Accepted |
| [0023](0023-dev-toolchain-floor.md) | Dev container toolchain floor | Accepted |
| [0024](0024-mandatory-repo-source-mirror.md) | Mandatory repo source mirror | Accepted; **§5's ambient mirror-read superseded by ADR-0025** (banner in-body) |
| [0025](0025-provisioned-workspaces.md) | Provisioned workspaces | Accepted — supersedes ADR-0024 §5's risk posture |
| [0026](0026-attempt-reset-policy-and-bad-output-brake.md) | Attempt-reset policy + opt-in bad-output brake | Accepted (2026-08-04) |
| [0027](0027-failure-taxonomy-as-data.md) | Failure taxonomy as data | Accepted (2026-08-04) |
| [0028](0028-composition-root-factory.md) | Composition-root factory (`build_services()` + wiring-only main.py) | Accepted (2026-08-04); amends ADR-0015 |
| [0029](0029-normalized-result-shapes.md) | Normalized result shapes (TokenReport v1 + SQL-readiness) | Accepted (2026-08-04). Numbered 0029 but landed before 0028: it touches finalize/costing/runs_service, which the composition-root rewrite then leaves alone |
| [0030](0030-standalone-devcake-default-board-and-composer.md) | Standalone DevCake: auto-provisioned default board + mission composer | Accepted (2026-08-04); amends `docs/19` §6 (transcription carve-out), reverses PR #14's create-form deletion |
| [0031](0031-freshness-gate-for-context-closing-transitions.md) | The Freshness Gate: no context-closing transition on an unread feed | Accepted (2026-08-06); phase 1 (REVIEW finalize + sweep disclosure) implemented — amends docs/03/04/15. Phase 2 = decomposition cancel, pending |
| [0032](0032-mission-handoff-notes.md) | Mission handoff notes: narrative flows with the graph | Accepted (2026-08-06); amends ADR-0017 (blocker note), ADR-0014 D3 (MISSION.md), ADR-0012's lineage-note-only `append_description` charter |
