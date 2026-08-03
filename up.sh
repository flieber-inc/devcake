#!/usr/bin/env bash
# Bring up the DevCake stack with host-discovered DOCKER_GID.
#
#   ./up.sh                 # upsert DOCKER_GID → docker compose up -d
#   ./up.sh --bake          # bake all first, then up
#   ./up.sh --bake app admin
#   ./up.sh -- dagu app     # pass service names to compose up
#   ./up.sh --dry-run       # print discovered GID + planned actions
#
# DOCKER_GID is host-specific (the group of /var/run/docker.sock). Compose
# requires it for Dagu's sock access; this script always re-discovers and
# writes it into .env so plain `docker compose up -d` works afterwards too.
set -euo pipefail
cd "$(dirname "$0")"

SOCK="${DOCKER_SOCK:-/var/run/docker.sock}"
DRY_RUN=0
DO_BAKE=0
BAKE_TARGETS=()
COMPOSE_ARGS=()

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage 0 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --bake)
      DO_BAKE=1
      shift
      # Collect bake targets until -- or end (default: all).
      while [[ $# -gt 0 && "$1" != --* && "$1" != "--" ]]; do
        BAKE_TARGETS+=("$1")
        shift
      done
      ;;
    --)
      shift
      COMPOSE_ARGS+=("$@")
      break
      ;;
    -*)
      echo "unknown option: $1 (try --help)" >&2
      usage 2
      ;;
    *)
      # Bare args after options → compose service names (same as after --).
      COMPOSE_ARGS+=("$@")
      break
      ;;
  esac
done

discover_docker_gid() {
  if [[ ! -e "$SOCK" ]]; then
    echo "error: $SOCK not found — is the Docker daemon running?" >&2
    exit 1
  fi
  local gid=""
  if gid=$(stat -c '%g' "$SOCK" 2>/dev/null); then
    :
  elif gid=$(stat -f '%g' "$SOCK" 2>/dev/null); then
    : # BSD/macOS
  else
    echo "error: cannot read group id of $SOCK" >&2
    exit 1
  fi
  if [[ ! "$gid" =~ ^[0-9]+$ ]]; then
    echo "error: unexpected DOCKER_GID from $SOCK: ${gid@Q}" >&2
    exit 1
  fi
  printf '%s\n' "$gid"
}

upsert_env_var() {
  local key="$1" val="$2" file="$3"
  local tmp
  tmp=$(mktemp)
  if [[ -f "$file" ]]; then
    awk -v k="$key" -v v="$val" '
      BEGIN { done = 0 }
      $0 ~ ("^" k "=") {
        print k "=" v
        done = 1
        next
      }
      { print }
      END {
        if (!done) {
          if (NR > 0) print ""
          print k "=" v
        }
      }
    ' "$file" >"$tmp"
  else
    printf '%s=%s\n' "$key" "$val" >"$tmp"
  fi
  # Preserve mode when replacing an existing .env (often 600).
  if [[ -f "$file" ]]; then
    chmod --reference="$file" "$tmp" 2>/dev/null || chmod "$(stat -c '%a' "$file" 2>/dev/null || echo 600)" "$tmp"
  else
    chmod 600 "$tmp"
  fi
  mv "$tmp" "$file"
}

GID="$(discover_docker_gid)"
echo "── DOCKER_GID=${GID}  (from ${SOCK})"

# ADR-0025: per-run workspace base. HOST-ABSOLUTE on purpose — dev-run.yaml
# bind sources resolve on the daemon host. Existing .env value wins (operator
# relocation); default is ./workspaces in this checkout. 0700: the tree holds
# repo source, activity transcripts and agent output (docs/14 §1).
WS_HOST="$(grep -E '^DEVCAKE_WS_HOST=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)"
if [[ -z "$WS_HOST" ]]; then
  WS_HOST="$(pwd)/workspaces"
fi
if [[ "$WS_HOST" != /* ]]; then
  echo "error: DEVCAKE_WS_HOST must be an absolute host path, got: ${WS_HOST@Q}" >&2
  exit 1
fi
echo "── DEVCAKE_WS_HOST=${WS_HOST}"

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    echo "── creating .env from .env.example"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      cp .env.example .env
      chmod 600 .env
    fi
  else
    echo "error: no .env and no .env.example — create .env with bootstrap passwords first" >&2
    exit 1
  fi
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "── would upsert DOCKER_GID=${GID} in .env"
  echo "── would upsert DEVCAKE_WS_HOST=${WS_HOST} in .env (+ mkdir -p, chmod 700)"
  if [[ "$DO_BAKE" -eq 1 ]]; then
    if [[ ${#BAKE_TARGETS[@]} -eq 0 ]]; then
      echo "── would: docker buildx bake all"
    else
      echo "── would: docker buildx bake ${BAKE_TARGETS[*]}"
    fi
  fi
  echo "── would: docker compose up -d ${COMPOSE_ARGS[*]:-}"
  exit 0
fi

upsert_env_var DOCKER_GID "$GID" .env
upsert_env_var DEVCAKE_WS_HOST "$WS_HOST" .env
mkdir -p "$WS_HOST"
chmod 700 "$WS_HOST"
# Export so this shell's compose invocation sees it even if env_file order is odd.
export DOCKER_GID="$GID"
export DEVCAKE_WS_HOST="$WS_HOST"

if [[ "$DO_BAKE" -eq 1 ]]; then
  if [[ ${#BAKE_TARGETS[@]} -eq 0 ]]; then
    echo "── docker buildx bake all"
    docker buildx bake all
  else
    echo "── docker buildx bake ${BAKE_TARGETS[*]}"
    docker buildx bake "${BAKE_TARGETS[@]}"
  fi
fi

if [[ ${#COMPOSE_ARGS[@]} -gt 0 ]]; then
  echo "── docker compose up -d ${COMPOSE_ARGS[*]}"
  docker compose up -d "${COMPOSE_ARGS[@]}"
else
  echo "── docker compose up -d"
  docker compose up -d
fi

echo "── stack starting (admin: http://localhost:8080)"
echo "   bootstrap passwords still come from .env; operator secrets via Config."
