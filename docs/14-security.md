# 14 — Security (product contract)

> **Audience:** operators, implementers, and anyone writing outward-facing claims.
> **Status:** normative. Other docs link here for trust language; they must not
> claim a stronger security posture than this file.

## 0. Product security contract

DevCake is a **single-operator agent runtime for a dedicated host** (one VM or
machine you control). Anyone who can **write tickets** on a configured PMO
instance or **land content** in a configured repository can influence coding
agents that hold your forge and model credentials. The control plane — admin
basic auth, Dagu (with host `docker.sock`), and the `/data` secrets volume — is
**equivalent to host root** for blast-radius purposes.

**Multi-tenant least-privilege SaaS is an explicit non-goal.** Capable defaults
(agents that can code, push branches, and reach the network) are intentional.
The primary defense against supply-chain damage is **outside the agent**: forge
**branch protection** (the control that actually stops a Dev token from landing
on the default branch), **who can write tickets** on each configured PMO
instance (Linear team, Gitea Issues board, …), the per-repo app-side **`auto_merge`
default off** (the control plane will not merge for you — it does **not** strip
merge capability from the Dev; see §2 zone C), and optional tighter credentials
(read-only PAT; **reviewer token** for formal forge approval). The app **warns**
on weak posture; it does not nanny-gate most of those choices. See §8.
(Assigning different Dev Types to EXECUTE vs REVIEW is a **performance** staffing
choice — skills and identifying prompts — not a supply-chain control.)

If that contract is wrong for your environment, do not run DevCake there.

### 0a. How to read this contract

This file mixes **design stance**, **implementation residuals**, and **fog at
the edges**. A reading aid (Rumsfeld’s three buckets) and the plain labels the
§10 residual-risk tables use for them:

| Term | Meaning here | §10 label · what to do |
|---|---|---|
| **Known known** | Named and accepted — by design (the deal) or as tracked debt (§11) | **Accepted** · don’t file it; read the stance (§§2–8) |
| **Known unknown** | The *kind* of failure is known; *whether / when / how hard* it hits is not | **Weather** · you can only watch — or **Verify** · operator-checkable state, retire it via §9 |
| **Unknown unknown** | Paths not inventoried (composition, novel tooling, platform surprises) | No label — a finding that maps to no §10 row and no §§2–8 stance is one; report it |

**Rule:** design choices — capable agents, prompt injection as input, open Dev
egress, credentials in the Dev under the current stack — are **known knowns**.
Do not reclassify them as unknown simply because they are sharp. The inverse
also holds: naming a control here never immunizes it — an implemented claim
that fails to hold (anything §8 marks **hard** or **gate**; INV-4; §7
redaction at its choke points) is a bug to report, not weather.

---

## 1. Asset inventory

| Asset | Blast radius if leaked |
|---|---|
| Model credentials (API keys, subscription OAuth tokens) | Billing abuse; subscription hijack |
| Forge tokens (write / RO / reviewer) | Repo write, PR approval/merge — supply-chain-grade |
| PMO credentials (per instance — Linear API key, Gitea Issues token, …) | Read/write of that instance's project data (one team/board per instance) |
| `docker.sock` (via Dagu) | **Root-equivalent on the host** |
| `ADMIN_PASSWORD` / GUI secret store | All operator secrets on the volume |
| Config profile snapshots (`/data/secrets/profiles/`) | A saved profile's full secret set — same class as the live store; dormant copies survive live rotation/deletion until the profile is deleted (ADR-0013) |
| Exported settings bundle (with secrets/setup values) | Password-manager-grade when it contains B or C; encrypted by default, plaintext only behind an explicit acknowledgment (ADR-0013) |
| `/data` volume (or its backups) | Full secret dump — treat backups like a password-manager export |
| `gitea_data` volume (or its backups) | Internal repo content + Gitea's credential DB — same handling as `/data` backups (`scripts/backup_gitea.sh`) |
| Mission content + repo content | Feeds agent prompts (trust zone B, §2) |
| `activity-*` repos on the internal Gitea (ADR-0014) | Full feed mirrors + complete Dev session transcripts, durably greppable by every future Dev until Clear; redaction at the feed/finalize choke-points is the only scrubbing — a human pasting a secret into the PMO feed lands it here. Single-operator posture; swept by Clear-runs. |
| `$DEVCAKE_WS_HOST` per-run workspace tree (ADR-0025) | A user-owned **host** directory holding each run's repo source, activity-transcript history, and agent/tool output, persisting from dispatch until cleanup (not bounded by container lifetime). `0700`, gitignored, DevCake-exclusive, excluded from the backup set (`13` §8). If tool output embeds a secret it lands here for the run's lifetime — treat a host snapshot like a repo backup. |

---

## 2. Three trust zones

### Zone A — Host / control plane

