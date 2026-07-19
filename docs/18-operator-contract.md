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
| **Read `/health`** — `security_warnings`, `circuit_breakers`, `poll_degraded`, merge queue, needs-human | Routinely; always when something feels off | Admin Overview renders it ([`11`](11-admin-panel.md)); OpenObserve alerts back it ([`12`](12-observability.md) §5–6) |
| **Acknowledge breakers** | When tripped | `DEV_AUTH`: re-upload that Dev Type's credentials — the write clears the breaker ([`15`](15-errors-and-retries.md) §4). Repo breakers clear on a green probe: fix the token, wait a poll |
| **Back up `/data`** | Before every upgrade; weekly otherwise | Volume copy; it is a **secret dump** — store accordingly ([`10`](10-persistence.md), [`13`](13-deployment.md) §8) |
| **Back up `gitea_data`** | Same cadence, if the internal forge holds real work | `scripts/backup_gitea.sh` / `restore_gitea.sh` ([`13`](13-deployment.md) §8) |
| **Export a settings bundle** | Before risky config surgery | Admin Settings export — encrypted by default; store like a credential dump ([`11`](11-admin-panel.md)) |
| **Rotate secrets** | On a schedule you choose; immediately on suspicion or a team departure | §4 below |
| **Pause intake** | Before maintenance, upgrades, or anything that shouldn't dispatch new work | Sidebar master switch; in-flight runs finish ([`02`](02-domain-model.md), [`11`](11-admin-panel.md)) |
| **Re-run the fresh-`/data` drill** | Each release you adopt; quarterly otherwise | [operator drill](tutorials/operator-drill.md) — the standing stranger-operability proof |
| **Rebuild in lockstep on upgrade** | Every time `app/`, `admin/`, or `images/` change | `docker buildx bake all && docker compose up -d` ([`13`](13-deployment.md) §8, `AGENTS.md`) |
| **Treat Clear runs as a maintenance window** | When you press Clear runs | Dispatch (poll, hello, OAuth, mapper) is paused for the **entire** wipe, including OpenObserve stream deletes ([`11`](11-admin-panel.md)). Soft drain + Dagu force-stop, then wipe; if the response still lists undrained containers, check Dagu — the app has no `docker.sock` and cannot host-kill. In-flight finalize cannot resurrect local run files after the wipe (`store_gen`, [`10`](10-persistence.md)) |

## 4. Secret rotation (the procedure, in one place)

Rotation is four different motions depending on the secret:

1. **PMO and forge tokens** — mint the new token at the provider, paste it
   into the Config card's secret field, **Save**, then run the connection
   test. The write path hot-reloads adapters ([`11`](11-admin-panel.md)).
2. **Model / harness credentials** — upload via the Dev Type card (OAuth
   wizard, credential upload, or `scripts/grok_login.sh`). The write clears
   any `DEV_AUTH` breaker for Dev Types using that credential
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

The app warns; **you** gate (`14` §8):

- **Branch protection** on every work repo's default branch — the forge
  enforces; the app only surfaces an advisory ([`13`](13-deployment.md) §8a).
- **Team membership** — every ticket writer steers agents that hold your
  credentials (`14` §0).
- **Auto-merge** — off by default; turning it on is a judgment call the app
  will never make for you.
- **REVIEW separation** — a different Dev Type for REVIEW than EXECUTE is
  recommended configuration; the app warns when shared, nothing more.

## 6. Ownership map

[`14-security.md`](14-security.md) §10 assigns every residual risk an owner —
Design, Operator, or Engineering. The rows marked **Operator** are this page's
justification. Read them once; they are the honest edges of the product.
