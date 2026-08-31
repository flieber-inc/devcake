# ADR-0038 — The DevCake CLI: scope, command surface, and agent-operability contract

- **Status:** accepted (2026-08-30) — founder ratification on CAKE-175
  amended the draft (env auto-init default, settings-bundle import slice,
  verb orthogonality) and flipped this ADR to accepted; sibling
  implementation issues may proceed once they cite this ADR
- **Context:** Host bring-up today is `./up.sh` plus `python -m dev_factory`
  under systemd / launchd / flock respawn (`docs/13-deployment.md`). There is
  no first-class `down` / `status` / `doctor` / `setup` verb, no installable
  userspace package, and no agent-operable receipt surface. Agents and
  operators need one Python-first CLI that reuses the existing bake/receipt
  and validation chokepoints instead of forking them. This ADR seals the
  design **before** any implementation.

## Founder-sealed inputs (givens — not re-litigated here)

1. **Python-first** — reuse `scripts/dev_factory` and app-side validation;
   a rewrite that forks bake/receipt logic is forbidden.
2. **Userspace install** — uv tool / pipx (or equivalent user install); not a
   system daemon installer and not a privileged package manager path for v1.
3. **Never sudo** — steady-state needs none. One-time host prep (Linux docker
   group membership; possibly `loginctl enable-linger` for systemd `--user`)
   is **detected and PRINTED** by `doctor` / `setup`, never executed by the
   CLI.
4. **`.env` auto-init is the default** (ratification 2026-08-30) — when
   required bootstrap values are missing, bring-up generates strong secrets
   into a mode-600 `.env` with no opt-in flag (Decision 1 / `up`).
5. **Settings-bundle import is a first-class `setup` slice** (ratification
   2026-08-30) — restore a previously exported DevCake settings bundle via
   the existing ADR-0013 path (Decision 1 / `setup`).
6. **Verb orthogonality** (ratification 2026-08-30) — `setup` does not
   orchestrate the stack; the clean-host story is
   `devcake up --bake && devcake setup … --json` (Decision 8).

## Decision

### 1 — Command surface v1

Exact verb set (no others in v1):

| Verb | Role |
|---|---|
| `devcake up` | Absorb today's `./up.sh` bring-up |
| `devcake down` | Stop the compose stack |
| `devcake status` | Readiness snapshot |
| `devcake doctor` | Preflight checks only |
| `devcake bake` | Image bake (no stack start) |
| `devcake baker run` | Host baker foreground / supervisor entry |
| `devcake setup` | Operator/agent bootstrap |

Universal flags on **every** verb: `--help`, `--json` (see Decision 2).
No TTY prompts on any verb — flags, env, and stdin only.

#### `devcake up`

Mirrors current `./up.sh` flag surface:

| Flag | Semantics |
|---|---|
| `--bake [targets…]` | Bake before up. Targets use today's Bake vocabulary (`all`, `app`, `admin`, `images`, `app-test`, `hello`, per-harness names, …). Bare `--bake` keeps today's default (control plane + hello). |
| `--dry-run` | Print discovered `DOCKER_GID` / planned actions; do not mutate. |
| `--foreground-baker` | After up, run the baker in this terminal (no supervisor). |
| `--no-hello-smoke` | With `--bake`, skip the hello dispatch smoke. |
| `[service…]` or `-- <service…>` | Optional compose service names passed through to `compose up` (same as today). |

`DOCKER_GID` / `DEVCAKE_WS_HOST` / `DEVCAKE_TAG` discovery + `.env` upsert
stay inside this verb — same chokepoint policy as `scripts/lib/stack_env.sh`
(ADR-0034). Deploy lockstep (`stop dagu` before multi-minute bake, pin
export for bake **and** compose) stays inside this verb.

**`.env` bootstrap auto-init (default, not an option):** when required
bootstrap values are missing (usernames/passwords such as `ADMIN_USER` /
`ADMIN_PASSWORD`, internal-forge `GITEA_ADMIN_*`, Dagu / Redis / OpenObserve
ingest credentials, and any other field `.env.example` marks required for
boot), `devcake up` **auto-generates** strong values into a **mode-600**
`.env` by default — **no flag required and no flag to disable**. Precedence:

1. Explicitly provided values always win (process env, existing `.env`
   entries, or documented file/flag inputs that feed the same upsert path).
2. Only **missing** required keys are generated.
3. Generated material is **never echoed** (stdout, stderr, or `--json`
   receipts). Receipts / human summaries report **presence only** (e.g.
   `env_generated: ["ADMIN_PASSWORD", …]` as key names, never values).
