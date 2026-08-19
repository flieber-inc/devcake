# Contributing

Thanks for helping improve DevCake. This file is the short path for a first-time
contributor. Product contracts live under [`docs/`](docs/); agent coding rules
live in [`AGENTS.md`](AGENTS.md).

## Prerequisites

- **Docker Engine** with Compose v2 and **Buildx** (images are built with Bake)
- **Python 3.12** if you run unit tests against the working tree without Docker
  (`PYTHONPATH=app`)
- **Node.js** if you change admin SPA helper tests
  (`npm run test:helpers --prefix admin/spa`)

## Build doctrine (Bake only)

DevCake images are **Bake-only**. Compose never builds them.

| Do | Do not |
|---|---|
| `./up.sh --bake` (preferred first-time / lockstep path) | `docker compose build` |
| `docker buildx bake …` / `bake all` / `bake images` / `bake ci` | `docker compose up --build` for `devcake/*` images |
| Rebake after edits under `app/`, `admin/`, or `images/` | Expect compose `build:` keys for DevCake images |

Normative detail: [`AGENTS.md`](AGENTS.md) ·
[`docs/13-deployment.md`](docs/13-deployment.md).

Quickstart (clone the public remote you intend to contribute to, then):

```bash
cp .env.example .env
# Strong ADMIN / REDIS / DAGU / OO / GITEA passwords only.
# Operator PMO/forge/model secrets go in the admin UI after up — not in .env.

./up.sh --bake
# Admin UI: http://localhost:8080  (basic auth from .env)
```

## Tests

| Surface | Command |
|---|---|
| App unit suite (rebakes `app-test` first) | `./scripts/pytest_app.sh` |
| Admin hermetic helpers | `npm run test:helpers --prefix admin/spa` |
| Full local suite (stack + forge battery + dispatch smoke) | `scripts/ci_suite.sh` — requires a healthy compose stack already up |

**Stale image trap:** `devcake/app-test` copies `app/devcake` and `app/tests`
at bake time. Re-running pytest on an old image grades the last bake, not your
tree. Prefer `./scripts/pytest_app.sh` or `PYTHONPATH=app` on Python 3.12.

New behavior is **test-first** at public seams (`app/tests/`, port fakes). See
[`AGENTS.md`](AGENTS.md) (TDD, SOLID, Always Works™).

## Pull requests

- **One intent per PR.** Prefer small, reviewable diffs.
- **Always Works™:** prove the change class you touched (docs re-read; bake +
  health when images/UI/runtime change; pytest when `app/` changes). “Build
  succeeded” alone is not enough for a run/up path.
- **Security claims** must not exceed [`docs/14-security.md`](docs/14-security.md).
  The reviewer token (app-only) is the security-relevant second forge identity;
  REVIEW is always a pipeline stage.
- **Docs drift:** if you change a public seam (ports, dispatch spine, lifecycle),
  update the matching `docs/*` in the same change.
- Do not commit `.env`, `/data/`, `/workspaces/`, backup tarballs, or real
  credentials. Test doubles should stay obvious fakes (`example.com`, padded
  `ghp_…` fixtures, etc.).
- GitHub’s PR template (`.github/pull_request_template.md`) restates this
  checklist.

## Secrets and local state

| Path | Role |
|---|---|
| `.env` | Stack bootstrap only (schema v4) — gitignored; copy from `.env.example` |
| Admin UI secrets | Operator PMO / forge / model credentials under `/data` |
| `/workspaces/` | Per-run host workspaces (ADR-0025) — gitignored |

Never put forge/PMO/model tokens in `.env` for normal ops. Vulnerability
reporting: [`SECURITY.md`](SECURITY.md).

## CI and forks

GitHub Actions for this repository do **not** require custom private org
secrets for pull-request CI.

| Workflow | Secrets a public fork needs | Notes |
|---|---|---|
| `ci.yml` | None beyond default `GITHUB_TOKEN` | Bake group `ci`, ruff, pip-audit, npm audit, pytest, minimal compose dispatch smoke |
| `docker-images.yml` | None | Path-filtered harness image bake + smoke |
| `docker-publish.yml` | Default `GITHUB_TOKEN` with `packages: write` on the **target** repo | **Manual** `workflow_dispatch` only; publishes to GHCR for the repo owner. Forks can publish only to their own GHCR if they run the workflow with package write enabled. No custom org secrets. |

If CI fails on a fork for a missing optional external service, that should be
visible in the workflow logs — PR CI is designed not to depend on private
tokens the maintainer org holds privately.

## Dependency / SBOM honesty

CI runs `pip-audit` (app-test) and `npm audit --omit=dev` (admin SPA). Manual
image publish attaches Bake SBOM metadata. There is **no** committed tree-wide
SBOM or continuous full-repo SBOM pipeline. Details:
[`SECURITY.md`](SECURITY.md).

## Further reading

| Topic | Doc |
|---|---|
| Product overview | [`docs/00-overview.md`](docs/00-overview.md) |
| Operator duties | [`docs/18-operator-contract.md`](docs/18-operator-contract.md) |
| Security contract | [`docs/14-security.md`](docs/14-security.md) |
| Deployment / bake matrix | [`docs/13-deployment.md`](docs/13-deployment.md) |
| Agent coding rules | [`AGENTS.md`](AGENTS.md) |
| Changelog / living log | [`CHANGELOG.md`](CHANGELOG.md) → [`docs/16-roadmap.md`](docs/16-roadmap.md) |
| First mission | [`docs/tutorials/01-first-mission.md`](docs/tutorials/01-first-mission.md) |

### Missions and agent-generated PRs

Some pull requests are opened by DevCake Dev Types (ONBOARD → PLAN → EXECUTE →
REVIEW). Humans remain in the loop for merge when `auto_merge` is off (default).
You do not need to understand the full orchestrator to contribute by hand —
treat agent PRs like any other PR, and keep your own changes independently
proven.