| Surface | Stance |
|---|---|
| Host `docker.sock` on **Dagu only** | **Design choice.** Required so Dagu can spawn sibling Dev containers. Dedicated host only (§5). |
| Host baker (`./up.sh` watcher) | **Same socket the operator already uses for `docker buildx bake`.** A host process — not a compose service, not a new socket-holder container. The app never talks to Docker. It publishes an allowlisted keep-set (**pins only**: known template + semver; re-validated at publish against house pins) and, for prune, a timestamp-only request file (no image names). The baker validates independently (known templates, semver, refuse pin `image`/`docker_image` fields, `devcake/dev-*` only) then compiles, probes, or prunes. |
| Admin panel HTTP basic auth + GUI secrets | **Design choice** (simplicity). Fine on loopback / dedicated host; publishing past localhost publishes every PAT (§4). **Since ADR-0013, admin auth includes one-request secret READ-OUT** via settings export — it was always host-root-equivalent in blast radius; export makes the equivalence direct. |
| Bootstrap secrets in `.env` | Stack only (Dagu/Redis/OO/admin/Gitea/DOCKER_GID). Operator PMO/forge/model secrets are GUI-stored (ADR-0011). |
| Control-plane ports | Bind **loopback by default** (`13-deployment.md`). Remote access = SSH tunnel or host firewall — never “open to the LAN for convenience” without reading this file. |

Compromise of Dagu auth, the admin password, or the host is total for that machine.

### Zone B — Agent execution

