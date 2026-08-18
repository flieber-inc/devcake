# 18 — Operator Contract: What You Own

> **Audience:** the person who runs a DevCake host — before the first real
> EXECUTE and every week after. **Status:** adopted pre-v0.2 (2026-07-18).
> **Zero-drift rule:** this page consolidates *what* you own and *when*; the
> linked docs stay the single source for *how*. If this page and a linked doc
> disagree, the linked doc wins — then fix this page.

## 1. The deal in one paragraph

DevCake automates the work, not the trust. Your board is the interface for
delegation (`17`); this page is the other half of that sentence: you host the
stack, you decide who can write tickets, you protect the default branch, you
hold the merge button, and you keep the secrets and the backups. None of that
is busywork the product forgot to automate — it is the control that makes
autonomous work trustworthy (`14` §0). The app **warns** on weak posture; it
does not gate you (`14` §8). It assumes an adult operator.

## 2. Once, before the first real EXECUTE

The checklist is [`14-security.md`](14-security.md) §9 — ten items, from
dedicated host and loopback-only control ports to treating `/data` backups as
secret material. Do not start real work without it. The GUI-only path for
proving a fresh machine works is the
[operator drill](tutorials/operator-drill.md).

## 3. Recurring duties

| Duty | When | How (normative source) |
|---|---|---|
| **Read `/health`** — `security_warnings`, `circuit_breakers`, `poll_degraded`, merge queue, needs-human | Routinely; always when something feels off | Admin Overview renders it ([`11`](11-admin-panel.md)); OpenObserve alerts back it when you provisioned them ([`12`](12-observability.md) §5–6 — optional `scripts/provision_oo.py` + `OO_ALERT_WEBHOOK`; ingest connectivity is app-boot, not this script) |
| **Acknowledge breakers** | When tripped | `DEV_AUTH`: re-upload that Dev Type's credentials — the write clears the breaker ([`15`](15-errors-and-retries.md) §4). Repo breakers clear on a green probe: fix the token, wait a poll |
| **Back up `/data`** | Before every upgrade; weekly otherwise | `scripts/backup_data.sh` — secret dump; no-arg default under `~/.local/share/devcake/backups` ([`13`](13-deployment.md) §8) |
| **Back up `gitea_data`** | Same cadence, if the internal forge holds real work | `scripts/backup_gitea.sh` / `restore_gitea.sh` — same outside-checkout default and password-export handling ([`13`](13-deployment.md) §8) |
| **Re-run the fresh-`/data` drill** | Each release you adopt; quarterly otherwise | [operator drill](tutorials/operator-drill.md) — stranger-operability proof; pytest on backup payloads does **not** replace it ([`13`](13-deployment.md) §8 residual) |
| **Export a settings bundle** | Before risky config surgery | **Configuration → Profiles & Export** — encrypted by default; store like a credential dump ([`11`](11-admin-panel.md)) |
| **Rotate secrets** | On a schedule you choose; immediately on suspicion or a team departure | §4 below |
| **Pause intake** | Before maintenance, upgrades, or anything that shouldn't dispatch new work | Sidebar master switch; in-flight runs finish ([`02`](02-domain-model.md), [`11`](11-admin-panel.md)) |
| **Treat Clear run history as a maintenance window** | When you use it | Dispatch (poll, hello, OAuth, steward) is paused for the entire wipe, including OpenObserve stream deletes: Clear-runs holds the poll + dispatch locks, soft-drains and Dagu-stops live Devs, then wipes. `ok: false` with `undrained` means a container could not be stopped — inspect Dagu before re-running work because the app has no `docker.sock`. The store wipe generation prevents an in-flight finalizer from resurrecting deleted records ([`10`](10-persistence.md), [`11`](11-admin-panel.md), [`13`](13-deployment.md) §8) |
| **Do NOT treat a throttled Dev Type as a breaker** | When `/health` shows `dev_backend_degraded` | There is no credential to fix — DevCake has throttled that Dev Type to one probe run and resumes automatically once runs succeed. Check your model provider ([15] §4a) |
| **Tend team memory** | When `/health` shows `memory_curator_no_board` / `claims_queue_capped`; whenever a Curator PR waits | The notebooks are YOUR repositories (they survive Clear; only a full stack wipe takes them): their READMEs are the filing policy, and with `memory_auto_merge` off (default) every note becomes official only through your merge. A capped queue means leads are being refused — run the Memory Curator (Configuration → Scheduled Tasks) or raise the cap ([`11`](11-admin-panel.md), ADR-0035) |
| **Rebuild in lockstep on upgrade** | Every time `app/`, `admin/`, or `images/` change | `docker compose stop dagu` then `./up.sh --bake` so the live DAG bind cannot see a new `dev-run.yaml` without `DEVCAKE_WS_HOST` ([`13`](13-deployment.md) §8, `AGENTS.md`). Default `--bake` is control plane + hello; the host baker compiles configured harness pins. |

