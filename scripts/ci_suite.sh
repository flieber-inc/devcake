#!/usr/bin/env bash
# DevCake CI suite (docs/16 M7): deterministic, model-free, ~1 minute.
# Requires the compose stack running + Bake images present.
# Real-model acceptance is scripts/acceptance.py.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
set -a
# Load Redis password (and anything else tests may need) from .env
# without treating the whole file as executable shell.
if [[ -f .env ]]; then
  # shellcheck disable=SC2046
  export $(grep -E '^(REDIS_PASSWORD|ADMIN_USER|ADMIN_PASSWORD)=' .env | xargs)
fi
set +a

: "${REDIS_PASSWORD:?REDIS_PASSWORD must be set (compose .env)}"
: "${ADMIN_USER:?ADMIN_USER must be set (compose .env)}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set (compose .env)}"

echo "── digest-pin gate (ISSUES #29)"
python3 scripts/check_image_pins.py

echo "── bake app-test (prod image has no pytest)"
docker buildx bake -f docker-bake.hcl app-test

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

echo "── stub-harness smoke: full dispatch pipeline (Dagu → container → Redis → finalize)"
RUN=$(curl -sf -u "$ADMIN_USER:$ADMIN_PASSWORD" -H 'X-DevCake-Request: 1' -X POST \
  "http://localhost:8080/api/v1/debug/dispatch-hello?sleep=2" | python3 -c "import json,sys; print(json.load(sys.stdin)['run_id'])")
for i in $(seq 1 30); do
  STATE=$(curl -sf -u "$ADMIN_USER:$ADMIN_PASSWORD" \
    "http://localhost:8080/api/v1/runs/$RUN" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")
  [ "$STATE" = "finished" ] && break
  [ "$STATE" = "failed" ] || [ "$STATE" = "timed_out" ] && { echo "hello run $STATE"; exit 1; }
  sleep 3
done
[ "$STATE" = "finished" ] || { echo "hello run stuck in $STATE"; exit 1; }
echo "hello run finished ✓"
echo "── CI suite green"
