# 14 — Security

> **Audience:** implementers and adopters. An honest threat model for a system that runs autonomous coding agents holding credentials, next to a host Docker socket.

## 1. Asset inventory

| Asset | Blast radius if leaked |
|---|---|
| Model credentials (Anthropic/xAI/OpenAI keys, subscription OAuth tokens) | Billing abuse; subscription hijack |
| Forge tokens (GitHub/GitLab, incl. reviewer token) | Repo write, PR approval/merge — supply-chain-grade |
| Linear API key | Read/write of the team's project data |
| `docker.sock` | **Root-equivalent on the host** |
| Mission content + repo content | Feeds agent prompts (→ §2) |

## 2. Trust model and prompt injection

v0 explicitly **trusts**: the configured repository's content and the Mission descriptions/comments in the configured Linear team. Both flow into agent prompts, so anyone who can write an issue in that team (or land content in the repo) can attempt prompt injection against a Dev that holds forge write credentials.

v0 stance: **accepted risk**, mitigated by:
- single-team scoping (nothing outside `pmo.team_key` is ever read — `05-pmo-adapter.md` §2);
- Devs cannot write to the PMO (INV-4); code output lands in PRs (`06-forge-adapter.md` §3);
- **outcome legality is an app-side invariant** (`03-mission-lifecycle.md` §6, the `LEGAL_OUTCOMES` table): a forged `result.json` (e.g. an EXECUTE run claiming `reviewed`/approve) is parked with `DEVCAKE-SKIP`, never acted on — the app-as-deputy path is closed;
- `auto_merge` defaults **off**, so a human gate sits before the default branch; the toggle's confirm dialog states the consequence (`11-admin-panel.md` §3);
- every action is traced (`12-observability.md`) and every PMO write audit-logged (`10-persistence.md`).

**Honest limit — the Dev's own forge token (normative mitigation: branch protection).** An EXECUTE Dev holds `DEVCAKE_FORGE_TOKEN` inside its container (it must, to push its branch and open the PR), and on GitHub, pushing a feature branch and merging a PR both require the same `contents: write` permission — the capability is indivisible at the token level. The playbooks forbid direct merges, but that is guidance, not enforcement. **The effective control is forge-side branch protection on the default branch** (require PRs + ≥1 approval; no bypass for the Dev token's account). DevCake's own pipeline keeps working: the reviewer token files a formal approval before the app merges. Deployment requirement in `13-deployment.md` §8a. DevCake verifies and surfaces this: the forge connection test and `/api/v1/health` report the default branch's protection state, and the admin panel shows an amber warning when unprotected. A **detection tripwire** backs it up: if a mission's PR turns up merged while the mission is still mid-pipeline, the app posts an "out-of-pipeline merge" comment, audits `out_of_pipeline_merge`, and surfaces it in health (`15-errors-and-retries.md`). Per-run scoped forge tokens (§7) would not change this — they hit the same indivisibility.

Future hardening in §7.

## 3. Credential handling rules (normative)

1. Secrets never in images, never in git, never in Run JSON, and **never in Dagu DAG params or YAML** — trigger params are rendered unmasked in the Dagu UI and run API (verified live on v2.10.5). Dagu receives `RUN_ID`, `IMAGE`, `TRACEPARENT`, plus one deliberate exception: the per-run scoped Redis ACL credential, revoked at finalization. Run JSON stores only its one-way verifier. Real secret material is never at rest anywhere between dispatch and pickup: the app rebuilds it from current config when an authenticated *active* run sends `runspec.get`, copies it into the per-run reply (15-minute TTL), and XDELs that entry on acknowledgment (`09-messaging.md` §§3, 5).
2. Uploaded credential JSONs: `/data/secrets/{dev_type}/{secret_file}` (filename fixed by the harness registry, e.g. `grok-auth.json`), `0600`, app-owned; their **content** is delivered in the run spec and written by the Dev entrypoint to the harness path (`0600`), then privileges dropped (`07-dev-runtime.md` §5, `08-harness-templates.md` §4). No bind mounts into Dev containers.
3. Forge tokens reach git via a credential helper, never embedded in remote URLs on disk (`06-forge-adapter.md` §1).
4. Secrets never logged: the telemetry layer and the transcript renderer share a redaction filter (§5).
5. Minimum token scopes per forge listed in `06-forge-adapter.md` §§6–7; Linear key is a personal key scoped by team choice.

