#!/usr/bin/env bash
# Always-fresh unit suite: rebuild app-test (COPY'd tree) then pytest.
# Avoids the stale-image false green when app/ or tests/ changed since last bake.
# Uses scripts/lib/bake_app_test.sh: Docker Buildx bake when available, else
# `docker build -f app/Dockerfile --target test` (podman/buildah hosts).
# For the full local suite (forge battery + dispatch smoke) use ci_suite.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

# Redis for the live messaging tests (test_messaging_live.py — 14 tests that
# otherwise fail on ConnectionError). Preference order:
#   1. the compose network's redis when the stack is up (devcake_control);
#   2. a throwaway redis on a scratch network, exactly like ci.yml runs it,
#      torn down on exit. A cold machine gets the same green as CI.
REDIS_PASSWORD="${REDIS_PASSWORD:-}"
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  while IFS= read -r line; do
    case "$line" in
      REDIS_PASSWORD=*) export "${line?}" ;;
    esac
  done < <(grep -E '^REDIS_PASSWORD=' .env || true)
fi

# Always rebuild before pytest (stale COPY'd image = silent false green).
bash scripts/lib/bake_app_test.sh

TAG="${DEVCAKE_TAG:-latest}"
NET_ARGS=()
ENV_ARGS=(-e "OTEL_SDK_DISABLED=true")  # no openobserve teardown noise in tests
SCRATCH_NET="devcake-pytest"
SCRATCH_REDIS="devcake-pytest-redis"
# Pin matches ci.yml so local and CI grade the same redis.
REDIS_IMAGE="redis:7-alpine@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"

cleanup() {
  docker rm -f "${SCRATCH_REDIS}" >/dev/null 2>&1 || true
  docker network rm "${SCRATCH_NET}" >/dev/null 2>&1 || true
}

if docker network inspect devcake_control >/dev/null 2>&1; then
  NET_ARGS=(--network devcake_control)
  if [[ -n "${REDIS_PASSWORD:-}" ]]; then
    ENV_ARGS+=(-e "REDIS_URL=redis://redis:6379/0" -e "REDIS_PASSWORD=${REDIS_PASSWORD}")
  fi
else
  echo "── compose network absent: starting throwaway redis (as ci.yml does)"
  trap cleanup EXIT
  cleanup
  docker network create "${SCRATCH_NET}" >/dev/null
  docker run -d --name "${SCRATCH_REDIS}" --network "${SCRATCH_NET}" \
    --network-alias redis "${REDIS_IMAGE}" \
    redis-server --requirepass ci-redis-pass --appendonly no >/dev/null
  for _ in $(seq 1 30); do
    if docker exec "${SCRATCH_REDIS}" redis-cli --no-auth-warning -a ci-redis-pass ping 2>/dev/null | grep -q PONG; then
      break
    fi
    sleep 1
  done
  NET_ARGS=(--network "${SCRATCH_NET}")
  ENV_ARGS+=(-e "REDIS_URL=redis://redis:6379/0" -e "REDIS_PASSWORD=ci-redis-pass")
fi

echo "── pytest via devcake/app-test:${TAG}"
docker run --rm \
  "${NET_ARGS[@]}" \
  "${ENV_ARGS[@]}" \
  -v "$(pwd)/images/common:/srv/images/common:ro" \
  -v "$(pwd)/images/hello:/srv/images/hello:ro" \
  -v "$(pwd)/images:/srv/images-tree:ro" \
  -v "$(pwd)/docker-compose.yml:/srv/docker-compose.yml:ro" \
  -v "$(pwd)/docker-bake.hcl:/srv/docker-bake.hcl:ro" \
  -v "$(pwd)/.github/workflows/docker-publish.yml:/srv/docker-publish.yml:ro" \
  -v "$(pwd)/dagu/dags:/srv/dagu-dags:ro" \
  -v "$(pwd)/app/Dockerfile:/srv/app.Dockerfile:ro" \
  -v "$(pwd)/images/Dockerfile:/srv/images.Dockerfile:ro" \
  -v "$(pwd)/docs:/srv/docs:ro" \
  -v "$(pwd)/scripts:/srv/repo-scripts:ro" \
  -v "$(pwd)/up.sh:/srv/up.sh:ro" \
  -w /srv \
  "devcake/app-test:${TAG}" \
  python -m pytest tests/ -q -o cache_dir=/tmp/pytest-cache "$@"
