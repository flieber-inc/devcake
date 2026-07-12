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
- `auto_merge` defaults **off**, so a human gate sits before the default branch; the toggle's confirm dialog states the consequence (`11-admin-panel.md` §2);
- every action is traced (`12-observability.md`) and every PMO write audit-logged (`10-persistence.md`).

**Honest limit — the Dev's own forge token (normative mitigation: branch protection).** An EXECUTE Dev holds `DEVCAKE_FORGE_TOKEN` inside its container (it must, to push its branch and open the PR), and on GitHub, pushing a feature branch and merging a PR both require the same `contents: write` permission — the capability is indivisible at the token level. The playbooks forbid direct merges, but that is guidance, not enforcement. **The effective control is forge-side branch protection on the default branch** (require PRs + ≥1 approval; no bypass for the Dev token's account). DevCake's own pipeline keeps working: the reviewer token files a formal approval before the app merges. Deployment requirement in `13-deployment.md` §8a. DevCake verifies and surfaces this: the forge connection test and `/api/v1/health` report the default branch's protection state, and the admin panel shows an amber warning when unprotected. A **detection tripwire** backs it up: if a mission's PR turns up merged while the mission is still mid-pipeline, the app posts an "out-of-pipeline merge" comment, audits `out_of_pipeline_merge`, and surfaces it in health (`15-errors-and-retries.md`). Per-run scoped forge tokens (§7) would not change this — they hit the same indivisibility.

Future hardening in §7.

## 3. Credential handling rules (normative)

1. Secrets never in images, never in git, and **never in Dagu DAG params or YAML** — trigger params are rendered unmasked in the Dagu UI and run API (verified live on v2.10.5). Dagu receives `RUN_ID`, `IMAGE`, `TRACEPARENT`, plus one deliberate exception: the per-run scoped Redis ACL credential, revoked at finalization and unreadable by other Devs since the Dagu UI/API is authenticated (`09-messaging.md` §1a). All real secret material travels over the per-run Redis `runspec.get` channel, scoped to exactly the Dev Type's needs, with the reply entry `XDEL`ed on acknowledgment and a 15-minute TTL cap (`09-messaging.md` §§3, 5). Redis itself requires auth; each Dev holds only its own scoped user, and every Dev→app message is identity-verified against it — spoofed run_ids and cross-run reads are rejected (`09-messaging.md` §1a).
2. Uploaded credential JSONs: `/data/secrets/{dev_type}/creds.json`, `0600`, app-owned; their **content** is delivered in the run spec and written by the Dev entrypoint to the harness path (`0600`), then privileges dropped (`07-dev-runtime.md` §5, `08-harness-templates.md` §4). No bind mounts into Dev containers.
3. Forge tokens reach git via a credential helper, never embedded in remote URLs on disk (`06-forge-adapter.md` §1).
4. Secrets never logged: the telemetry layer and the transcript renderer share a redaction filter (§5).
5. Minimum token scopes per forge listed in `06-forge-adapter.md` §§6–7; Linear key is a personal key scoped by team choice.

## 4. `docker.sock`

Held by exactly **one** service: `dagu` (spawning Devs — the mission-doc architecture requires it). The app kills and reconciles runs through the Dagu REST API instead (verified stop/status endpoints, `13-deployment.md` §4), so it holds no socket. **Never Dev containers.** This is root-equivalent host access and is flagged inline in `docker-compose.yml`; operators uncomfortable with it on shared hosts should run DevCake on a dedicated VM (the recommended production posture anyway).

## 5. Transcript redaction (normative)

Before any transcript or report is posted to the PMO System (an external SaaS), the renderer scans for the **known secret values** of the current config (every env-var value referenced by config, every uploaded credential file's key material) plus common token patterns (`ghp_…`, `glpat-…`, `sk-…`, `xai-…`, `lin_api_…`) and replaces them with `«REDACTED»`. The same filter wraps the OTLP log exporter.

## 6. Dev container hardening

- Non-root harness user (uid 1000 for the whole entrypoint; Claude Code enforces this itself by refusing `--dangerously-skip-permissions` as root — verified at M3).
- Resource limits (`07-dev-runtime.md` §7).
- No `docker.sock`; no host or volume mounts at all (credentials arrive via the run-spec channel).
- MCP free-text commands are **arbitrary code execution by design** — an admin-only surface, run inside the disposable container, labeled as such in the UI (`11-admin-panel.md` §2).

## 7. Future hardening (post-v0 backlog)

gVisor/kata runtime for Devs · per-run scoped forge tokens (GitHub App installation tokens) · egress allowlists per Dev Type · admin panel OIDC/SSO (v0 ships basic auth — `11-admin-panel.md` §5) · prompt-injection detection pass over ACTIVITY.md before harness launch · collector-side telemetry scrubbing.
