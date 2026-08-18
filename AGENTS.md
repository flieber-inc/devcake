# Agent notes — DevCake

Instructions for coding agents (Claude, Cursor, Grok, Codex, etc.) working in this repo.
These rules are **mandatory** unless the user explicitly overrides them for a task.

## Security / product claims

The **product security contract** is [`docs/14-security.md`](docs/14-security.md).
Do not write docs, README, or PR copy that claims a stronger posture (multi-tenant
sandbox, secrets never leave the host under injection, hard-gated branch
protection) than that file. Do not treat staffing a different Dev Type for
REVIEW as a security control — the security-relevant second identity is the
**reviewer token** (app-only); REVIEW is always a pipeline stage. Design choices
(dedicated host, adult-operator prompt trust, warnings vs gates) are intentional.

## Admin SPA design system (mandatory)

Any change under `admin/spa/` that affects look, feel, copy, or interaction
**must follow [`admin/spa/DESIGN.md`](admin/spa/DESIGN.md)** — the decided
design guideline (identity, tokens, layout idioms, action hierarchy, dialogs,
copy voice, evidence loop). Read it before touching the SPA. Iron rules:

- Colors come from the `@theme` tokens in `admin/spa/src/index.css` — never raw
  hex or new color families in components. `accent-*` is the only brand accent.
- Scalar settings are `SettingRow`s; record lists are real tables styled like
  the Runs table; config renders one section per `#/config/<section>` view
  (the `limits` view carries the merged Limits + Traffic cards); Repositories
  and PMO are draft-editing pages under the sidebar's Adapters item, not
  Config sections.
- One primary action per header/card; secondary/rare/destructive actions go in
  a `MoreMenu` (⋯) with honest one-line consequence descriptions — but never a
  one-item menu when it's the element's only action.
- Native `window.confirm/prompt/alert` are banned — use `Modal.jsx` dialogs.
- Draft semantics are untouchable: config edits ride `useConfigDraft`; anything
  immediate is wrapped in `InstantZone`.

## Always Works™ (mandatory before done)

**"Should work" ≠ "does work."** Before marking any change complete, prove it with evidence you personally observed — not assumptions.

| Change type | Minimum proof |
|---|---|
| Docker / Bake / Compose | `docker buildx bake …` succeeds **and** `docker compose up -d` + healthchecks pass |
| App / API | **`docker buildx bake app-test`** (or `scripts/pytest_app.sh`) **then** pytest in that image, **or** `PYTHONPATH=app` on Python **3.12** against the working tree; **and** bake/restart prod `app` when the run path changed |
| Admin SPA | `bake admin` **and** load UI / nginx-health |
| Dev harness / entrypoint | `bake images` (or affected target) **and** smoke CLI + import entrypoint |
| Docs-only | No runtime required; still re-read for accuracy |

**Stale `app-test` trap:** the `devcake/app-test` image **COPY**s `app/devcake` and `app/tests` at bake time. Re-running pytest on an old `devcake/app-test:latest` grades the last bake, not your working tree — a silent false green. Always rebake after `app/` edits, or use `PYTHONPATH=app` on 3.12, or `./scripts/pytest_app.sh` (always bakes first). CI rebakes on every run; local agent loops often forget.

Never claim done from "build succeeded" alone when the user-facing path is run/up. Name anything still unproven.

## Engineering standards (mandatory for new work)

### TDD for new implementations

**New behavior is test-first.** Do not implement production code for a new feature, bug fix with a known reproduction, or new module until a failing test exists.

| Rule | Detail |
|---|---|
| **Red → green → (then ship)** | Write a failing test that names the behavior; write the minimum code to pass; only then refactor. |
| **Vertical slices** | One seam / one behavior at a time. Do **not** bulk-write a suite of imagined tests then implement everything. |
| **Agree the seam first** | Before the first test: state the public interface under test (function, port Protocol, HTTP path). Tests hit that seam only. |
| **No private tests** | Do not assert on private helpers, call counts of internal collaborators, or implementation structure. Prefer fakes at **port** seams (`ports/*`). |
| **Independent expected values** | Assertions use known literals / domain rules — not recomputing the same algorithm as production. |
| **Where tests live** | `app/tests/test_*.py`. Run with Python **3.12** (prod image). Prefer `./scripts/pytest_app.sh` (rebakes `app-test` then pytest), or `PYTHONPATH=app` locally on 3.12. |
| **When TDD does not apply** | Pure renames, docs-only, config/copy, or mechanical follow-the-existing-pattern refactors with no behavior change — still run existing tests (Always Works™). |

### SOLID (always)

Design and refactors must respect SOLID. Prefer **deep modules** (small interface, large behavior) over shallow pass-through wrappers. Domain vocabulary: see `docs/01-architecture.md` and `docs/02-domain-model.md`. Architecture terms for structure: **module, interface, implementation, depth, seam, adapter, leverage, locality** (not “service/API/boundary” as design words).

