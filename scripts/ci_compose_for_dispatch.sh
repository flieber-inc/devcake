#!/usr/bin/env bash
# Bring up the minimal compose set for hello dispatch smoke (no Gitea).
# Used by GHA ci.yml and optionally for local clean-room proof.
#
# Service set (transitive deps for app + Dagu spawning hello):
#   fluentbit  — redis/dagu depend_on + fluentd log driver
#   openobserve — app depends_on service_started
#   otel-collector — hello OTLP export target
#   redis, dagu, app, admin
#
# Third-party pull resilience (no registry credentials):
#   - Skip services whose digest-pinned image is already local (GHA unit-test
#     Redis leaves redis:7-alpine@sha256:… present before this step).
#   - Retry remaining pulls with exponential backoff — Docker Hub anonymous
#     `toomanyrequests` on shared GHA egress is the common failure mode.
# Override: CI_COMPOSE_PULL_ATTEMPTS (default 6), CI_COMPOSE_PULL_INITIAL_DELAY
# (seconds, default 20; doubles each retry, capped at 120).
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${DEVCAKE_TAG:-latest}"
DOCKER_GID="${DOCKER_GID:-$(stat -c %g /var/run/docker.sock 2>/dev/null || echo 0)}"
# ADR-0025: per-run workspace base — host-absolute (DAG bind sources resolve
# on the daemon host). 0777 on purpose: the GHA runner user is uid 1001 but
# the app container (which mkdirs run subdirs here) is uid 1000; CI runners
# are ephemeral, so the prod 0700 posture does not apply.
DEVCAKE_WS_HOST="${DEVCAKE_WS_HOST:-$(pwd)/workspaces}"

# On GHA always write synthetic .env. Locally: only if CI_COMPOSE_WRITE_ENV=1
# (never clobber a developer's real .env by default).
WRITE_ENV="${CI_COMPOSE_WRITE_ENV:-0}"
if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  WRITE_ENV=1
fi

ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Ci-admin-Pass1!}"
REDIS_PASSWORD="${REDIS_PASSWORD:-Ci-redis-Pass1!}"
DAGU_USER="${DAGU_USER:-devcake}"
DAGU_PASSWORD="${DAGU_PASSWORD:-Ci-dagu-Pass1!}"
OO_ROOT_EMAIL="${OO_ROOT_EMAIL:-ci@example.com}"
OO_ROOT_PASSWORD="${OO_ROOT_PASSWORD:-Ci-oo-root-Pass1!}"
OO_INGEST_EMAIL="${OO_INGEST_EMAIL:-ingest@example.com}"
OO_INGEST_PASSWORD="${OO_INGEST_PASSWORD:-Ci-oo-ingest-Pass1!}"

if [[ "$WRITE_ENV" == "1" ]]; then
  echo "── write ephemeral .env for dispatch compose (CI workspace only)"
  cat > .env <<EOF
DEVCAKE_TAG=${TAG}
OO_ROOT_EMAIL=${OO_ROOT_EMAIL}
OO_ROOT_PASSWORD=${OO_ROOT_PASSWORD}
OO_INGEST_EMAIL=${OO_INGEST_EMAIL}
OO_INGEST_PASSWORD=${OO_INGEST_PASSWORD}
GITEA_ADMIN_USER=devcakeadmin
GITEA_ADMIN_PASSWORD=Ci-gitea-unused-Pass1!
DAGU_USER=${DAGU_USER}
DAGU_PASSWORD=${DAGU_PASSWORD}
REDIS_PASSWORD=${REDIS_PASSWORD}
ADMIN_USER=${ADMIN_USER}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
DOCKER_GID=${DOCKER_GID}
DEVCAKE_WS_HOST=${DEVCAKE_WS_HOST}
DAGU_UI_URL=http://localhost:8525
OO_UI_URL=http://localhost:5080
EOF
elif [[ ! -f .env ]]; then
  echo "no .env present; set CI_COMPOSE_WRITE_ENV=1 to generate a synthetic one, or copy .env.example" >&2
  exit 1
else
  echo "── reusing existing .env (CI_COMPOSE_WRITE_ENV not set)"
  # shellcheck disable=SC1091
  set -a
  # load only what readiness curls need
  while IFS= read -r line; do
    case "$line" in
      ADMIN_USER=*|ADMIN_PASSWORD=*|REDIS_PASSWORD=*|DAGU_USER=*|DAGU_PASSWORD=*|DEVCAKE_TAG=*|DEVCAKE_WS_HOST=*)
        export "${line?}" ;;
    esac
  done < <(grep -E '^(ADMIN_USER|ADMIN_PASSWORD|REDIS_PASSWORD|DAGU_USER|DAGU_PASSWORD|DEVCAKE_TAG|DEVCAKE_WS_HOST)=' .env || true)
  set +a
  ADMIN_USER="${ADMIN_USER:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:?ADMIN_PASSWORD missing in .env}"
fi