4. Creating `.env` from `.env.example` (when no `.env` exists) remains the
   seed step; auto-init then fills empty required secrets before compose.

This is a deliberate upgrade over today's `./up.sh`, which copies
`.env.example` and leaves passwords empty for the operator to fill. The CLI
must not require a human to invent bootstrap secrets on a virgin host.

#### `devcake down`

| Flag | Semantics |
|---|---|
| *(default)* | `docker compose down` **without** wiping volumes. |
| *(none in v1)* | **No** volume-wipe flag in v1. Destructive volume removal stays a documented manual `docker compose down -v` (or later ADR), never a casual CLI default. |

#### `devcake status`

Reports compose project state plus baker liveness signals the admin already
cares about (`baker_alive`, control-plane health). Human text by default;
`--json` for agents.

#### `devcake doctor`

Checks only: docker sock / gid visibility, group membership, linger state
(where applicable), bake tooling presence (`docker buildx`), checkout layout
(compose / bake files / `scripts/dev_factory`). Prints remediation commands
for one-time host prep. **Never** runs `sudo`, `usermod`, or
`loginctl enable-linger`. Exit non-zero when steady-state would fail (see
exit-code table).

#### `devcake bake`

Forwards to `docker buildx bake` with the same target vocabulary as
`docker-bake.hcl` / today's `--bake` targets. Does **not** start the stack
or the baker.

#### `devcake baker run`

Console replacement for `python -m dev_factory`. Same
`PYTHONPATH` / `DEVCAKE_FACTORY_*` semantics as today's host baker entry
(`scripts/dev_factory` → `watch.main`). Supervisor `ExecStart` / runner
scripts target this verb once the CLI ships (Decision 5).

#### `devcake setup`

Bootstrap slices an agent or operator can assert on. **`setup` does not
start, stop, bake, or wait on the compose stack** (Decision 8) — the control
plane must already be reachable (typically after `devcake up --bake`). Seal:

**(a) Dev Type first-setup** — creates `executor` / `judge` / `steward` on an
empty roster via the same shape as `POST /api/v1/dev-types/first-setup`
(`roles.{judge,executor,steward}.{harness_template,model}`), mirroring
`FirstSetupDialog.jsx`. Flags:

| Flag | Semantics |
|---|---|
| `--role-harness <role>=<template>` | Per-role harness (repeatable; roles ∈ `judge`,`executor`,`steward`). |
| `--role-model <role>=<model>` | Per-role model (repeatable; empty string = registry/CLI default). |
| `--same-harness <template>` | Same-for-all shorthand (mirrors Apply/Prefill all). |
| `--same-model <model>` | Model applied to all three roles with `--same-harness`. |

`--same-harness` and per-role flags must not disagree for the same role
(usage error). First-setup is **create-once**: non-empty roster → conflict
exit (mirrors HTTP 409); no silent overwrite of existing Dev Types.

**(b) PMO + repo connection wiring** — flags/subcommands that upsert
connection cards through the existing app validation paths (not a second
schema). Exact connection field flags are implementation detail as long as
they cover name/kind/endpoint (or equivalent) without putting secrets on
argv. Skill-source wiring may ride the same connections slice or a later
additive flag set; v1 must cover PMO + repo at minimum.

**(c) Secrets** — via **env / file / stdin only — never argv**. Examples of
sealed patterns (names illustrative): `--pmo-api-key-env`,
`--pmo-api-key-file`, `--pmo-api-key-stdin`; repo token equivalents. Align
with ADR-0011 never-echo: receipts report presence/counts, never values.

**(d) Settings-bundle import (first-class slice)** — restore a previously
exported DevCake settings bundle. A thin deploy CLI that cannot restore an
export is incomplete; this is not optional sugar.

| Flag | Semantics |
|---|---|
| `--import <bundle.yaml>` | Import path to a `kind: devcake-settings-bundle` file. |
| `--import-passphrase-env <VAR>` | Passphrase for an encrypted envelope (B/C) from env. |
| `--import-passphrase-file <path>` | Passphrase from file. |
| `--import-passphrase-stdin` | Passphrase from stdin. |

Rules:

- Rides the **existing ADR-0013** serializer / `unprotect_bundle` /
  import→profile→apply path and validation — **never a second schema**.
- Encrypted bundles follow the existing export contract (scrypt + AESGCM
  envelope for sections B/C; section A plaintext YAML).
- Secrets handled per never-echo: passphrase and secret values never appear
  on argv, in logs, or in receipts.
- Host-side `setup_env` (section C) from a bundle is applied to the checkout
  `.env` (mode 600) when the import includes C; compose restart remains the
  operator's follow-up (`devcake up` / Decision 8) — `setup` does not
  restart the stack itself.