| Principle | In this codebase |
|---|---|
| **S** — Single responsibility | One reason to change per module. Dispatch spine → `RunBootstrap`; mission transitions → orchestrator; vendor HTTP → adapters. |
| **O** — Open/closed | Extend via new adapters / new callers of a deep module; do not fork copy-paste spines. |
| **L** — Liskov | Port adapters are substitutable (prod + test fakes). Fakes must honor the Protocol contract. |
| **I** — Interface segregation | Prefer focused Protocols (`ports/*`) over god objects callers only use 10% of. |
| **D** — Dependency inversion | Domain depends on **ports**, not `adapters/*`. Composition root: `api/services.build_services()` (ADR-0028; `api/main.py` is wiring + ≤4-statement route forwards). Inject dependencies; do not construct infrastructure inside domain logic. The domain→adapters ban is now test-enforced (`test_structure_guards.test_domain_never_imports_adapters`, ADR-0034), with two allowlisted seams. |

**Deletion test:** if deleting a module only moves lines around (no complexity concentrates), it was shallow — do not add more of those.

### Python best practices

| Area | Expectation |
|---|---|
| **Version** | Target **Python 3.12** (matches `app/Dockerfile`). Avoid 3.13+–only syntax unless the image moves. |
| **Style** | Match surrounding code: existing imports, naming, logging, async patterns. Prefer `from __future__ import annotations` where the file already uses it. |
| **Types** | Annotate public functions and Protocol methods. Use `Protocol` for seams (`ports/`). Prefer `X \| None` over `Optional[X]` in new code when consistent with the file. |
| **Async** | Domain I/O is async. Tests: use a dedicated event loop helper (`asyncio.new_event_loop().run_until_complete`) — do not rely on deprecated implicit loops. |
| **Errors** | Raise domain/port exceptions (`PMOTransient`, `ForgeError`, …); do not leak `httpx`/`redis` types upward. |
| **Secrets** | Never log or persist credentials. Redact via `security.redact` at PMO/forge egress. No secrets in tests beyond obvious fakes. |
| **Pydantic** | Config and DTOs stay pydantic models; keep field names aligned with `docs/02-domain-model.md`. |
| **No drive-by** | Do not reformat unrelated files, rename widely without need, or expand scope beyond the task. |
| **Docs drift** | If you change a public seam (ports, dispatch spine, lifecycle), update the matching `docs/*` in the same change (zero-drift with `docs/01`–`04` and ADRs). |

### Quick anti-patterns (reject these)

- Leftover ≠ decided. If a locked decision is unimplemented, implement it or refuse the PR. Do not comment the gap.
- Implementing a feature then “adding tests later”
- Typing domain code against concrete `DaguExecutor` / `Messaging` / `RunStore` instead of ports
- Duplicating the ACL → digest → save → start spine outside `RunBootstrap`
- Giant untested functions in `orchestrator.py` without a public-seam test
- `except Exception: pass` that swallows real failures without logging — lint-enforced: ruff `BLE001`; every blanket catch is narrowed or carries `# noqa: BLE001 — <justification>` naming its contract (docs/15 §7)
- Mutating production modules only “to make the test pass” by weakening invariants
- New endpoint bodies in `api/main.py` or new attributes bound onto `MissionManager` after its class body — main.py is wiring + ≤4-statement route forwards (composition happens in `api/services.build_services()`, ADR-0028), orchestrator behavior lives in module functions taking `mgr` (ADR-0015; enforced by `tests/test_structure_guards.py`)
- New checkpoint-step key literals — every `finalized_steps`/`_checkpoint` key registers in `domain/orchestrator/steps.py` (ADR-0034; the AST guard in `test_structure_guards` rejects bare literals). Same file forbids a domain module importing `adapters/*` outside the two allowlisted seams.

## Docker images: Bake only

**DevCake images are built only with Docker Bake.** Compose never builds them.

| Command | What it builds |
|---|---|
| `docker buildx bake all` | Everything — **use this on first setup and full upgrades** |
| `docker buildx bake` | Control plane only: `app` + `admin` (prod — **no** pytest) |
| `docker buildx bake app-test` | App + pytest + `tests/` for CI (`devcake/app-test`) |
| `docker buildx bake images` | Dev harnesses + hello stub (shared `base` stage) |
| `docker buildx bake ci` | `app` + `app-test` + `admin` + `hello` (PR loop without full harness matrix) |
| `docker compose up -d` | **Run** the stack (requires images already baked) |

**Build cache:** opt-in — `BAKE_LOCAL_CACHE=1` exports to `.buildx-cache/` (gitignored), but needs a docker-container builder or the containerd image store; the default `docker` driver refuses cache export, so plain `bake` commands stay cache-less and work on a stock Docker Engine. On GitHub Actions:

```bash
docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl all
# or use workflows — they already pass both files to docker/bake-action
```

**GitHub Actions** (`.github/workflows/`):

| Workflow | When | What |
|---|---|---|
| `ci.yml` | every PR + `main` | Bake group `ci` → ruff → Redis + `app-test` pytest → minimal compose (no Gitea) via `scripts/ci_compose_for_dispatch.sh` → `scripts/ci_dispatch_hello.sh` |
| `docker-images.yml` | `images/**` changes + `main` + manual | Bake group `images` → harness CLI smoke + hello redis-import smoke (layer only; full dispatch is `ci.yml`) |
| `docker-publish.yml` | **manual** (`workflow_dispatch`) | Bake `all` + push to GHCR (`ghcr.io/<owner>/devcake/<name>`) |

**Local unit path:** `./scripts/pytest_app.sh` (always rebakes `app-test`, then pytest).  
**Local full suite:** `scripts/ci_suite.sh` — pin gate + bake app-test + pytest + Gitea forge battery + dispatch smoke.  
**Clean-room dispatch compose:** `scripts/ci_compose_for_dispatch.sh` (minimal services; set `CI_COMPOSE_WRITE_ENV=1` only when you intend to overwrite `.env`).

### Do

- Build/rebuild with `docker buildx bake` / `bake all` / `bake images` (see `docker-bake.hcl`).
- After changing `app/`, `admin/`, or `images/`, rebake the affected targets (or `bake all`).
- Keep app/admin tags aligned with compose: `DEVCAKE_TAG` (default `latest`) in bake and compose.
- For **new behavior**: red→green TDD first (see **Engineering standards**), then bake/prove Always Works™.

### Do not

- Do **not** add `build:` back to `docker-compose.yml` for DevCake images.
- Do **not** use `docker compose build` or `docker compose --profile images build` — those paths are gone.
- Do **not** expect `docker compose up --build` to build `devcake/*` images (`pull_policy: never`).
- Do **not** invent per-harness Dockerfiles under `images/<harness>/` — use multi-target `images/Dockerfile` + a target in `docker-bake.hcl`.

### Layout

| Path | Role |
|---|---|
| `docker-bake.hcl` | **Single source of truth** for image builds and tags |
| `docker-compose.yml` | Runtime only (networks, volumes, third-party images) |
| `app/Dockerfile` | Multi-stage FastAPI app → tag `devcake/app:${TAG}` |
| `admin/Dockerfile` | Multi-stage SPA + nginx → tag `devcake/admin:${TAG}` |
| `images/Dockerfile` | Multi-target Dev harnesses (`base`, `hello`, `claude-code`, `codex`, `grok-build`, `pi`, `opencode`, `qwen-code`) |
| `app/devcake/harness.py` | Runtime image names for Dagu dispatch (`devcake/dev-*:latest`) |

### Typical agent workflows

```bash
# After editing orchestrator / API
docker buildx bake app && docker compose up -d app

# After editing admin SPA
docker buildx bake admin && docker compose up -d admin

# After editing images/common/dev_entrypoint.py or a harness
docker buildx bake images

# Full lockstep (app + admin + all Dev images) — required when protocol/entrypoint changes
docker buildx bake all && docker compose up -d

# ADR-0025: dev-run.yaml + compose + .env (DEVCAKE_WS_HOST) also deploy in
# lockstep. dagu/dags is a LIVE :ro bind, so a new DAG goes live at `git pull`
# before ./up.sh — stop dagu first: docker compose stop dagu && ./up.sh --bake
```

Optional tag pin (bake and compose must match):

```bash
export DEVCAKE_TAG=$(git rev-parse --short HEAD)
./up.sh --bake all            # upserts pin into .env + bake + compose
# or without up.sh:
docker buildx bake all
docker compose up -d          # needs export still set, or DEVCAKE_TAG in .env
```

`./up.sh` resolves `DEVCAKE_TAG` once (process env > `.env` > `latest`),
exports it for bake + compose, and **upserts it into `.env`** so a later plain
`docker compose up -d` stays lockstep. Compose passes the pin into the app
container, and **dispatch derives the harness image tags from it**
(`app/devcake/harness.py`). An app container recreated with neither export nor
`.env` pin falls back to `:latest` for app, admin, *and* dispatched harnesses.

### Third-party images

Redis, Dagu, OpenObserve, Fluent Bit still come from registries via compose (`docker compose pull`). Only **DevCake-built** images go through bake.

### More detail

- Deployment runbook and matrix: `docs/13-deployment.md` §6
- Harness templates: `docs/08-harness-templates.md`
- Human quickstart: `README.md`