## 4. Secret rotation (the procedure, in one place)

Rotation is four different motions depending on the secret:

1. **PMO tokens** — mint the new token at the provider, paste it into the
   **PMO** page's secret field (`#/pmo`), **Save**, then run the named
   connection test. **Forge tokens** — same motion on **Repositories**
   (`#/repos`). **Skill-source read tokens** — same motion on the
   **Skill sources** card (Configuration → Skills). The write path
   hot-reloads adapters ([`11`](11-admin-panel.md)).
2. **Model / harness credentials** — upload via the Dev Type card under
   Configuration (OAuth wizard, credential upload, or `scripts/grok_login.sh`).
   The write clears any `DEV_AUTH` breaker for Dev Types using that credential
   ([`15`](15-errors-and-retries.md) §4).
3. **Stack bootstrap passwords** (`ADMIN_*`, `REDIS_*`, `DAGU_*`, `OO_*`,
   `GITEA_ADMIN_*`) — edit `.env`, then restart the stack; services take
   their passwords from `.env` at boot ([`13`](13-deployment.md) §8).
4. **After any rotation: re-save your profiles.** A profile snapshot holds
   secret values *as of its save*; applying a stale one resurrects the old
   secret. The apply preview warns on secrets “updated after this snapshot”
   ([`11`](11-admin-panel.md)) — treat that warning as a stop sign unless you
   mean it.

Old settings exports and old `/data` backups still contain the pre-rotation
secrets. Rotation is not complete until you have re-exported and re-backed-up —
or accepted that the old artifacts must now be guarded like the old secret.

## 5. What the app will not do for you

Operator scripts outside the FOSS CI path (not obligations for a stranger
clone's green build):

- **`scripts/acceptance.py`** — manual pre-release golden path; spends real
  model + PMO tokens; **not** `ci_suite` / default GitHub Actions.
- **`scripts/seed_sandbox.py`** — mutates a real Linear team; needs explicit
  API key + team key; sandbox fixtures only.
- **`scripts/export_receipts.py`** — evidence pack (not a backup); excludes
  `/data/secrets` but still holds private mission content.
- **`scripts/provision_oo.py`** — optional dashboard/alerts polish after boot
  already created the ingest user ([`12`](12-observability.md) §5).

The app warns; **you** gate (`14` §8):

- **Branch protection** on every work repo's default branch — a **forge**
  policy (require PR + reviews; Dev write account cannot bypass). The app only
  surfaces an advisory ([`13`](13-deployment.md) §8a). This is what stops a
  **Dev** from merging; `auto_merge` does not. Token scopes usually cannot
  separate push-branch from merge — see [`14`](14-security.md) §2.
- **Forge token split** — write for EXECUTE (and app merge when enabled); RO
  for non-EXECUTE (recommended); **reviewer token** (recommended for formal
  PR approval under branch protection) **app-only** — never given to a Dev.
  The REVIEW stage always runs; the Dev that staffs it only judges
  (`result.json`) and never receives the reviewer token
  ([`06`](06-forge-adapter.md) §4).
- **Team membership** — every ticket writer steers agents that hold your
  credentials (`14` §0).
- **Auto-merge** — off by default: the **app** will not merge PRs for you;
  turning it on is a judgment call the app will never make for you. Off is
  **not** a guarantee that no agent can merge — Devs still hold write tokens
  and forge CLIs (`14` §2 zone C).
- **Dev Type staffing for REVIEW** — optional performance choice (different
  skills / identifying prompt than EXECUTE). Not a security control; the
  security-relevant second identity is the **reviewer token**.
- **Note truth** — the app delivers leads and gates merges; it never judges
  whether a note is correct. A wrong note guides every later run until you
  revert it (git keeps every merge attributable). `memory_auto_merge` ON is
  two models in a row — a consent you give, never a default (ADR-0035).
- **Skill source trust** — an external skill source is third-party supply
  chain that tracks its branch: the app pins and records the commit per run
  and caps payload sizes, but never reviews content. Point sources only at
  repositories you trust like collaborators ([`14`](14-security.md) §2).

## 6. Ownership map

[`14-security.md`](14-security.md) §10 assigns every residual risk an owner —
Design, Operator, or Engineering. The rows marked **Operator** are this page's
justification. Read them once; they are the honest edges of the product.