- The receipt reports **what the bundle applied** (sections present, profile
  name if landed, counts / key names) — never values.

**(e) Idempotent re-run rules**

| Slice | Re-run rule |
|---|---|
| Dev Type first-setup | Create-once; conflict if roster non-empty. |
| Connections | Upsert by instance name (safe re-run). |
| Secrets | Upsert / replace by key id (safe re-run; never echo). |
| Settings-bundle import | Re-apply through ADR-0013 apply semantics (replace-the-world for carried sections; runs-active guard honored). |
| Doctor subset inside setup | Always re-checks; prints `next_steps` only. |

### 2 — Agent-operability contract

- **No TTY prompts ever.** Missing required input → usage error (exit 2),
  never an interactive prompt.
- **Idempotent re-runs** — per Decision 1 tables: `up` / `down` / `status` /
  `doctor` / `bake` are safe to re-run; `baker run` is a long-lived process;
  `setup` follows the create-once vs upsert split above.
- **Meaningful exit codes** (sealed integers agents may assert on):

| Code | Class |
|---:|---|
| `0` | Success (including successful no-op where idempotent). |
| `2` | Usage error (bad flags, mutually exclusive options, missing required input). |
| `3` | Preflight / doctor failure (steady-state would not work). |
| `4` | Compose or bake failure. |
| `5` | Setup conflict (e.g. first-setup on non-empty roster). |
| `6` | Supervisor / baker failure (start, displace, or liveness hard-fail). |
| `1` | Other unexpected failure (last resort; prefer a specific class). |

- **`--json` on every verb.** When `--json` is set: one JSON object on
  **stdout**; diagnostics / progress on **stderr**. When `--json` is absent:
  human-readable summary on **stdout**; diagnostics on **stderr**. JSON
  field names are stable once shipped; additive fields allowed, renames
  require a new ADR.
- **Setup JSON receipt schema** (`devcake setup --json`) — fields an agent
  can assert on (values never include secret material):

```json
{
  "ok": true,
  "schema_version": 1,
  "roles_created": ["executor", "judge", "steward"],
  "roles": {
    "judge": {"harness_template": "claude-code", "model": "", "created": true},
    "executor": {"harness_template": "claude-code", "model": "", "created": true},
    "steward": {"harness_template": "claude-code", "model": "", "created": true}
  },
  "connections": {
    "pmo": [{"name": "…", "configured": true, "tested": false}],
    "repos": [{"name": "…", "configured": true, "tested": false}]
  },
  "secrets_received": {
    "pmo_api_key": true,
    "repo_token_count": 1,
    "harness_key_count": 0
  },
  "bundle_import": {
    "applied": true,
    "path": "…",
    "sections": ["config", "secrets", "setup_env"],
    "profile": "imported-…",
    "setup_env_keys": ["ADMIN_PASSWORD", "GITEA_ADMIN_PASSWORD"],
    "secret_key_counts": {"connections": 2, "harness": 1}
  },
  "doctor": {
    "ok": false,
    "checks": [{"id": "docker_group", "ok": false, "detail": "…"}]
  },
  "next_steps": [
    "add this user to the docker group (printed only; CLI will not run it)",
    "loginctl enable-linger <user>  # Linux systemd --user hosts",
    "devcake up   # if bundle setup_env changed host .env and compose must reload"
  ]
}
```

`secrets_received` and `bundle_import.setup_env_keys` /
`bundle_import.secret_key_counts` carry booleans / counts / **key names
only** (ADR-0011) — never values or fingerprints. Omitted slices the
operator did not request are absent or empty arrays — never fabricated
success. Partial failure sets `ok: false` and a non-zero exit from the table
above.

`devcake up --json` similarly reports env auto-init presence without values,
e.g. `"env_generated": ["ADMIN_PASSWORD", "REDIS_PASSWORD"]` (key names
only; empty array when nothing was generated).

### 3 — Where it lives

- In-repo **Python package** with a **console script** entry point
  (`devcake = …:main`).
- **Placement (sealed intent):** top-level `cli/` directory, import package
  name `devcake_cli` (avoids shadowing the existing `app/devcake` package
  when both appear on `PYTHONPATH` during checkout development). Distribution
  / project name for uv/pipx: `devcake-cli`. A root `pyproject.toml` lands in
  a later implementation issue — this ADR records the intent, not the file.
- The CLI package is **core**: it may call into `scripts/dev_factory` and
  app validation modules; it must **not** contain vendor/connector adapter
  code (same segregation rule as domain).