| Surface | Stance |
|---|---|
| Mission text + repo content in prompts | **Trusted by design** (adult-operator: mission and repo text are accepted as written). Prompt injection is not a product defect (§3). |
| Forge + model credentials in the Dev | Required for clone/push and harnesses. Open **egress** by design (forge, package registries, model APIs). |
| Operator “latest CLI” / “check for a newer version” | **Asked egress** to npm (`registry.npmjs.org`) and x.ai (`/cli/stable`). Not polled; not on editor open. The app never talks to Docker. |
| MCP setup commands / `extra_cli_args` | **Admin-equivalent code execution** inside the disposable container (`11-admin-panel.md`). |
| Skill-store skills (`DevType.skills` / `skills_required`) | **Operator-controlled agent instructions** (same trust class as the MCP command area; **ADR-0016**). Available skills are installed for optional consult; Required adds a soft-force prompt line only (not kernel-enforced). The store repo is writable by human Gitea accounts; mission Dev tokens are never collaborators on it (no machine user, no tokens — the app reads it with the admin credential), so an injected Dev cannot poison skills for future runs. The entrypoint refuses absolute/`..` paths and confines writes to the registry-declared skills dir under `$HOME` (the runspec `skills_dir` is itself validated home-relative — absolute/`..` values fall back to the default) — that guards file placement, not content. |
| External skills (`<source>/<skill>` — ADR-0016 addendum 2) | **Third-party supply chain, deliberately** (founder ruling): the skill content tracks the source's branch — upstream can change it at any time without review. Controls: the operator owns WHICH skill source and its credentials (read tokens under the `skill:` secret scope; sources have no PR surface and are read-only by construction); the sync is a fail-closed dispatch precondition (the ADR-0024 gate's needed-set union); reads pin one commit and record it on the Run (`skill_repo_heads` provenance); mirror-side filters admit only regular-blob files under regex-valid skill dirs (symlink entries skipped) with the payload caps applied before any content read — placement + budget guards, not content review. Point skill sources only at repos you trust like collaborators. |
| Harness flags (`--dangerously-skip-permissions`, etc.) | Autonomous coding requires them; Devs are not a secure sandbox product (§6). |
| Unauthenticated OTLP to `otel-collector` | Residual on the dedicated host (self-noise / volume fill). Ops signal, not a tenancy boundary (§10). |
| Default-board PAT (`pmo-board.json`) | **App-minted** (ADR-0030): the `devcake-board` service user's PAT (`write:issue` + `write:repository`), minted by the admin-credentialed provisioner at boot and liveness-checked by `token_last_eight` thereafter. Same separation doctrine as every PMO key (§ docs/05 9.1): it is not a forge token, and `GITEA_ADMIN_*` never leaves the app. Deleting it via Clear secrets self-heals at the next reload. |
| Internal Gitea mission tokens | **User-scoped, not repo-scoped** (live-verified). Isolation is one machine user + collaborator grant per mission (`adr/0010`), not forge-side repo ACLs. Admin password never enters Devs. **Extra in-container RO clones** expand the same class: **blocker RO mounts** (ADR-0017 — a dependent mission’s Dev may receive a **done** blocker’s `token_read` as an `extra_repos` credential), **cross-instance blocker mounts** (ADR-0009 amendment — a native board edge may pull a **peer instance's** done mission tree, e.g. a CS team's work repo, read-only into this instance's Dev; the edge is created only by writers already inside the board trust boundary, and Linear-only ids prevent misattribution), and **reference repos** (PMO `reference_repos` — RO clone of configured cards into every stage), and **consumer memory notebooks** (PLAN_MEMORY — RO clone to `/workspace/memory/<card>/` using the card's read token or the mirror; a card with no `token_ro` falls back to the write token TRANSIENTLY for the provision clone only — askpass env, never persisted in the clone — and the app logs a warning; store a `token_ro` to close even that window; the app writes `.claims/` with the card's **write token**, via git, on every registered forge — never a mission Dev token, never `GITEA_ADMIN_*`). Still one credential per listed repo, never the Gitea admin password. `reference_only` notebooks are skipped (audit, the discovering run continues). Clear-runs (the all-boards wipe) prunes EVERY `.claims/*.json` — orphans from since-deleted boards included; notes outside `.claims/` stay. `memory_auto_merge` OFF (default) leaves memory-bound PRs in `DEVCAKE-MERGE` for a person; ON is two models in a row, not a person and not the reviewer token (`14` honesty: REVIEW staffing is not a security control). |
| OpenObserve ingest vs UI roles | OSS OO role separation is **advisory** only — do not treat ingest-only accounts as a hard multi-tenant boundary. |

### Zone C — Software supply chain (primary mitigation)

This is where supply-chain attacks are **actually** contained — not inside the
LLM, and not by pretending tickets are sterile.

| Control | Owner | App role |
|---|---|---|
| Branch protection on the default branch (PR + reviews; Dev token cannot bypass) | **Operator** | Detects/warns unprotected branch; out-of-pipeline merge tripwire |
| Who can write issues/comments on each configured PMO instance | **Operator** | Per-instance scope only (one team/board key per instance — `05-pmo-adapter.md`) |
| Who can push to the repo the agent clones | **Operator** | — |
| Per-repo `auto_merge` off (**app** does not merge that repo) | **Operator** (default off per card) | Confirm dialog when enabling; **not** a Dev capability fence — see below |
| Read-only forge PAT for non-EXECUTE stages | **Operator** (recommended) | Dismissable `forge-write-token` warning if missing |
| Reviewer token (app-only; different account from write) | **Operator** (recommended for protected-branch formal approval) | Used by the app after REVIEW approve — never injected into a Dev; **no** dismissable health warning if missing (unlike RO) |
| LEGAL_OUTCOMES + INV-4 (Dev never writes PMO via the app deputy path; forged outcomes cannot approve own work) | **Product (hard)** | Enforced for app-mediated PMO writes and outcome application; **residual** when a forge-issues board is the same repo a Dev holds a forge token for — dismissable `pmo-forge-mono-repo:*` warning (§8), not a dispatch gate |

#### Per-repo `auto_merge` gates the app, not the Dev (normative)

Doctrine is per `RepoInstance` (ADR-0020); internal/zero-repo missions always auto-merge.

Two different actors can merge a PR. Do not conflate them:

| Actor | When `auto_merge` is **OFF** (default) | When `auto_merge` is **ON** |
|---|---|---|
| **App** (finalization / merge sweep via `ForgePort`) | Does **not** call `merge`. REVIEW-approve parks at `DEVCAKE-MERGE` until a real merge is observed. | Squash-merges after REVIEW approve; Done only after a real merge (`03` §4.1, `06` §5). |
| **Dev** (container with write forge token + `gh`/`glab`/API) | Still holds credentials that can open PRs **and**, on most forges, merge them if branch protection allows. Playbooks instruct create-PR only — **guidance, not kernel enforcement**. | Same residual capability. |

What is **hard** when `auto_merge` is off:

- The control plane will not merge for you.
- Agents cannot make the app merge by forging `result.json` (`LEGAL_OUTCOMES` + INV-4).

What is **not** hard from the toggle alone:

- Stripping merge rights from the Dev token (token scopes often cannot separate “push feature branch” from “merge to default branch” — `13` §8a, `adr/0007`).
- Preventing a prompt-injected or misbehaving agent from calling `gh pr merge` / the forge merge API.
- That stop is **forge branch protection** (require PR + reviews; Dev account must not bypass). Unprotected default branch → advisory only; dispatch is not blocked (`§8`).
- Out-of-pipeline merge is a **detection tripwire** (feed + audit + health), not a block or rollback (`15-errors-and-retries.md`).

**Do not claim** “auto-merge off means no agent can land on the default branch.” Claim: “auto-merge off means DevCake’s app will not merge; protect the default branch so the Dev token cannot either.”

#### Who holds which forge token (normative)

| Secret (Repositories GUI) | Injected into Devs? | Used by the app for |
|---|---|---|
| **Write / access** (`token`) | **EXECUTE** always; other stages only if no RO PAT is set | PR comments, state, and **squash-merge** when `auto_merge` is on |
| **Read-only** (`token_ro`, recommended) | Non-EXECUTE stages (ONBOARD / PLAN / REVIEW / STEWARD) | — (clone/read only in-container) |
| **Reviewer** (`reviewer_token`, recommended for formal forge approval) | **Never** — app-side only | **Formal** PR/MR approval (`ForgePort.approve`) after REVIEW Dev returns approve |

REVIEW’s job is judgment (`result.json`). Formal forge approval and merge are **app** side effects, never something the REVIEW container is handed credentials to do with the reviewer token.

#### Branch protection (what it is)

**Branch protection** is a **forge-enforced** policy on a branch name (your
repo’s `default_branch` — usually `main`). It is configured in GitHub / GitLab /
Gitea, not in DevCake. Typical rules: no direct push to that branch; changes
must arrive via PR/MR; merge requires ≥1 formal approval and/or green checks;
the Dev token’s account must **not** be on a bypass list.

Token scopes on most forges **cannot** grant “push feature branches + open PRs”
without also granting the API capability to merge when protection allows.
Protection is therefore the real containment for zone C; DevCake only **warns**
when the default branch looks unprotected (`13` §8a, `/health`).

#### End-to-end merge path (operator mental model)

With write + RO + reviewer tokens and default-branch protection:

1. **EXECUTE** (write token) → push feature branch, open PR.
2. **REVIEW** Dev (RO token if set) → approve or reject in `result.json` only.
3. **App** on approve → PR comment; formal approval with **reviewer** token
   (satisfies “require reviews” without self-approval on GitHub/Gitea).
4. **`auto_merge` off:** park `DEVCAKE-MERGE`; human merges; sweep → Done.  
   **`auto_merge` on:** app merges with the **write** token, then Done.

Without protection, step 1’s write token can often merge out of band; that is
detected as an out-of-pipeline-merge **tripwire**, not prevented (`15`).

---

## 3. Prompt injection (design choice, not a bug)

v0 **trusts** the configured repository's content and Mission
descriptions/comments on each configured PMO instance. Both flow into agent
prompts. Anyone who can write an issue on that instance (or land content in the
repo) can attempt prompt injection against a Dev that may hold forge write
credentials and model keys.

**Stance:** accepted and intentional. The product *is* “ticket + repo in →
agent work out.” Users are adults; the job of the docs and product is to
**identify residual risk, highlight posture, and defend the supply chain**
(zone C) — not to promise injection-proof agents.

### What a successful injection can do (default-capable config)

- Steer the model’s plan, code, PR description, and review text.
- With the **write** forge token in-container: push branches, open/alter PRs
  (playbooks say not to force-push main — guidance, not kernel enforcement).
- **Even with `auto_merge` off:** the same write token + forge CLI (`gh` /
  `glab` / API) can **merge** a PR if forge branch protection does not forbid
  it (§2 zone C). Off only stops the **app** from merging.
- If **no RO PAT** is configured: **every** stage (ONBOARD/PLAN/REVIEW/STEWARD)
  gets the same write-capable token as EXECUTE — a non-EXECUTE Dev can push
  (and, without protection, may merge).
- Exfiltrate env secrets over **open egress** (redaction does not cover Dev
  sockets — §7).
- With `auto_merge` **on** and weak forge protection: the **app** also merges
  after REVIEW approve — path to default branch depends on forge rules and
  reviewer token setup (`06-forge-adapter.md`).

### What the app actually enforces (not “the model behaved”)

- Single-team scoping — nothing outside the configured team key is polled.
- Devs never call the PMO as a client (INV-4); app finalization is the only
  PMO writer. A forge-issues board that shares a work-repo forge token is a
  warned residual (`pmo-forge-mono-repo:*`), not a second PMO client.
- **`LEGAL_OUTCOMES`** (`03-mission-lifecycle.md` §6): e.g. EXECUTE claiming
  `reviewed` is refused; parked with `DEVCAKE-SKIP`, never acted on — so an
  agent cannot make the app merge by forging outcomes.
- **`auto_merge` defaults off** (and enable is a confirm dialog): the **app**
  will not call `forge.merge` until the operator opts in. This is **not** a
  hard gate on Dev-side merge capability (§2 zone C).
- Traces + audit log of PMO writes (`12-observability.md`, `10-persistence.md`).
- Out-of-pipeline merge **detection** when a PR is merged while the mission is
  still mid-pipeline (`15-errors-and-retries.md`) — loud signal, not a block.

These close **app-as-deputy** and **forged-result** paths. They do **not** stop
a capable agent from pushing bad code to a feature branch, **merging a PR when
branch protection is weak or absent**, or exfiltrating tokens.

---

## 4. Credential handling (normative)

1. Secrets never in images, never in git, never in Run JSON, and **never in Dagu
   DAG params or YAML** — trigger params are rendered unmasked in the Dagu UI
   (verified on v2.10.5; re-verified at the pinned 2.13.0, 2026-08-13 — the
   dag-run API the UI renders returns `params` in clear, the scoped Redis
   credential included). Dagu receives `RUN_ID`, `IMAGE`, `TRACEPARENT`,
   `MEMORY_BYTES`, `NANO_CPUS`, `PIDS` (container limits, 2026-08-13), plus
   one deliberate exception: the per-run scoped Redis ACL credential, revoked at
   finalization. Secret material is rebuilt on authenticated `runspec.get`
   (`09-messaging.md` §§3, 5).
2. Uploaded harness credential files: `/data/secrets/{dev_type}/…`, `0600`,
   delivered via runspec; entrypoint writes harness path then continues non-root
   (`07-dev-runtime.md`, `08-harness-templates.md`). No secret bind mounts into Devs.
   **Grok OAuth refresh is control-plane only** (`ports/oidc_token` +
   `domain/grok_oauth`): the app may call `auth.x.ai` and rewrite the host
   `grok-auth.json` before inject. Mission Devs must **never** return
   `auth.json` / tokens to the app (no write-back channel) — that would be
   credential exfiltration past the one-way runspec boundary.
3. Forge tokens reach git via credential helper, not embedded remote URLs on disk.
4. Secrets never logged at app choke points — redaction (§7).
5. Minimum forge scopes: `06-forge-adapter.md`. PMO credentials scoped by
   instance choice (team key / board — `05-pmo-adapter.md`). Skill-source
   tokens are read-scoped by construction — a source has no PR surface
   (ADR-0016 addendum 2).
6. **GUI secret store (schema v4, ADR-0011):** PMO / forge / skill-source / model values entered in
   Config; stored `0600` under `/data/secrets/connections/` and
   `/data/secrets/harness/`. Never echoed (`GET /config` has no secret material;
   `secrets-check` = presence + timestamp only). `.env` = **bootstrap only**.
6a. **Config profile snapshots (ADR-0013):** saving a profile copies the
   current secret VALUES to `/data/secrets/profiles/{name}.json` (`0600`, at
   redaction-glob level — dormant values ≥16 chars are masked by the scan;
   shorter ones aren't until applied, the same residual as any `/data`
   backup). The API stays presence-and-counts only for profiles: list/read
   endpoints and apply previews never emit a value or a fingerprint. Note the
   rotation caveat: a profile holds the values **as of its save** — rotating
   a live secret does not touch snapshots, and applying an old profile
   restores the old value (the apply preview warns).
6b. **Settings export (ADR-0013) — the ONE sanctioned value egress:** an
   explicit operator POST may serialize stored secret values (and `.env`
   setup values) into a downloadable bundle. Encrypted by default (scrypt +
   AESGCM passphrase envelope); plaintext only behind
   `acknowledge_plaintext: true` with red UI warnings. POST-only (no values
   in URLs/access logs); every export appends an audit event recording
   sections + encryption mode; previews, profile reads, and the export
   summary stay presence-and-counts only. Imports are hardened
   operator-input: `yaml.safe_load` only, 20 MB cap, and validation errors
   scrubbed of input values.
7. **Dev-Type secret env (`DevType.secret_env`):** mission-tooling credentials
   (e.g. a log-platform key for an MCP plugin) are the same harness-namespace
   secret class — GUI-stored, `0600`, redaction-registered on write and at boot.
   Config holds only the **names**; runspec delivery is the same authenticated
   rebuild-on-request path as harness credentials. Accepted risk: a
   `claude mcp add -e VAR=$VAR` line persists the **expanded** value in the
   container's local claude config — a subset of the existing Zone B posture
   (Devs already hold their credentials in env). The claude config lives in
   `$HOME` (ephemeral, dies with the container), but a setup command written
   with `-s project` — or any tool that writes an expanded value into the
   working directory — now lands it on the ADR-0025 workspace **host bind**,
   where it persists for the run's lifetime, not the container's (§1 asset
   row; `0700`, reclaimed at run end). Same class: a *failing* MCP setup
   command's stderr tail goes to the container's raw stdout/stderr
   (`docker logs`) unredacted — anything reaching devcake's own failure
   records passes `redact()`, but the host log driver sees what the
   registration CLI chose to print.

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
can launch containers. Compose mounts them **read-only** into Dagu
(`docker-compose.yml`, `13-deployment.md`); operators must not leave
world-writable DAG trees on the host.

---

## 6. Dev container isolation (design: not a sandbox product)

Intentional controls:

- Non-root harness user (uid 1000).
- No `docker.sock`; no host/volume SECRET mounts (runspec delivery). The only mounts the **agent** container sees are its own per-run workspace bind (ADR-0025) — no ambient anything: the ADR-0024 source mirrors are mounted RO into the **provision** container ONLY, so the agent never sees `/mirrors`, a bare-pack duplicate of its own repo, or any other repo's source (this SUPERSEDES the old deployment-wide ambient read surface — ADR-0025 §8). The workspace bind carries repo CONTENT and agent/tool OUTPUT (which may embed a secret the tool printed), never delivered secret material, and is reclaimed at run end (§1 asset row) — nested-podman writes land there as foreign-uid files, so the dev-run DAG's exit handler re-chowns the workspace to uid 1000 at run end (success, failure, AND stop — live-drilled 2026-08-13) before the app deletes it; a dagu crash mid-run skips the handler and the app then logs the unreclaimable tree loudly (`/health` counts it as `workspaces.leaked`). The workspace/host disk itself is NOT covered by the container cgroup limits (pre-existing class — a Dev could always fill its bind; the nested engine makes that no easier and no harder).
- Attach only to `devcake_runtime`: Redis, otel-collector, internal Gitea.
  App/admin/Dagu/OpenObserve stay on `devcake_control` (OO left runtime — A23).
- Outbound network **enabled** (forge, packages, model APIs).

Not claimed:

- Multi-tenant isolation between hostile customers.
- Egress allowlists (optional future ops hardening, §11).
- Nested containers (rootless podman, ADR-0023 addendum 2026-08-13): Devs
  run a container engine INSIDE their own container — no socket, no
  privilege; the recorded cost is the dev-run seccomp profile widening
  Docker's default by ONE 15-syscall allow rule (userns/mount set — inline
  in the DAG, structurally pinned as never-unconfined) plus the /dev/fuse
  and /dev/net/tun device nodes. Kernel attack surface grows by exactly
  those syscalls + devices — for EVERY container the dev-run DAG launches,
  hello included, engine user or not; accepted (single-operator, dedicated
  host, Devs already non-sandbox §6).
