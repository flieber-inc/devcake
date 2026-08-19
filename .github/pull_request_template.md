<!-- One intent per PR. Restates CONTRIBUTING.md — do not invent policy here. -->

## Intent

<!-- What this PR does and why (one or two sentences). -->

## Proof (Always Works™)

Name the proof for the change class you touched. “Build succeeded” alone is not enough for a run/up path.

- [ ] Docs / templates only — re-read for accuracy
- [ ] `app/` — `./scripts/pytest_app.sh` (or `PYTHONPATH=app` on Python 3.12)
- [ ] `admin/` — `docker buildx bake admin` and load UI / nginx-health
- [ ] `images/` / entrypoint — `docker buildx bake images` (or affected target) and smoke
- [ ] Bake / Compose — `docker buildx bake …` **and** `docker compose up -d` + healthchecks

## Checklist

- [ ] **One intent** — small, reviewable; unrelated work left out
- [ ] **Security claims** do not exceed [`docs/14-security.md`](https://github.com/flieber-inc/devcake/blob/main/docs/14-security.md). The **reviewer token** (app-only) is the security-relevant second forge identity; REVIEW is always a pipeline stage
- [ ] **Docs drift** — public-seam / lifecycle changes update matching `docs/*` in this PR
- [ ] **No secrets or local state** — no `.env`, `/data/`, `/workspaces/`, backup tarballs, or real credentials; test doubles stay obvious fakes
- [ ] **Bake only** when images change — `docker buildx bake …`, never `docker compose build` for `devcake/*`

## Links

- Contributing: [`CONTRIBUTING.md`](https://github.com/flieber-inc/devcake/blob/main/CONTRIBUTING.md)
- Vulnerability reports: [`SECURITY.md`](https://github.com/flieber-inc/devcake/blob/main/SECURITY.md) (private channel — not a public issue with exploit detail)
