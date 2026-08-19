#!/usr/bin/env bash
# Bring up the DevCake stack with host-discovered DOCKER_GID.
#
#   ./up.sh                 # upsert GID/WS_HOST/TAG → compose up -d → baker
#   ./up.sh --bake          # bake control plane + hello, then up + baker
#   ./up.sh --bake app admin
#   ./up.sh -- dagu app     # pass service names to compose up
#   ./up.sh --dry-run       # print discovered GID + planned actions
#
# DOCKER_GID is host-specific (the group of /var/run/docker.sock). Compose
# requires it for Dagu's sock access; this script always re-discovers and
# writes it (plus DEVCAKE_WS_HOST and DEVCAKE_TAG) into .env so plain
# `docker compose up -d` works afterwards too.
set -euo pipefail
cd "$(dirname "$0")"

SOCK="${DOCKER_SOCK:-/var/run/docker.sock}"
DRY_RUN=0
DO_BAKE=0
BAKE_TARGETS=()
COMPOSE_ARGS=()

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \?//'
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
  # SAME-FILESYSTEM temp (2026-08-12 audit OPS-L6): mktemp defaults to
  # $TMPDIR (often a tmpfs), so `mv tmp .env` was a cross-device COPY —
  # non-atomic, and a crash mid-copy could truncate the file holding every
  # bootstrap password. A sibling temp makes the mv a pure rename.
  tmp=$(mktemp "$(dirname "$file")/.env.tmp.XXXXXX")
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
# dispatching dev-* images that were never baked this round (dev-run.yaml
# pull: never). Resolve ONCE, export for both, and upsert into .env so a later
# plain `docker compose up -d` (no shell export) stays lockstep. Precedence:
# already-exported DEVCAKE_TAG (the
# `export DEVCAKE_TAG=$(git rev-parse --short HEAD)` release ritual) > .env >
# "latest".
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
  echo "── would upsert DEVCAKE_TAG=${TAG} in .env"
  if [[ "$DO_BAKE" -eq 1 ]]; then
    echo "── would: docker compose stop dagu (deploy window — ADR-0025 R9)"
    echo "── would: compute DEVCAKE_APP_DIGEST from scripts/app_digest.py"
    if [[ ${#BAKE_TARGETS[@]} -eq 0 ]]; then
      echo "── would: DEVCAKE_TAG=${TAG} docker buildx bake app admin hello"
    else
      echo "── would: DEVCAKE_TAG=${TAG} docker buildx bake ${BAKE_TARGETS[*]}"
    fi
  fi
  echo "── would: docker compose up -d ${COMPOSE_ARGS[*]:-}"
  echo "── would: start host baker (.factory/watch.pid) — not a compose service"
  exit 0
fi

upsert_env_var DOCKER_GID "$GID" .env
upsert_env_var DEVCAKE_WS_HOST "$WS_HOST" .env
upsert_env_var DEVCAKE_TAG "$TAG" .env
# 2026-08 evaluation: .env holds every bootstrap password (and the admin
# password is host-root-equivalent via settings export — docs/14 §0 / §4), yet a
# file created before up.sh existed kept whatever mode it was born with —
# 0644 observed in the wild. Upserts above preserve-by-reference, so enforce
# the floor every run, not only at creation.
chmod 600 .env
mkdir -p "$WS_HOST"
chmod 700 "$WS_HOST"
# Export so this shell's bake + compose invocations all see the same values
# even if env_file order is odd (AUD-004: DEVCAKE_TAG for the bake too).
# .env upsert above is the durable half — plain compose after this session
# substitutes the same pin without needing the export still in the shell.
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
    # SIGINT/SIGTERM do NOT fire an ERR trap (2026-08-12 audit OPS-M2): an
    # operator who Ctrl-C's a slow bake got exactly the half-down failure the
    # ERR guard was built for — app healthy, dagu silently stopped, every
    # dispatch failing. Restore dagu on an interrupt too, then re-raise the
    # default disposition so the interrupt still stops the script.
    _restore_dagu() {
      echo "── bake interrupted/failed: restarting dagu (half-down stack guard)" >&2
      docker compose start dagu || true
    }
    trap '_restore_dagu' ERR
    trap '_restore_dagu; trap - INT TERM; kill -INT $$' INT TERM
  fi
  DIGEST="$(python3 scripts/app_digest.py)"
  export DEVCAKE_APP_DIGEST="$DIGEST"
  echo "── DEVCAKE_APP_DIGEST=${DIGEST}"
  if [[ ${#BAKE_TARGETS[@]} -eq 0 ]]; then
    # Harnesses come from the host baker after types exist (keep-set).
    # Virgin host = control plane + hello only. `./up.sh --bake all` still
    # compiles the full matrix when an operator asks for it.
    echo "── docker buildx bake app admin hello"
    docker buildx bake app admin hello
  else
    echo "── docker buildx bake ${BAKE_TARGETS[*]}"
    docker buildx bake "${BAKE_TARGETS[@]}"
  fi
  trap - ERR INT TERM
fi

if [[ ${#COMPOSE_ARGS[@]} -gt 0 ]]; then
  echo "── docker compose up -d ${COMPOSE_ARGS[*]}"
  docker compose up -d "${COMPOSE_ARGS[@]}"
else
  echo "── docker compose up -d"
  docker compose up -d
fi

# Post-up health gate (2026-08-12 audit OPS-M2): the deploy used to return the
# moment `up -d` dispatched the containers — a wedged app (bad config, failed
# migration, unreachable dependency) was the operator's to discover later.
# Wait for the app's own /health/live, non-fatally: a timeout WARNS with the
# diagnostic command instead of failing the script (the containers are up;
# the operator may want to inspect logs rather than have up.sh exit non-zero).
echo "── waiting for the app to report healthy…"
_app_live() {
  docker compose exec -T app python -c \
    "import urllib.request as u; u.urlopen('http://localhost:8000/api/v1/health/live', timeout=3)" \
    >/dev/null 2>&1
}
_app_deps() {
  # /health needs basic auth; the app container already has ADMIN_*.
  # Compose healthcheck stays on /health/live (must stay cheap / never 500).
  docker compose exec -T app python -c '
import base64, json, os, urllib.request
u, p = os.environ.get("ADMIN_USER", ""), os.environ.get("ADMIN_PASSWORD", "")
tok = base64.b64encode(f"{u}:{p}".encode()).decode()
req = urllib.request.Request(
    "http://localhost:8000/api/v1/health",
    headers={"Authorization": f"Basic {tok}"})
body = json.loads(urllib.request.urlopen(req, timeout=10).read())
bad = [k for k in ("redis", "dagu") if body.get(k) is False]
raise SystemExit(1 if bad else 0)
' >/dev/null 2>&1
}
_ok=0
for _ in $(seq 1 30); do
  if _app_live; then _ok=1; break; fi
  sleep 2
done
if [[ "$_ok" -eq 1 ]]; then
  echo "── app live ✓"
  if _app_deps; then
    echo "── app redis+dagu probes ok ✓"
  else
    echo "── WARNING: app is live but redis/dagu probe is red — check: docker compose logs --tail=50 app" >&2
  fi
else
  echo "── WARNING: app did not report live within ~60s. The stack is up," >&2
  echo "   but the app may be wedged — check: docker compose logs --tail=50 app" >&2
fi

# Host baker: same machine and socket as today's bake — not a compose
# service, not a new socket-holder. It reads the app-published keep-set
# and compiles + probes when pins change.
_FACTORY_DIR="$(pwd)/.factory"
mkdir -p "$_FACTORY_DIR"
if [[ -f "$_FACTORY_DIR/watch.pid" ]]; then
  _old="$(cat "$_FACTORY_DIR/watch.pid" 2>/dev/null || true)"
  if [[ -n "$_old" ]] && kill -0 "$_old" 2>/dev/null; then
    echo "── restarting host baker (was pid ${_old})"
    kill "$_old" 2>/dev/null || true
    sleep 0.3
  fi
fi
# Ingest creds so the baker can ship a dying word to OO if the app is
# already gone (the app's push_oo_log chokepoint cannot run then).
# Read only those keys — do not source the whole .env into this shell.
if [[ -f .env ]]; then
  while IFS= read -r line; do
    case "$line" in
      OO_INGEST_EMAIL=*|OO_INGEST_PASSWORD=*|OO_ORG=*) export "${line?}" ;;
    esac
  done < <(grep -E '^(OO_INGEST_EMAIL|OO_INGEST_PASSWORD|OO_ORG)=' .env || true)
fi
export PYTHONUNBUFFERED=1
export PYTHONPATH="$(pwd)/scripts:$(pwd)/app"
export DEVCAKE_OO_URL="http://127.0.0.1:5080"
# Inherit OO_INGEST_* from the exports above — do not put the password
# on the process argv (readable via ps /proc).
nohup python3 -m dev_factory \
  >>"$_FACTORY_DIR/watch.log" 2>&1 &
echo $! >"$_FACTORY_DIR/watch.pid"
echo "── host baker watching keep-set (pid $! → .factory/watch.log)"

echo "── stack starting (admin: http://localhost:8080)"
echo "   bootstrap passwords still come from .env; operator secrets via Config."