- ~~Docker HostConfig CPU/memory/PID limits on the Dev container~~ —
  **DELIVERED 2026-08-13**: kernel cgroup limits ride every Dev container via
  `AppConfig.container_limits` → dev-run docker-executor `host:` (the old
  `container:`-shorthand era had no HostConfig path; `07-dev-runtime.md` §7).
  What is still NOT claimed: the limits bound one container, not a hostile
  operator config (0 = unlimited is a legitimate setting).
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
- **GUI secret store (disk + write/boot registration)** — every long-ish
  string value (≥16 chars) under `/data/secrets/*/*` is scanned; every store
  write also `register_runtime_secret`s the value, and boot calls
  `secrets.register_all()` so values under the 16-char scan floor stay exact-
  match masked (ADR-0011). System-managed files under `internal_forge/` use
  the same atomic write + scan-invalidation choke point.
- **Gitea internal tokens** — 40 hex; patterns would mask git SHAs. Value
  registration only (`register_runtime_secret` at mint/load + the disk scan
  of `/data/secrets/internal_forge/`). Factory registration for explicit
  Gitea adapter tokens is content-addressed (not a token prefix) so concurrent
  mission PATs cannot collide in the runtime map.
- **Runtime registry** — per-run Redis ACL password (empty after app restart
  until the next run mints one) plus the write/boot/factory registrations
  above. Inbound envelope scrub still covers the dominant Redis-password
  echo path.

