# 14 — Security (product contract)

> **Audience:** operators, implementers, and anyone writing outward-facing claims.
> **Status:** normative. Other docs link here for trust language; they must not
> claim a stronger security posture than this file.

## 0. Product security contract

DevCake is a **single-operator agent runtime for a dedicated host** (one VM or
machine you control). Anyone who can **write tickets** in the configured PMO
team or **land content** in a configured repository can influence coding agents
that hold your forge and model credentials. The control plane — admin basic
auth, Dagu (with host `docker.sock`), and the `/data` secrets volume — is
**equivalent to host root** for blast-radius purposes.

**Multi-tenant least-privilege SaaS is an explicit non-goal.** Capable defaults
(agents that can code, push branches, and reach the network) are intentional.
The primary defense against supply-chain damage is **outside the agent**: forge
branch protection, who is on the Linear team, human merge (`auto_merge` off by
default), and optional tighter credentials (read-only PAT, independent REVIEW
Dev Type). The app **warns** on weak posture; it does not nanny-gate most of
those choices. See §8.

If that contract is wrong for your environment, do not run DevCake there.

---

## 1. Asset inventory

| Asset | Blast radius if leaked |
|---|---|
| Model credentials (API keys, subscription OAuth tokens) | Billing abuse; subscription hijack |
| Forge tokens (write / RO / reviewer) | Repo write, PR approval/merge — supply-chain-grade |
| Linear API key | Read/write of the team's project data |
| `docker.sock` (via Dagu) | **Root-equivalent on the host** |
| `ADMIN_PASSWORD` / GUI secret store | All operator secrets on the volume |
| `/data` volume (or its backups) | Full secret dump — treat backups like a password-manager export |
| Mission content + repo content | Feeds agent prompts (trust zone B, §2) |

---

## 2. Three trust zones

### Zone A — Host / control plane

| Surface | Stance |
|---|---|
| Host `docker.sock` on **Dagu only** | **Design choice.** Required so Dagu can spawn sibling Dev containers. Dedicated host only (§5). |
| Admin panel HTTP basic auth + GUI secrets | **Design choice** (simplicity). Fine on loopback / dedicated host; publishing past localhost publishes every PAT (§4). |
| Bootstrap secrets in `.env` | Stack only (Dagu/Redis/OO/admin/Gitea/DOCKER_GID). Operator PMO/forge/model secrets are GUI-stored (ADR-0011). |
| Control-plane ports | Bind **loopback by default** (`13-deployment.md`). Remote access = SSH tunnel or host firewall — never “open to the LAN for convenience” without reading this file. |

Compromise of Dagu auth, the admin password, or the host is total for that machine.

### Zone B — Agent execution

| Surface | Stance |
|---|---|
| Mission text + repo content in prompts | **Trusted by design** (adult-operator / OpenClaw-class). Prompt injection is not a product defect (§3). |
| Forge + model credentials in the Dev | Required for clone/push and harnesses. Open **egress** by design (forge, package registries, model APIs). |
| MCP setup commands / `extra_cli_args` | **Admin-equivalent code execution** inside the disposable container (`11-admin-panel.md`). |
| Harness flags (`--dangerously-skip-permissions`, etc.) | Autonomous coding requires them; Devs are not a secure sandbox product (§6). |
| Unauthenticated OTLP to `otel-collector` | Residual on the dedicated host (self-noise / volume fill). Ops signal, not a tenancy boundary (§10). |

### Zone C — Software supply chain (primary mitigation)

This is where supply-chain attacks are **actually** contained — not inside the
LLM, and not by pretending tickets are sterile.

| Control | Owner | App role |
|---|---|---|
| Branch protection on the default branch (PR + reviews; Dev token cannot bypass) | **Operator** | Detects/warns unprotected branch; out-of-pipeline merge tripwire |
| Who can write issues/comments on the configured team | **Operator** | Single-team scope only (`05-pmo-adapter.md`) |
| Who can push to the repo the agent clones | **Operator** | — |
| `auto_merge` off (human merges) | **Operator** (default off) | Confirm dialog when enabling |
| Read-only forge PAT for non-EXECUTE stages | **Operator** (recommended) | Dismissable `forge-write-token` warning if missing |
| Independent REVIEW Dev Type (different model/role than EXECUTE) | **Operator** (recommended) | API/UI warning if shared — not a hard 422 |
| LEGAL_OUTCOMES + INV-4 (Dev never writes PMO; forged outcomes cannot approve own work via app deputy path) | **Product (hard)** | Enforced |

---

## 3. Prompt injection (design choice, not a bug)

v0 **trusts** the configured repository's content and Mission
descriptions/comments in the configured PMO team. Both flow into agent prompts.
Anyone who can write an issue in that team (or land content in the repo) can
attempt prompt injection against a Dev that may hold forge write credentials and
model keys.

**Stance:** accepted and intentional. The product *is* “ticket + repo in →
agent work out.” Users are adults; the job of the docs and product is to
**identify residual risk, highlight posture, and defend the supply chain**
(zone C) — not to promise injection-proof agents.

