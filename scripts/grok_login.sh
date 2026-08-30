#!/usr/bin/env bash
# One-time (or re-run on expiry) Grok Build OAuth login for a DevCake Dev Type.
#
# Usage: ./scripts/grok_login.sh <dev-type>
#   Positional wins over DEVCAKE_DEV_TYPE. Pass the operator-chosen Dev Type
#   name (first-setup's executor is a common grok-build example — names are
#   never hardcoded by the product seed).
# Env:
#   DEVCAKE_DEV_TYPE       — Dev Type name (required if no positional arg)
#   DEVCAKE_APP_CONTAINER  — app container id/name (default: docker compose ps -q app)
#
# Runs `grok login --device-auth` INSIDE the dev-grok-build image so the session
# is created in the exact runtime that will use it, then stores the resulting
# auth.json in the data volume at /data/secrets/<dev-type>/grok-auth.json
# (0600). Dev containers receive it per-run over the runspec channel and install
# it at ~/.grok/auth.json (docs/08 §4, docs/09 §3). Your local ~/.grok is never
# touched. Re-run this script whenever that Dev Type trips the DEV_AUTH breaker.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
source scripts/lib/app_container.sh

DEV_TYPE="${1:-${DEVCAKE_DEV_TYPE:-}}"
if [[ -z "$DEV_TYPE" ]]; then
  echo "grok_login: pass a Dev Type name (positional or DEVCAKE_DEV_TYPE)" >&2
  echo "  example after first-setup: ./scripts/grok_login.sh executor" >&2
  exit 2
fi
# Align with DevType.name in app/devcake/config.py (path-safe token).
if [[ ! "$DEV_TYPE" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "grok_login: refusing Dev Type name ${DEV_TYPE@Q}" >&2
  exit 2
fi

APP="$(devcake_app_container)"
if [[ -z "$APP" ]]; then
  echo "grok_login: no app container — start the stack (docker compose up -d app) or set DEVCAKE_APP_CONTAINER" >&2
  exit 1
fi

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
# Image user is uid 1000; mktemp is 0700 owned by the host operator.
chmod a+rwx "$OUT" 2>/dev/null || true

echo "── A URL and a code will appear below: open the URL, enter the code, approve."
docker run -it --rm -v "$OUT:/out" --entrypoint sh devcake/dev-grok-build:latest \
  -c 'grok login --device-auth && cp ~/.grok/auth.json /out/grok-auth.json'

[ -f "$OUT/grok-auth.json" ] || { echo "login did not produce auth.json"; exit 1; }

docker exec "$APP" mkdir -p "/data/secrets/${DEV_TYPE}"
docker cp "$OUT/grok-auth.json" "${APP}:/data/secrets/${DEV_TYPE}/grok-auth.json"
docker exec "$APP" sh -c \
  "chmod 600 /data/secrets/${DEV_TYPE}/grok-auth.json && ls -l /data/secrets/${DEV_TYPE}/"
echo "── stored. ${DEV_TYPE} is ready; the DEV_AUTH breaker (if tripped) clears on next dispatch."