Redaction matters **because Devs hold secrets**. It is the last line of defense
for **app-mediated** posts to PMO systems and forges, not a substitute for zone C.

**Scanner models:** CodeQL advanced setup loads
`.github/codeql/extensions/devcake-redact/` as a models-as-data pack that
declares `security.redact` as a taint barrier (the scanner-side mirror of this
chokepoint). When `redact()` moves, is renamed, or gains sibling scrubbers that
must also stop taint, update that model file in the same change — the pack is a
pinned mirror, not a second redaction implementation.

---

## 8. Warnings vs gates

| Signal | Kind | Notes |
|---|---|---|
| `forge-write-token:{repo}` | **Warning** in `security_warnings` (dismissable) | No RO PAT — all stages get write token for that repo |
| `repo-read-only:{repo}` | **Warning** in `security_warnings` (dismissable) | Repo is in a PMO work set but stores only a RO token (no write) — EXECUTE will fail at push; move it to reference repos or add a write token |
| `pmo-forge-mono-repo:{pmo}` | **Warning** in `security_warnings` (dismissable) | Forge-issues `team_key` normalizes to the same repository path (and host) as a configured work-repo URL — Dev forge token can reach that Issues board; prefer a dedicated Issues repo or Linear PMO |
| `gui-secrets-basic-auth` | **Info** in `security_warnings` | Reminder of control-plane posture |
| Unprotected default branch | **Advisory** via `/health` `forge_protection` (SPA alert) — **not** in the `security_warnings` list | Operator must fix forge-side |
| `secret_env` value missing **and** referenced by an mcp_setup_command | **Gate** (dispatch refused) | `blocked_reasons`/health names the var; self-heals the poll cycle after the value is pasted. Declared-but-unreferenced = warning only (log + ✗ on the Config card) |
| `auto_merge` enable | Confirm dialog | Operator accepts **app**-driven merge after REVIEW (not a Dev sandbox) |
| `LEGAL_OUTCOMES` violations | **Hard** | Illegal outcomes not applied |
| Out-of-pipeline merge | **Hard detection** | Comment + audit + health — does not prevent the merge |
| INV-4 (Dev → PMO) | **Hard** (app-mediated path) | Devs still have no PMO client and no PMO credential injected — the app is the sole PMO writer. **Residual** when a forge-issues board is the same repository a Dev holds a forge token for: forge-API reachability to that board is warned via `pmo-forge-mono-repo:*`, not refused at config save or dispatch |