- Reuse is mandatory: bake/receipt logic stays single-sited in
  `dev_factory`; setup validation goes through existing app seams (e.g.
  first-setup / config validators), not a parallel copy.

### 4 — How `up.sh` dies

**Thin shim for a transition window**, then removal in a follow-up issue
once docs and muscle memory move.

- Rejected: silent dual-maintained logic (a second copy of bring-up that can
  drift from `devcake up`).
- Rejected as the default cutover: wholesale delete-on-day-one without a
  shim (allowed only as an explicit later cutover commit that updates every
  doc reference in the same change).
- During the window, `up.sh` becomes a few dozen lines that `exec`s
  `devcake up "$@"` when `devcake` is on `PATH`, or prints a loud “install
  the CLI (uv/pipx)” message and exits non-zero when it is missing.

**Cutover executed (2026-08-31, founder call):** the shim is removed in the
same change that updates every doc reference, per the clause above. `devcake
up` is the only bring-up entry; a test pins that `up.sh` does not return.

**Deploy ritual successor:** `devcake up --bake` (same
`docker compose stop dagu` → pull → bake/up lockstep story from
`docs/13-deployment.md`). `./up.sh --bake` remains valid while the shim
exists.

### 5 — Supervisor migration

- When `devcake baker run` ships, refresh
  `scripts/systemd/devcake-baker.service`,
  `scripts/launchd/com.devcake.baker.plist` / runner, and the flock respawn
  path so `ExecStart` / runner scripts invoke `devcake baker run` (or an
  absolute path resolved at install/refresh time) instead of
  `@PYTHON@ -m dev_factory`.
- **In-place upgrade:** install/upgrade the userspace CLI → re-run
  `devcake up` (which already displaces leftover `python -m dev_factory`
  bakers and rewrites supervisor units via today's `baker_host.sh`
  behavior). No manual unit editing required.
- **Deprecated alias:** `python -m dev_factory` remains import-compatible
  during the transition window as a deprecated entry (rollback / half-upgraded
  hosts). Removal of the module entry point is a follow-up after the shim
  window closes — not day-one of the CLI cutover.

### 6 — Packaging path

- **v1:** `uv tool install` / `pipx install` from the in-repo package
  (git URL or local path). Userspace only.
- **Phase-2 deferred (named, not built):** Homebrew tap, AUR, and deb —
  wrappers only over the same package. Explicitly out of v1.

### 7 — Explicitly out of scope for v1

- Embedding the scheduler (dagu stays).
- A lite compose profile.
- Any container-runtime replacement.
- GUI replacement — admin SPA first-setup remains valid and authoritative for
  the same roster seed.
- brew / AUR / deb packaging (phase 2).
- Implementing any verb in this ADR's landing PR (docs seal only).
- Having `setup` orchestrate bake/compose/up (rejected — Decision 8).

### 8 — Verb orthogonality (clean-host story)

`setup` configures an already-reachable control plane. It does **not**
bake images, run `compose up`, start the baker, or block on health beyond
calling the live API. The sealed clean-host chain is:

```bash
devcake up --bake && devcake setup … --json
```

Agents and runbooks compose verbs; they do not get a hidden mega-command.
Sibling acceptance criteria that assumed `setup` would bring the stack up
are wrong — follow this chain instead (CAKE-178 ratification note).

## Consequences

- Founder ratification is recorded; sibling CLI implementation issues may
  start **once they cite this ADR**. Design is the license to implement.
- `./up.sh` and `python -m dev_factory` remain the live operators until an
  implementation PR lands the package + shim + supervisor refresh.
- Agents gain a stable exit-code and setup-receipt contract to assert on;
  operators gain `doctor` / `setup` without privilege escalation, default
  `.env` auto-init on `up`, and ADR-0013 bundle restore on `setup --import`.
- Still forbidden: rewriting bake/receipt in the CLI, putting secrets on
  argv, auto-running sudo/linger, volume-wipe defaults, expanding the v1
  verb set, or teaching `setup` to orchestrate the stack.

## Related

- CAKE-175 (this ADR)
- Sibling implementation issues in the same Linear project (must reference
  this ADR before fleet feed)
- ADR-0011 (never-echo secrets)
- ADR-0013 (settings bundle / setup_env — `setup --import` reuses this path;
  never a second schema)
- ADR-0025 / ADR-0034 (deploy lockstep + stack-env chokepoint)
- `docs/13-deployment.md` (current `./up.sh` / baker runbook)
- `docs/14-security.md` (product security contract — CLI must not claim a
  stronger posture)
- `admin/spa/src/components/FirstSetupDialog.jsx` +
  `POST /api/v1/dev-types/first-setup` (same-for-all roster seed to mirror)
