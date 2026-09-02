---
name: devcake-ops
description: Install, deploy, and manage a DevCake stack on this machine — preflight, bring-up, non-interactive setup, day-to-day operations, upgrades, and troubleshooting through the devcake CLI and the admin API. For agents operating a deployment on behalf of a human, not for developing DevCake itself.
---

# Operating DevCake

You are helping a human install and run **DevCake**, a self-hosted system that
turns tickets on their project board into pull requests, executed by AI
developers in disposable containers. What you operate is: a **Docker Compose
stack** (orchestrator app, admin UI, Dagu scheduler, Redis, OpenObserve,
optional bundled Gitea), a **host baker** process that compiles agent images,
and the **`devcake` host CLI** that drives all of it. Everything binds to
loopback; there is no hosted service.

Ground rules before anything else:

- **Never run `docker compose down -v`** or delete Docker volumes. The `/data`
  volume holds the operator's secrets and run history; `-v` destroys them.
- **The CLI never uses sudo, and neither should you** without the human's
  explicit approval. `devcake doctor` prints any one-time privileged remedy
  (like joining the docker group) for the human to run themselves.
- **Operator secrets (PMO API keys, forge tokens, model credentials) never go
  in `.env`** — they enter through the admin UI or `devcake setup`, always via
  env/file/stdin, never on a command line.
- Treat `.env` and any `/data` backup like a password-manager export.
- Ask the human before anything destructive or outward-facing (wiping state,
  changing forge settings on real repositories, exposing ports).

## 1. Prerequisites

- Linux or macOS on a **dedicated machine** (the stack holds `docker.sock`,
  which is root-equivalent — not for shared hosts).
- Docker Engine or Docker Desktop, with **Compose** and **Buildx** ("bake").
  Podman/buildah is not sufficient — `devcake doctor` detects the shim.
- Python **3.12+** on the host, plus **uv** (preferred) or **pipx**.
- A clone of the DevCake repository (the CLI operates on this checkout —
  compose files, bake targets, and scripts live here).

## 2. Install the CLI

From the repository root:

```bash
uv tool install devcake-cli   # from PyPI (or: pipx install devcake-cli)
devcake --help                # verify the console script resolves
```

When operating a checkout day to day, prefer installing from it — the
CLI is versioned with the tree, and PyPI releases can lag it. Three
routes exist, and each has its own refresh rule after a `git pull`:

| Route | Install | After `git pull` |
|---|---|---|
| PyPI tool | `uv tool install devcake-cli` | `uv tool upgrade devcake-cli` (once the release is published) |
| Checkout tool (snapshot) | `uv tool install .` at the repo root | re-run `uv tool install .` |
| Editable venv | `uv venv && uv pip install -e .` (or `uv sync`) | nothing — it imports from the tree |

`uv tool upgrade` only knows tools installed with `uv tool install`; on an
editable venv it reports "not installed", which is not an error.

## 3. Preflight — `devcake doctor`

```bash
devcake doctor           # human-readable
devcake doctor --json    # machine-readable: {ok, checks: [{id, ok, detail}]}
```

Doctor runs named checks (docker socket, docker group, derivable socket gid,
buildx, checkout layout, digest tooling, systemd user session, control-plane
ports, baker liveness). Hard failures exit **3**; soft warnings exit 0 with
remedies in the detail text. Fix what it names — it never fixes things itself.
Two soft warnings are normal and fine: occupied control-plane ports when the
stack is already running, and a missing systemd user session (the baker then
runs under a loud fallback supervisor).

## 4. First bring-up — `devcake up --bake`

```bash
devcake up --bake
```

What it does, in order: seeds `.env` from `.env.example` if missing and
**auto-generates any missing bootstrap passwords** into it (mode 600 — tell
the human their admin login lives in `.env` as `ADMIN_USER` /
`ADMIN_PASSWORD`); discovers the docker socket group id; stops the scheduler,
computes the image digest, bakes the control plane + hello images, and
restarts; brings the compose stack up; waits for the app to report healthy;
verifies the scheduler can write the docker socket; runs a hello dispatch
smoke; installs the host baker under a supervisor (systemd user unit on
Linux, launchd on macOS, a flock respawn loop as the loud fallback).

Useful variants: `devcake up` (day-to-day restart, no bake),
`devcake up --dry-run` (prints the plan, changes nothing),
`devcake up --foreground-baker` (baker takes the terminal),
`devcake up --json` (one JSON receipt on stdout, progress on stderr).

**Exit codes (all verbs, assertable):** `0` success · `2` usage error ·
`3` preflight/doctor failure · `4` compose or bake failure · `5` setup
conflict · `6` baker/supervisor failure · `1` other.

**macOS / Docker Desktop:** read `docs/13-deployment.md` §8b first — socket
gid quirks are the most common first-run failure. If the scheduler cannot
write the socket, `devcake up` prints the exact compose-override fix.