Prefer a **dedicated Issues repo** (or the provisioned `devcake-pmo/missions` board) **or** a **Linear** PMO so board credentials stay disjoint from work-repo forge tokens.

Dismissing a warning is an **explicit acceptance** of that residual risk. Prefer
keeping a mental (or health) inventory of active posture issues even after UI
dismiss.

---

## 9. Operator checklist (before first real EXECUTE)

1. **Dedicated host** — not a shared multi-tenant Docker host.
2. **Loopback-only** control ports (default compose) or SSH tunnel; do not bind
   admin/Dagu/OO to the public internet.
3. **Sandbox (or tightly controlled) PMO membership** on every configured
   instance — ticket writers = agent trust.
4. **Branch protection** on the default branch; Dev token must not bypass —
   this is what stops a Dev from merging, not the auto-merge toggle alone
   (§2 zone C, `13` §8a).
5. Leave **`auto_merge` off** until you understand forge approval + reviewer
   token (off = app will not merge; protect the branch so Devs cannot either).
6. Prefer a **read-only forge PAT** for non-EXECUTE and a **reviewer token**
   (app-only, different account) for formal PR/MR approval under branch
   protection. REVIEW is always a pipeline stage; staffing which Dev Type runs
   it is not a security control.
7. Strong bootstrap passwords in `.env`: empty/`change-me*` refuse boot, and
   `ADMIN_PASSWORD` must be ≥ 12 characters. `DEVCAKE_ALLOW_INSECURE=1` waives
   both checks (local sandbox only).
