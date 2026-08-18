#!/usr/bin/env bash
# DevCake model-free local suite — not a full clone of GHA ci.yml.
#
# Preconditions (fail late if missing):
#   - healthy compose stack already up (app + redis + dagu + … + gitea for
#     contracts); use scripts/ci_compose_for_dispatch.sh with
#     CI_COMPOSE_WITH_GITEA=1 for a clean-room bring-up
#   - hello image present as devcake/dev-hello:${DEVCAKE_TAG:-latest}
#   - ADMIN_* / REDIS_PASSWORD in .env (or environment)
#
# Steps: pin gate → app-test rebuild (tree) → ruff → pytest → forge/PMO
# contracts (live app) → dispatch-hello. Mixed-version is warn-not-fail
# (banner only): unit lanes grade the tree; dispatch/contracts grade the
# live stack. Does NOT: npm helper tests, npm audit, pip-audit, rebake
# prod app/admin/hello, or write SBOM.
#
# App-test rebuild: scripts/lib/bake_app_test.sh (Buildx bake preferred;
# Dockerfile --target test fallback on buildah/podman).
# Token-spending acceptance: scripts/acceptance.py (not this script).
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
# Load Redis password (and anything else tests may need) from .env without
# treating the whole file as executable shell. Line-wise export (audit A29):
# `export $(grep … | xargs)` word-split values, so a password containing
# spaces, quotes, or '#' was mangled or aborted the suite.
if [[ -f .env ]]; then
  while IFS= read -r line; do
    export "${line?}"
  done < <(grep -E '^(REDIS_PASSWORD|ADMIN_USER|ADMIN_PASSWORD|DEVCAKE_TAG)=' .env)
fi

: "${REDIS_PASSWORD:?REDIS_PASSWORD must be set (compose .env)}"
: "${ADMIN_USER:?ADMIN_USER must be set (compose .env)}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set (compose .env)}"

# AUD-004 applied to the SUITE (2026-08-12 audit OPS-M3): on a pinned
# install the suite used to bake app-test:latest from the working tree while
# the dispatch battery graded the LIVE, pinned stack — green then proved a
# MIX of two code versions. DEVCAKE_TAG now rides from .env into the bake
# and every docker run below (tag lockstep, like up.sh); and when the live
# app container is not running the freshly-baked image, the run is loudly
# labeled so a mixed-version green can never read as tree evidence.
mixed_version_banner() {
  local live_id local_id
  live_id="$(docker inspect -f '{{.Image}}' devcake-app-1 2>/dev/null || true)"
  local_id="$(docker image inspect -f '{{.Id}}' "devcake/app:${DEVCAKE_TAG:-latest}" 2>/dev/null || true)"
  if [[ -n "$live_id" && -n "$local_id" && "$live_id" != "$local_id" ]]; then
    echo "⚠️  MIXED-VERSION RUN: the live app container is NOT running the"
    echo "    local devcake/app:${DEVCAKE_TAG:-latest} image (stack predates"
    echo "    the last bake). Unit lanes grade the TREE; the dispatch and"
    echo "    contract batteries grade the LIVE STACK. Redeploy (./up.sh)"
    echo "    before reading this green as evidence for the tree."
  fi
}

echo "── digest-pin gate (ISSUES #29)"
python3 scripts/check_image_pins.py

echo "── rebuild app-test (prod image has no pytest; DEVCAKE_TAG=${DEVCAKE_TAG:-latest} lockstep)"
bash scripts/lib/bake_app_test.sh
mixed_version_banner

echo "── ruff (syntax / undefined names / blanket-except policy, docs/15 §7)"
docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff-cache -w /srv \
  "devcake/app-test:${DEVCAKE_TAG:-latest}" ruff check devcake tests

echo "── unit + live-redis tests (INV-1..6 coverage) via app-test image"
# Same Docker network as compose so redis://redis resolves; prod app stays lean.
# Mount images/common for test_entrypoint_render (same path as compose:
# /srv/images/common) plus the structural-test binds — the suite hard-fails
# with "mount missing" when any is absent (same set as pytest_app.sh / ci.yml).
docker run --rm \
  --network devcake_control \
  -e "REDIS_URL=redis://redis:6379/0" \
  -e "REDIS_PASSWORD=${REDIS_PASSWORD}" \
  -v "$(pwd)/images/common:/srv/images/common:ro" \
  -v "$(pwd)/images/hello:/srv/images/hello:ro" \
  -v "$(pwd)/docker-bake.hcl:/srv/docker-bake.hcl:ro" \
  -v "$(pwd)/.github/workflows/docker-publish.yml:/srv/docker-publish.yml:ro" \
  -v "$(pwd)/dagu/dags:/srv/dagu-dags:ro" \
  -v "$(pwd)/app/Dockerfile:/srv/app.Dockerfile:ro" \
  -v "$(pwd)/images/Dockerfile:/srv/images.Dockerfile:ro" \
  -v "$(pwd)/docs:/srv/docs:ro" \
  -v "$(pwd)/scripts:/srv/repo-scripts:ro" \
  -v "$(pwd)/up.sh:/srv/up.sh:ro" \
  -w /srv \
  "devcake/app-test:${DEVCAKE_TAG:-latest}" \
  python -m pytest tests/ -q -o cache_dir=/tmp/pytest-cache

echo "── forge contract battery (gitea lane — bundled instance, no external tokens)"
docker compose exec -T app python - < scripts/contract_tests_forge.py

echo "── PMO contract battery (gitea_issues auto-lane — bundled instance, no external tokens)"
# Default path in contract_tests_pmo.py: GITEA_ADMIN_* → scratch board → 13 scenarios.
# App container already has GITEA_ADMIN_USER/PASSWORD (compose). No Linear required.
docker compose exec -T app python - < scripts/contract_tests_pmo.py

# Full Dagu → hello container → Redis → finalize (also wired into GHA ci.yml)
./scripts/ci_dispatch_hello.sh
mixed_version_banner
echo "── CI suite green"
