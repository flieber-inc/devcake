# ADR-0010 — Internal fallback forge (bundled Gitea)

- **Status:** accepted (2026-07-14, docs/16 M11, feature F4)
- **Context:** DevCake must handle missions that resolve to **no configured repo** (M10's zero-repo gate) — the substrate for v0.2's non-developer workloads. Rather than special-case repo-less missions downstream, a mission with no repo gets a *perfectly ordinary forge repo* on a bundled internal Gitea, so EXECUTE/REVIEW/PR mechanics run unchanged (the strongest live test of F1's forge-agnosticism).

## Decision

Bundle **Gitea as a long-lived compose service** among the eight-service stack (`13-deployment.md` §1: app, dagu, redis, openobserve, admin, fluentbit, otel-collector, gitea). Image `gitea/gitea:1.24.7-rootless` (bumped to `1.27.1-rootless` 2026-08 — the 1.27.0 security batch is directly in the agent-reachable threat model), digest-pinned, sqlite3, loopback UI :3300, dual-homed on **control + runtime** like redis and the otel-collector — **not** like OpenObserve (OO is control-only after A23). A mission that resolves to no configured repo un-gates onto an auto-created internal repo at intake; on the REVIEW-approved merge, the changed files are zipped and attached to the PMO activity feed — the PMO stays the one place the user looks.

### Isolation — honest mechanism

Gitea tokens are **user-scoped, not repo-scoped** (live-verified). Isolation is therefore built from users, not scopes:

- a private org `devcake-internal`; two durable service users — `devcake-app` (org owner: PR ops + merges) and `devcake-reviewer` (formal approvals);
- **one machine user per mission** (`svc-{repo}`), collaborator on **only its own repo**, holding a **write+read `*:repository`-scoped token pair**. The write token reaches exactly one repo (cross-repo → 404); the read token 403s on writes. Non-EXECUTE stages receive the read token — the same per-stage scoping as external repos, without a separate RO PAT;
- `main` branch protection `required_approvals: 1` so the mission Dev cannot self-merge; the reviewer is on the protection's **approvals whitelist** (live-verified: a review counts as *official* only if the reviewer had write access OR was whitelisted when the review was created — provisioning whitelists before any approval).

The admin credential the provisioner uses (`GITEA_ADMIN_*`, a stack bootstrap secret) is the sharpest credential on the stack and **never leaves the app**. Per-mission token pairs live at `/data/secrets/internal_forge/*.json` (0600, two path levels so the existing redaction glob auto-covers them) and are runtime-registered.

### Redaction

Gitea tokens are 40 hex chars — a redaction regex would mask every git SHA in transcripts. So the descriptor's `token_patterns` is **deliberately empty**; value-registration (at mint/load) is the only redaction line for internal-forge tokens (docs/14 §7).

### Retention (founder decision, 2026-07-14)

Internal repos are **retained indefinitely** (the v0.2 non-dev-workload tests will want the history), with a **manual Clear** button in the admin panel now — `DELETE /api/v1/internal-repos/{name}` deletes the repo, its machine user (revoking both tokens), and the stored secret; it refuses while a live run is using the repo. Internal repos surface as a **read-only admin list** (founder decision) with a link into the Gitea UI. No automatic GC in v0.1.

## Consequences

- Zero-repo missions run fully autonomously and locally — acceptance for them costs no external forge tokens (M12's gitea lane).
- The Dev-side story is curl against Gitea's `/api/v1` (probe-verified `pr_instructions`), not an unverified `tea` CLI — no Dev-image rebuild, no speculative CLI flags. The recorded fallback (a `tea` CLI in the base image) stays available if API friction shows up.
- `ForgeCapabilities` is extracted here from the observed GitHub/GitLab/Gitea divergence (`mergeable_tristate`, `self_approval_blocked`, `branch_protection_read`, `pr_list_head_filter`); at least one call site (review's conflict-vs-handoff classification) branches on a capability instead of forge identity.
- The provisioner is constructed only via `adapters.registry.make_internal_forge`; day-to-day PR ops use the ordinary `ForgePort` from `InternalForgePort.mission_repo_binding` → `make_gitea_adapter` (explicit tokens + redaction registration). Both factories live in the registry so the F1 import tripwire stays honest. The naming convention (`internal_repo_name`) lives on `ports/internal_forge.py` so the domain derives it without importing the adapter. Provision-time `register_runtime_secret` at mint/load in `gitea/provision.py` is **value registration for empty `token_patterns`**, not a second adapter factory.
