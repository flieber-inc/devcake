#!/usr/bin/env bash
# Always-fresh unit suite: rebake app-test (COPY'd tree) then pytest.
# Avoids the stale-image false green when app/ or tests/ changed since last bake.
# For the full local suite (forge battery + dispatch smoke) use ci_suite.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

# Optional Redis for live messaging tests (compose network). If the network
# is missing, pytest still runs; live-redis tests skip or fail on connection.
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  while IFS= read -r line; do
    case "$line" in
      REDIS_PASSWORD=*) export "${line?}" ;;
    esac
  done < <(grep -E '^REDIS_PASSWORD=' .env || true)
fi

echo "── bake app-test (ensures image matches working tree)"
docker buildx bake -f docker-bake.hcl app-test

TAG="${DEVCAKE_TAG:-latest}"
NET_ARGS=()
ENV_ARGS=()
if docker network inspect devcake_control >/dev/null 2>&1; then
  NET_ARGS=(--network devcake_control)
  if [[ -n "${REDIS_PASSWORD:-}" ]]; then
    ENV_ARGS=(-e "REDIS_URL=redis://redis:6379/0" -e "REDIS_PASSWORD=${REDIS_PASSWORD}")
  fi
fi

echo "── pytest via devcake/app-test:${TAG}"
docker run --rm \
  "${NET_ARGS[@]}" \
  "${ENV_ARGS[@]}" \
  -v "$(pwd)/images/common:/srv/images/common:ro" \
  -v "$(pwd)/dagu/dags:/srv/dagu-dags:ro" \
  -v "$(pwd)/app/Dockerfile:/srv/app.Dockerfile:ro" \
  -v "$(pwd)/docs:/srv/docs:ro" \
  -w /srv \
  "devcake/app-test:${TAG}" \
  python -m pytest tests/ -q "$@"