### What a successful injection can do (default-capable config)

- Steer the model’s plan, code, PR description, and review text.
- With the **write** forge token in-container: push branches, open/alter PRs
  (playbooks say not to force-push main — guidance, not kernel enforcement).
- If **no RO PAT** is configured: **every** stage (ONBOARD/PLAN/REVIEW/MAPPER)
  gets the same write-capable token as EXECUTE — a non-EXECUTE Dev can push.
- Exfiltrate env secrets over **open egress** (redaction does not cover Dev
  sockets — §7).
- With `auto_merge` **on** and weak forge protection: path to default branch
  depends on forge rules and reviewer token setup (`06-forge-adapter.md`).

### What the app actually enforces (not “the model behaved”)

- Single-team scoping — nothing outside the configured team key is polled.
- Devs never call the PMO (INV-4); app finalization is the only PMO writer.
- **`LEGAL_OUTCOMES`** (`03-mission-lifecycle.md` §6): e.g. EXECUTE claiming
  `reviewed` is refused; parked with `DEVCAKE-SKIP`, never acted on.
- `auto_merge` defaults **off**.
- Traces + audit log of PMO writes (`12-observability.md`, `10-persistence.md`).
- Out-of-pipeline merge **detection** when a PR is merged while the mission is
  still mid-pipeline (`15-errors-and-retries.md`).

These close **app-as-deputy** and **forged-result** paths. They do **not** stop
a capable agent from pushing bad code to a feature branch or exfiltrating tokens.

---

## 4. Credential handling (normative)

1. Secrets never in images, never in git, never in Run JSON, and **never in Dagu
   DAG params or YAML** — trigger params are rendered unmasked in the Dagu UI
   (verified on v2.10.5). Dagu receives `RUN_ID`, `IMAGE`, `TRACEPARENT`, plus
   one deliberate exception: the per-run scoped Redis ACL credential, revoked at
   finalization. Secret material is rebuilt on authenticated `runspec.get`
   (`09-messaging.md` §§3, 5).
2. Uploaded harness credential files: `/data/secrets/{dev_type}/…`, `0600`,
   delivered via runspec; entrypoint writes harness path then continues non-root
   (`07-dev-runtime.md`, `08-harness-templates.md`). No secret bind mounts into Devs.
3. Forge tokens reach git via credential helper, not embedded remote URLs on disk.
4. Secrets never logged at app choke points — redaction (§7).
5. Minimum forge scopes: `06-forge-adapter.md`. Linear key scoped by team choice.
6. **GUI secret store (schema v4, ADR-0011):** PMO/forge/model values entered in
   Config; stored `0600` under `/data/secrets/connections/` and
   `/data/secrets/harness/`. Never echoed (`GET /config` has no secret material;
   `secrets-check` = presence + timestamp only). `.env` = **bootstrap only**.

**Simplicity vs security:** GUI secrets behind HTTP basic auth on a dedicated
host with loopback binds is an intentional trade. Residual risk is the operator
workstation, a mis-bound port, shared `.env`/backups, and anyone who learns
`ADMIN_PASSWORD`. OIDC/SSO and external secret managers are optional if you
expose beyond that posture — not required for the default dedicated-host
deploy.

---

## 5. `docker.sock`

Held by exactly **one** service: **`dagu`**. The app kills and reconciles via
the Dagu REST API and holds no socket. **Never** Dev containers.

This is root-equivalent host access by design (sibling Dev containers). Normative
deployment: **dedicated VM/host**, not a shared Docker workstation with other
tenants’ workloads.

**DAG definitions** under `./dagu/dags` must be treated as trusted code that
can launch containers. Compose should mount them **read-only** into Dagu
(`13-deployment.md`); operators must not leave world-writable DAG trees on the
host.

---

## 6. Dev container isolation (design: not a sandbox product)

Intentional controls:

- Non-root harness user (uid 1000).
- No `docker.sock`; no host/volume secret mounts (runspec delivery).
- Attach only to `devcake_runtime`: Redis, otel-collector, internal Gitea.
  App/admin/Dagu/OpenObserve stay on `devcake_control` (OO left runtime — A23).
- Outbound network **enabled** (forge, packages, model APIs).

Not claimed:

- Multi-tenant isolation between hostile customers.
- Egress allowlists (optional future ops hardening, §11).
- **Docker HostConfig** CPU/memory/PID limits on the Dev container — Dagu 2.10.5
  `container:` schema has no HostConfig fields; DAG `resources.limits` is
  process-cgroup only. That is **engineering debt** (noisy-neighbor / fork bomb
  on the dedicated host), not adult-operator philosophy.
- MCP free-text commands and harness “skip permissions” flags are powerful on
  purpose (`11-admin-panel.md`, `08-harness-templates.md`).

Internal Gitea is dual-homed (control + runtime) so Devs can clone/push
zero-repo missions; the Gitea **admin** password never enters the Dev env
(ADR-0010).

---

## 7. Transcript redaction (hygiene, not a control plane)

