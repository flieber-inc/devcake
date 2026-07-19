#!/usr/bin/env bash
# DevCake CI suite (docs/16 M7): deterministic, model-free, ~1 minute.
# Requires the compose stack running + Bake images present.
# Real-model acceptance is scripts/acceptance.py.
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
  done < <(grep -E '^(REDIS_PASSWORD|ADMIN_USER|ADMIN_PASSWORD)=' .env)
fi

: "${REDIS_PASSWORD:?REDIS_PASSWORD must be set (compose .env)}"
: "${ADMIN_USER:?ADMIN_USER must be set (compose .env)}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set (compose .env)}"

echo "── digest-pin gate (ISSUES #29)"
python3 scripts/check_image_pins.py

echo "── bake app-test (prod image has no pytest)"
docker buildx bake -f docker-bake.hcl app-test

echo "── ruff (syntax / undefined names / blanket-except policy, docs/15 §7)"
docker run --rm -e RUFF_CACHE_DIR=/tmp/ruff-cache -w /srv \
  "devcake/app-test:${DEVCAKE_TAG:-latest}" ruff check devcake tests

echo "── unit + live-redis tests (INV-1..6 coverage) via app-test image"
# Same Docker network as compose so redis://redis resolves; prod app stays lean.
# Mount images/common for test_entrypoint_render (same path as compose: /srv/images/common).
docker run --rm \
  --network devcake_control \
  -e "REDIS_URL=redis://redis:6379/0" \
  -e "REDIS_PASSWORD=${REDIS_PASSWORD}" \
  -v "$(pwd)/images/common:/srv/images/common:ro" \
  -w /srv \
  "devcake/app-test:${DEVCAKE_TAG:-latest}" \
  python -m pytest tests/ -q

echo "── forge contract battery (gitea lane — bundled instance, no external tokens)"
docker compose exec -T app python - < scripts/contract_tests_forge.py

# Full Dagu → hello container → Redis → finalize (also wired into GHA ci.yml)
./scripts/ci_dispatch_hello.sh
echo "── CI suite green"
