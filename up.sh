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

# ONE derivation, shared with the CI bring-up (ADR-0034; policy stays here)
# shellcheck source=scripts/lib/stack_env.sh
source "$(dirname "$0")/scripts/lib/stack_env.sh"

discover_docker_gid() {
  local gid=""
  if ! gid="$(devcake_docker_gid "$SOCK")"; then
    echo "error: cannot derive DOCKER_GID from $SOCK — is the Docker daemon running?" >&2
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
WS_HOST="$(devcake_ws_host .env "$(pwd)")"
if [[ "$WS_HOST" != /* ]]; then
  echo "error: DEVCAKE_WS_HOST must be an absolute host path, got: ${WS_HOST@Q}" >&2
  exit 1
fi
echo "── DEVCAKE_WS_HOST=${WS_HOST}"

# AUD-004: bake and compose MUST agree on the image tag. `docker buildx bake`
# reads DEVCAKE_TAG from the PROCESS env / HCL default — never from .env — so a
# pinned `.env` tag would bake `:latest` while compose runs the pin, silently
# dispatching dev-* images that were never baked this round (pull_policy:
# missing). Resolve ONCE and export for both. Precedence: an already-exported
# DEVCAKE_TAG (the `export DEVCAKE_TAG=$(git rev-parse --short HEAD)` release
# ritual) > .env > "latest".
TAG="${DEVCAKE_TAG:-$(grep -E '^DEVCAKE_TAG=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)}"
TAG="${TAG:-latest}"
echo "── DEVCAKE_TAG=${TAG}  (bake + compose lockstep)"

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
    echo "── would: docker compose stop dagu (deploy window — ADR-0025 R9)"
    if [[ ${#BAKE_TARGETS[@]} -eq 0 ]]; then
      echo "── would: DEVCAKE_TAG=${TAG} docker buildx bake all"
    else
      echo "── would: DEVCAKE_TAG=${TAG} docker buildx bake ${BAKE_TARGETS[*]}"
    fi
  fi
  echo "── would: docker compose up -d ${COMPOSE_ARGS[*]:-}"
  exit 0
fi

upsert_env_var DOCKER_GID "$GID" .env
upsert_env_var DEVCAKE_WS_HOST "$WS_HOST" .env
# 2026-08 evaluation: .env holds every bootstrap password (and the admin
# password is host-root-equivalent via settings export — docs/14 §3), yet a
# file created before up.sh existed kept whatever mode it was born with —
# 0644 observed in the wild. Upserts above preserve-by-reference, so enforce
# the floor every run, not only at creation.
chmod 600 .env
mkdir -p "$WS_HOST"
chmod 700 "$WS_HOST"
# Export so this shell's bake + compose invocations all see the same values
# even if env_file order is odd (AUD-004: DEVCAKE_TAG for the bake too).
export DOCKER_GID="$GID"
export DEVCAKE_WS_HOST="$WS_HOST"
export DEVCAKE_TAG="$TAG"

if [[ "$DO_BAKE" -eq 1 ]]; then
  # AUD-003: a --bake is a DEPLOY. `./dagu/dags` is a LIVE :ro bind, so a
  # pulled two-step DAG is already visible to a running dagu that may lack the
  # new env (empty $DEVCAKE_WS_HOST → dockerd creates a root-owned junk dir at
  # the host root). Stop dagu BEFORE the multi-minute bake to close that
  # window; `up -d` below recreates it with the current env (ADR-0025 R9).
  if [[ -n "$(docker compose ps -q dagu 2>/dev/null)" ]]; then
    echo "── stopping dagu before bake (deploy window — ADR-0025 R9)"
    docker compose stop dagu || true
    # 2026-08 evaluation: under `set -e` a failed bake (network blip pulling
    # a base layer, upstream installer change) used to abort the script HERE
    # — with dagu silently stopped, the app healthy, and every dispatch
    # failing until an operator noticed. Restart dagu on ANY error exit for
    # as long as the bake window is open; cleared right after the bake so it
    # can never fire spuriously later.
    trap 'echo "── bake failed: restarting dagu (half-down stack guard)" >&2; docker compose start dagu || true' ERR
  fi
  if [[ ${#BAKE_TARGETS[@]} -eq 0 ]]; then
    echo "── docker buildx bake all"
    docker buildx bake all
  else
    echo "── docker buildx bake ${BAKE_TARGETS[*]}"
    docker buildx bake "${BAKE_TARGETS[@]}"
  fi
  trap - ERR
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