## 4. `docker.sock`

Held by exactly **one** service: `dagu` (spawning Devs — the mission-doc architecture requires it). The app kills and reconciles runs through the Dagu REST API instead (verified stop/status endpoints, `13-deployment.md` §4), so it holds no socket. **Never Dev containers.** This is root-equivalent host access and is flagged inline in `docker-compose.yml`; operators uncomfortable with it on shared hosts should run DevCake on a dedicated VM (the recommended production posture anyway).

## 5. Transcript redaction (normative)

Before any transcript or report is posted to the PMO System (an external SaaS), the renderer scans for the **known secret values** of the current config (every env-var value in the secret list, every uploaded credential file's key material) plus known token patterns and replaces them with `«REDACTED»`. The same filter wraps the OTLP log exporter.

The lists are assembled in two parts (`app/devcake/security.py`):

- **Platform lists** — `PLATFORM_SECRET_ENV_VARS` (harness keys and infra passwords: `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `XAI_API_KEY`, `OPENAI_API_KEY`/`CODEX_API_KEY`, `REDIS_PASSWORD`, `DAGU_PASSWORD`, `ADMIN_PASSWORD`, `OO_ROOT_PASSWORD`) and `PLATFORM_TOKEN_PATTERNS` (harness/model key shapes `sk-…`, `xai-…`) — static, not adapter-owned.
- **Registry contributions** — every **registered** PMO system and forge adapter contributes its `secret_env_vars` and `token_patterns` through its registry entry / `ForgeDescriptor`, **configured or not**, so switching adapters never opens a redaction gap. The forge/PMO token shapes (`ghp_…`, `github_pat_…`, `glpat-…`, `lin_api_…`, `lin_oauth_…`) all come from here, not from a hardcoded list. The registry import is lazy and memoized at the first `redact()` call (security is imported by the adapters themselves, so it can't import them at module load).

`secret_env_vars()` / `token_patterns()` expose the unions, and a **superset tripwire** in `app/tests/test_security.py` pins them: the union must remain a superset of the v0 lists, and every registered adapter's contributions must be included — a refactor can add shapes but can never silently drop one.

- **Runtime registry** — ephemeral per-run credentials (the scoped Redis relay password) exist only as process-local values, so `redact()` learns them through an in-memory registry: registered when the ACL user is created, dropped when it is deleted. Accepted limitation: the registry is empty after an app restart, so a transcript posted post-restart for a pre-restart run misses this layer — the inbound `_scrub_envelope_auth` filter (which masks the credential inside the Dev's own artifact payload) still covers the dominant echo vector, and the ACL user is dead by teardown anyway. Plaintext is deliberately never persisted to close this gap.

## 6. Dev container hardening

- Non-root harness user (uid 1000 for the whole entrypoint; Claude Code enforces this itself by refusing `--dangerously-skip-permissions` as root — verified at M3).
- Resource limits (`07-dev-runtime.md` §7).
- No `docker.sock`; no host or volume mounts at all (credentials arrive via the run-spec channel).
- Devs attach only to `devcake_runtime`: Redis and OpenObserve are reachable, while the app/admin/Dagu control plane is absent from that network. Outbound forge/package access remains enabled.
- MCP free-text commands are **arbitrary code execution by design** — an admin-only surface, run inside the disposable container, labeled as such in the UI (`11-admin-panel.md` §3).

## 7. Future hardening (post-v0 backlog)

gVisor/kata runtime for Devs · per-run scoped forge tokens (GitHub App installation tokens) · egress allowlists per Dev Type · admin panel OIDC/SSO (v0 ships basic auth — `11-admin-panel.md` §6) · prompt-injection detection pass over ACTIVITY.md before harness launch · collector-side telemetry scrubbing.