**Boundary:** app→PMO and app→forge *writes* are scrubbed at choke points
(`MissionManager._feed`, forge PR comments, `create_mission` titles/bodies).
This does **not** cover Dev egress — a prompt-injected Dev can still exfiltrate
env over the network (§3).

Before transcripts/reports are posted, `security.redact` replaces known secret
values and token patterns with `«REDACTED»` (`app/devcake/security.py`):

- **Platform lists** — harness keys and infra passwords; model key shapes.
- **Registry contributions** — every registered PMO/forge adapter’s
  `secret_env_vars` / `token_patterns` (configured or not). Superset tripwire in
  `app/tests/test_security.py`.
- **Gitea internal tokens** — 40 hex; patterns would mask git SHAs. Value
  registration only (`register_runtime_secret` + `/data/secrets/internal_forge/`).
- **Runtime registry** — per-run Redis ACL password; empty after app restart
  (inbound envelope scrub still covers the dominant echo path).

Redaction matters **because Devs hold secrets**. It is the last line of defense
for **app-mediated** posts to Linear/forges, not a substitute for zone C.

---

## 8. Warnings vs gates

| Signal | Kind | Notes |
|---|---|---|
| `forge-write-token:*` | **Warning** (dismissable) | No RO PAT — all stages get write token |
| Unprotected default branch | **Warning** | Operator must fix forge-side |
| EXECUTE and REVIEW share Dev Type | **Warning** | Independent review recommended, not enforced |
| `gui-secrets-basic-auth` | **Info** | Reminder of control-plane posture |
| `auto_merge` enable | Confirm dialog | Operator accepts merge without human PR click |
| `LEGAL_OUTCOMES` violations | **Hard** | Illegal outcomes not applied |
| Out-of-pipeline merge | **Hard detection** | Comment + audit + health |
| INV-4 (Dev → PMO) | **Hard** | Architecture |

Dismissing a warning is an **explicit acceptance** of that residual risk. Prefer
keeping a mental (or health) inventory of active posture issues even after UI
dismiss.

---

## 9. Operator checklist (before first real EXECUTE)

1. **Dedicated host** — not a shared multi-tenant Docker host.
2. **Loopback-only** control ports (default compose) or SSH tunnel; do not bind
   admin/Dagu/OO to the public internet.
3. **Sandbox PMO team** (or tightly controlled membership) — ticket writers =
   agent trust.
4. **Branch protection** on the default branch; Dev token must not bypass.
5. Leave **`auto_merge` off** until you understand forge approval + reviewer token.
6. Prefer a **read-only forge PAT** for non-EXECUTE and a **different** Dev Type
   for REVIEW than EXECUTE.
7. Strong bootstrap passwords in `.env` (empty/`change-me*` refuse boot unless
   `DEVCAKE_ALLOW_INSECURE=1` — local sandbox only).
8. Read `/health` **security_warnings**; do not dismiss unread.
9. Treat **`/data` backups** as secret material.

Tutorials: `docs/tutorials/01-first-mission.md`, `13-deployment.md`.

---

## 10. Residual risk summary

### Zone A — Host / control plane

| Risk | Blast radius | Owns |
|---|---|---|
| Dagu or sock compromise | Host root | Design (dedicated host) |
| Admin password leak / mis-bound :8080 | All GUI secrets + config mutation + clear-runs | Operator + design |
| Volume/backup theft | All secrets | Operator (host encryption/backups) |

### Zone B — Agent execution

| Risk | Blast radius | Owns |
|---|---|---|
| Prompt injection via ticket/repo | Bad PR content; push; secret exfil | Design + operator (team/repo ACL) |
| Write token on non-EXECUTE | Push from “read” stages | Operator (RO PAT) |
| Open egress | Exfil of env | Design |
| No Dev cgroup HostConfig | Host resource exhaustion | Engineering debt (§11) |
| Unauth OTLP | Forged/flooded telemetry on this host | Design (dedicated host) |

### Zone C — Supply chain

| Risk | Blast radius | Owns |
|---|---|---|
| Unprotected default branch | Direct or weak path to main | **Operator** |
| `auto_merge` + weak review | Merged bad code | Operator |
| Shared EXECUTE/REVIEW identity | Weaker second look | Operator |

---

## 11. Engineering backlog (not philosophy)

Items that improve safety or hygiene **without** changing the adult-operator
contract:

- Docker HostConfig CPU/memory/PID limits on Dev containers (when Dagu supports
  them, or via another spawn path).
- Compose: read-only mount of `./dagu/dags` if not already.
- Protocol hardens: credential-upload filename allowlist + size cap; Linear
  `download_asset` host allowlist / redirect policy; stronger bootstrap password
  policy than a short deny-list.
- Optional: gVisor/Kata for Devs; egress proxy allowlists; OIDC if you must
  expose the admin UI beyond loopback; OTLP bearer auth on the collector
  (low priority on a dedicated single-tenant host).

**Not backlog (explicit non-goals):** multi-tenant SaaS hardening; treating
prompt injection as a ship-blocking defect; hard-gating dispatch on every
advisory warning by default; promising sandboxed multi-customer isolation.