8. Read `/health`: **`security_warnings`**, **`forge_protection`**,
   **`circuit_breakers`**, and (ops, not credentials) **`dev_backend_degraded`**
   if present — do not dismiss warnings unread (`15` §4 / §4a for breaker vs
   backend-throttle semantics).
9. Treat **`/data` backups** as secret material — including the config
   profile snapshots under `/data/secrets/profiles/`, which hold every
   secret value as of each save.
10. Store **settings export bundles** like credential dumps: prefer
    encrypted, never plaintext off the box, delete plaintext exports after
    use. Same handling for `gitea_data` backups (repo content + Gitea's
    credential DB).

Tutorials: `docs/tutorials/01-first-mission.md`, `13-deployment.md`.

---

## 10. Residual risk summary

Bucket labels per §0a: **Accepted** = the deal (or tracked debt — §11);
**Weather** = you can only watch; **Verify** = operator-checkable state —
retire it via the §9 checklist. The label names the decision-relevant aspect;
nearly every row also pairs an accepted blast radius with an uncertain
occurrence.

### Zone A — Host / control plane

| Risk | Blast radius | Owns | Bucket |
|---|---|---|---|
| Dagu or sock compromise | Host root | Design (dedicated host) | Accepted — the radius is the deal |
| Admin password leak / mis-bound :8080 | All GUI secrets + config mutation + clear-runs | Operator + design | Verify the bind (§9); the leak itself is weather |
| Volume/backup theft | All secrets | Operator (host encryption/backups) | Weather |
| Control-plane CVEs (Dagu, Redis, OO, Gitea images) | Component-dependent; sock-adjacent (Dagu) = host root | Design (pins) + operator (update cadence) | Weather — the next CVE is timing |

### Zone B — Agent execution