export DEVCAKE_TAG="$TAG"
export ADMIN_USER ADMIN_PASSWORD REDIS_PASSWORD DAGU_USER DAGU_PASSWORD
export DEVCAKE_WS_HOST

# The dir must exist app-writable BEFORE compose up: dockerd auto-creates an
# absent bind source ROOT-owned, and the app's boot writability probe would
# then fail health forever (ADR-0025 R8). chmod only on the synthetic-env
# (CI) path — a developer's real base keeps up.sh's 0700 posture.
mkdir -p "$DEVCAKE_WS_HOST"
if [[ "$WRITE_ENV" == "1" ]]; then
  chmod 0777 "$DEVCAKE_WS_HOST"
fi

SERVICES=(fluentbit openobserve redis dagu otel-collector app admin)
THIRD_PARTY=(fluentbit openobserve redis dagu otel-collector)

# Resolve compose image refs once (digest-pinned in docker-compose.yml).
# Prints "service<TAB>image" lines for the given service names.
_third_party_images() {
  docker compose config --format json | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
for name in sys.argv[1:]:
    svc = cfg["services"].get(name)
    if not svc or "image" not in svc:
        sys.stderr.write("compose service %r has no image\n" % (name,))
        sys.exit(1)
    print("%s\t%s" % (name, svc["image"]))
' "$@"
}

echo "── pull third-party images for dispatch set"
PULL_ATTEMPTS="${CI_COMPOSE_PULL_ATTEMPTS:-6}"
PULL_DELAY="${CI_COMPOSE_PULL_INITIAL_DELAY:-20}"
attempt=1
while true; do
  mapfile -t _img_rows < <(_third_party_images "${THIRD_PARTY[@]}")
  to_pull=()
  for row in "${_img_rows[@]}"; do
    svc="${row%%$'\t'*}"
    img="${row#*$'\t'}"
    if docker image inspect "$img" >/dev/null 2>&1; then
      echo "  $svc: already local"
    else
      echo "  $svc: need pull"
      to_pull+=("$svc")
    fi
  done
  if [[ ${#to_pull[@]} -eq 0 ]]; then
    echo "  all third-party images present"
    break
  fi
  echo "  pulling: ${to_pull[*]} (attempt ${attempt}/${PULL_ATTEMPTS})"
  if docker compose pull "${to_pull[@]}"; then
    break
  fi
  if [[ "$attempt" -ge "$PULL_ATTEMPTS" ]]; then
    echo "compose pull failed after ${PULL_ATTEMPTS} attempts (Docker Hub rate limit?)" >&2
    exit 1
  fi
  echo "  pull failed (often toomanyrequests); sleeping ${PULL_DELAY}s before retry…"
  sleep "$PULL_DELAY"
  attempt=$((attempt + 1))
  PULL_DELAY=$((PULL_DELAY * 2))
  if [[ "$PULL_DELAY" -gt 120 ]]; then
    PULL_DELAY=120
  fi
done

echo "── compose up: ${SERVICES[*]}"
docker compose up -d "${SERVICES[@]}"

# status via docker inspect: Health.Status if present, else State.Status
_container_status() {
  local svc="$1" cid
  cid=$(docker compose ps -q "$svc" 2>/dev/null | head -1)
  [[ -n "$cid" ]] || { echo missing; return; }
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo missing
}

wait_service() {
  local svc="$1" want="$2" max="${3:-60}" i st
  for i in $(seq 1 "$max"); do
    st=$(_container_status "$svc")
    if [[ "$st" == "$want" ]]; then
      echo "  $svc → $st (attempt $i)"
      return 0
    fi
    # services without healthchecks: "running" is enough
    if [[ "$want" == "running" && "$st" == "running" ]]; then
      echo "  $svc → running (attempt $i)"
      return 0
    fi
    sleep 2
  done
  echo "  $svc NOT $want (last=$st) after ${max} attempts" >&2
  return 1
}

echo "── wait for service readiness"
wait_service fluentbit running 30
wait_service openobserve running 45
wait_service redis healthy 40
wait_service dagu healthy 40
wait_service app healthy 60
wait_service admin healthy 40

echo "── wait for authenticated control plane via admin :8080"
BASE_URL="${DEVCAKE_BASE_URL:-http://127.0.0.1:8080}"
for i in $(seq 1 60); do
  if curl -sf "${BASE_URL}/nginx-health" | grep -q ok; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
      "${BASE_URL}/api/v1/runs" || echo 000)
    if [[ "$code" == "200" ]]; then
      echo "  authenticated GET /api/v1/runs → 200 (attempt $i)"
      echo "── compose stack ready for dispatch"
      exit 0
    fi
    echo "  nginx ok; /api/v1/runs → ${code} (attempt $i)"
  else
    echo "  nginx-health not ready (attempt $i)"
  fi
  sleep 2
done

echo "compose stack failed readiness" >&2
docker compose ps || true
docker compose logs --tail=80 app admin dagu redis fluentbit || true
exit 1