## 5. Configure — admin UI or `devcake setup`

The human path: open **http://localhost:8080** (basic auth from `.env`),
then Connections (PMO + Repositories), Fleet → Dev Types (harness + model
credentials, with connection tests), Settings. Saving Dev Types triggers the
baker's second bake; the first mission waits until it finishes.

The agent path — configure a **reachable** control plane non-interactively
(run `devcake up --bake` first; `setup` never starts or stops the stack):

```bash
# Create the first Dev roster (refuses if Devs already exist — exit 5):
devcake setup --same-harness claude-code --json

# Wire a PMO and a repository; secrets via env/file/stdin, never argv:
LINEAR_KEY=... devcake setup \
  --pmo-name main --pmo-system linear --pmo-team-key ENG \
  --pmo-api-key-env LINEAR_KEY \
  --repo-name product --repo-forge github \
  --repo-url https://github.com/org/product \
  --repo-token-file /path/to/token --json

# Restore a previously exported DevCake settings bundle:
devcake setup --import bundle.yaml --import-passphrase-stdin --json
```

The `--json` receipt reports what happened without ever echoing secret
values (`roles_created`, `connections`, `secrets_received` as booleans and
counts, `bundle_import`, a doctor snapshot, `next_steps`). The clean-host
chain an agent can run end to end is:

```bash
devcake up --bake && devcake setup … --json
```

Exit 5 on setup means Dev Types already exist — that is a refusal to
overwrite, not an error to force past. Talk to the human.

## 6. Day-to-day

```bash
devcake status            # compose snapshot + baker liveness (--json ok)
devcake up                # safe restart; refreshes baker supervision
devcake down              # stops the stack; never touches volumes
docker compose logs --tail=100 app     # or dagu, admin, openobserve, gitea
```

- Admin UI: http://localhost:8080. Dagu UI: 8525 · OpenObserve: 5080 ·
  bundled Gitea: 3300 — all loopback.
- API for scripting: same origin as the admin UI, basic auth from `.env`,
  and every mutating request (POST/PUT/PATCH/DELETE) must carry the header
  `X-DevCake-Request: 1`. Health: `GET /api/v1/health` (includes active run
  count and baker heartbeat).
- Right after a container swap the baker heartbeat can read false for under
  a minute while its probe backs off — re-check before treating it as down.

## 7. Upgrades

```bash
git pull
uv tool install .         # checkout tool install only — editable venv: nothing; PyPI: uv tool upgrade devcake-cli
devcake status            # pick a quiet moment (no active runs)
devcake up --bake         # rebake + restart; receipts stay honest
```

Always bake through `devcake up --bake` (or `docker buildx bake` wrapped
with the digest script it uses) — a bare bake of agent images mints a
sentinel digest and staffing fails closed on purpose.

## 8. Protect the default branch (required, not optional)

Devs hold a write-capable forge token, and token scopes cannot separate
"push a branch" from "merge to main" — **forge branch protection is the
enforcement layer** (`docs/13-deployment.md` §8a). The Repositories page can
apply a derived baseline per repo or in bulk ("Apply protection"): PR
required, no force-push, the repo's own discovered CI checks, and one
required approval only when a distinct reviewer token is stored. It never
weakens existing rules; a 403 means the write token lacks admin on that
repo. The Overview page shows a critical alert while a work repo's default
branch is unprotected.

## 9. Troubleshooting quick reference

| Symptom | Do this |
|---|---|
| `devcake` not on PATH after install | `uv tool update-shell` or ensure `~/.local/bin` is on PATH; re-open the shell |
| doctor: socket missing / gid underivable | Start Docker (Desktop: wait for the engine); or set `DOCKER_SOCK` |
| doctor: buildah shim detected | Install Docker Engine + Buildx — podman cannot bake these images |
| up: scheduler cannot write the socket | Follow the printed compose-override gid fix, then `devcake up` again |
| up: app never reports live | `docker compose logs --tail=50 app`; a weak OpenObserve root password crash-loops boot |
| baker DEGRADED warnings | Linux headless: `loginctl enable-linger <user>` (human runs it), re-login, `devcake up` |
| exit 5 from setup | Roster exists — configure through the admin UI instead of forcing |
| ports 8080/8525/5080/3300 in use | Existing stack (fine — `devcake status`) or a foreign listener to clear |

## 10. Where to read more

- `docs/13-deployment.md` — full runbook, macOS section, upgrade rituals
- `docs/14-security.md` — the security contract; §9 is the checklist to
  complete before the first real mission
- `docs/18-operator-contract.md` — everything the human owns, on one page
- `docs/tutorials/01-first-mission.md` and `02-operating-devcake.md` — the
  board-side workflow once the stack is up

For working on DevCake's own codebase (a different job from operating it),
read `AGENTS.md` instead.
