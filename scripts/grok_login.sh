#!/usr/bin/env bash
# One-time (or re-run on expiry) Grok Build OAuth login for DevCake's main-dev.
#
# Runs `grok login --device-auth` INSIDE the dev-grok-build image so the session
# is created in the exact runtime that will use it, then stores the resulting
# auth.json in the devcake_data volume at /data/secrets/main-dev/grok-auth.json
# (0600). Dev containers receive it per-run over the runspec channel and install
# it at ~/.grok/auth.json (docs/08 §4, docs/09 §3). Your local ~/.grok is never
# touched. Re-run this script whenever main-dev trips the DEV_AUTH breaker.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

echo "── A URL and a code will appear below: open the URL, enter the code, approve."
docker run -it --rm -v "$OUT:/out" --entrypoint sh devcake/dev-grok-build:latest \
  -c 'grok login --device-auth && cp ~/.grok/auth.json /out/grok-auth.json'

[ -f "$OUT/grok-auth.json" ] || { echo "login did not produce auth.json"; exit 1; }

docker exec devcake-app-1 mkdir -p /data/secrets/main-dev
docker cp "$OUT/grok-auth.json" devcake-app-1:/data/secrets/main-dev/grok-auth.json
docker exec devcake-app-1 sh -c \
  'chmod 600 /data/secrets/main-dev/grok-auth.json && ls -l /data/secrets/main-dev/'
echo "── stored. main-dev is ready; the DEV_AUTH breaker (if tripped) clears on next dispatch."