| Risk | Blast radius | Owns | Bucket |
|---|---|---|---|
| Prompt injection via ticket/repo | Bad PR content; push; **merge if unprotected**; secret exfil | Design + operator (team/repo ACL + branch protection) | Accepted — the capability is design; per-run outcome is weather |
| Write token on non-EXECUTE | Push (and potentially merge) from “read” stages | Operator (RO PAT) | Verify — set the RO PAT (§9) |
| Open egress | Exfil of env | Design | Accepted |
| Workspace host bind (ADR-0025) | The agent sees ONLY its own run's `/workspace` — no `/mirrors`, no other repo's source. On the HOST, the `$DEVCAKE_WS_HOST` tree holds repo content + agent output until run end | Design (`0700`, DevCake-exclusive, reclaimed at run end — the dev-run exit handler re-chowns nested-podman residue to uid 1000 first) + operator (host FS access) | Accepted — SUPERSEDES the old "shared RO mirror mount" ambient-read row; the agent's ambient read surface is now zero (mirrors are provision-only). Residual: a dagu crash mid-run skips the re-chown — the app warns loudly and `/health` gauges the leak |
| Dev cgroup HostConfig limits | Host resource exhaustion | **Closed 2026-08-13** — `container_limits` knobs, kernel-enforced per container (memory/CPU/PIDs; defaults 4g/2.0/off; live-verified `docker inspect`) | Retired — the residual is an operator choosing 0 = unlimited |
| Cross-Dev reachability on `devcake_runtime` | Concurrent Devs share one bridge with ICC on: Dev A can port-scan and connect to Dev B's in-container services — post-ADR-0023 that includes dev servers and a Chromium DevTools port (read B's page context, drive its browser) | Design (2026-08 evaluation) | Accepted — tracked debt (§11). ICC-off is NOT the fix: measured 2026-08-04, `enable_icc=false` blocks ALL container-to-container traffic on the bridge including Dev→Redis, severing the run bus. The real fix is per-run networks (docs/16 Candidates) |
| Malicious `MAXLEN` on the shared ingress stream | Any Dev holds `+xadd`, and XADD accepts MAXLEN: a hostile Dev can trim OTHER runs' unconsumed entries (artifact loss for concurrent runs). The flood variant is bounded: `maxmemory 1gb + noeviction` errors the writer instead of OOMing redis, and the app XDELs after every handled batch | Design (ticket-writer trust zone) | Accepted — per-run ingress streams are the real fix (docs/16 Candidates) |
| Unauth OTLP | Forged/flooded telemetry on this host | Design (dedicated host) | Accepted |
| `activity-*` repos: past transcripts greppable by every future Dev until Clear | Cross-mission exposure of anything choke-point redaction missed (§1, ADR-0014) | Design (single-operator) + operator (Clear cadence) | Accepted |

### Zone C — Supply chain

| Risk | Blast radius | Owns | Bucket |
|---|---|---|---|
| Unprotected default branch | Direct or weak path to main (agent **or** app) | **Operator** | Verify at the forge (§9): protection on **and** Dev account non-bypass — `/health` is advisory |
| Relying on `auto_merge` off alone | Agent still holds write token + CLI | **Operator** (must also protect branch) | Accepted — off gates the app only |
| `auto_merge` **on** + no reviewer token / weak forge review rules | App merges after REVIEW without formal forge approval | Operator | Verify — reviewer token + forge review rules (§9) |
| Human merges a bad PR | Bad code lands via the legitimate merge path | Operator (review process) | Weather — process risk outside the app |

**Read-across:** the sharp edges are mostly **accepted** properties of a
capable agent runtime on a trusted host; the rest splits into weather (watch)
and verifiable state (retire via §9). Anything that maps to no row here and no
stance in §§2–8 is an **unknown unknown** — report it. So is any implemented
claim that fails to hold (§0a Rule): mapping to a stance never immunizes
breaking it.

---

## 11. Engineering backlog (not philosophy)

Items that improve safety or hygiene **without** changing the adult-operator
contract:

- ~~Docker HostConfig CPU/memory/PID limits~~ (delivered 2026-08-13 — see
  §6). The ADR-0023 browser floor still sizes the per-container cap a run
  NEEDS — RAM is spent only when a Dev actually launches the browser
  (headless shell idle ≈150 MB, active pages 300–800 MB), so set
  `container_limits.memory_mb` with the browser working set in mind for
  browser-using fleets, and budget host RAM as
  `concurrency × container_limits.memory_mb`.
- Protocol hardens: ~~credential-upload filename allowlist + size cap~~
  (`secrets.require_credential_ref` + `MAX_CREDENTIAL_FILE_BYTES` + atomic
  `write_credential_file`); ~~PMO `download_asset` host allowlist / redirect
  policy~~ (`domain/asset_fetch.py` — Linear `uploads.linear.app`; Gitea Issues
  presentation hosts = `api_base` + `GITEA_UI_URL` + loopback, rewritten onto
  `api_base` origin, path pin `/attachments/`, netloc pin, body size cap;
  no off-allowlist redirects with auth headers). Operator use of the Gitea UI
  / direct git (migrate, clone, push) remains out of band and unrestricted by
  this app-side policy. Bootstrap password floor shipped: empty/`change-me*`
  deny-list plus `ADMIN_PASSWORD` ≥ 12 (`DEVCAKE_ALLOW_INSECURE=1` waives both).
- Optional: gVisor/Kata for Devs; egress allowlists / credential-injection
  proxy (e.g. [iron-proxy](https://github.com/ironsh/iron-proxy) class —
  deferred radar in `16-roadmap.md`; note the ADR-0023 baked browser widens
  the injection intake beyond curl — a Dev live-testing pages renders
  arbitrary third-party content straight into its context, same open-egress
  accept, bigger funnel); OIDC if you must expose the admin UI
  beyond loopback; OTLP bearer auth on the collector (low priority on a
  dedicated single-tenant host).

**Not backlog (explicit non-goals):** multi-tenant SaaS hardening; treating
prompt injection as a ship-blocking defect; hard-gating dispatch on every
advisory warning by default; promising sandboxed multi-customer isolation.
