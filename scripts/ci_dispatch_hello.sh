#!/usr/bin/env bash
# Stub-harness smoke: full dispatch pipeline (Dagu → container → Redis → finalize).
# Preconditions: compose stack up (app + admin + redis + dagu + …), hello image
# baked as devcake/dev-hello:${DEVCAKE_TAG:-latest}, ADMIN_* credentials set.
# Used by scripts/ci_suite.sh and .github/workflows/ci.yml.
set -euo pipefail

: "${ADMIN_USER:?ADMIN_USER must be set}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD must be set}"

BASE_URL="${DEVCAKE_BASE_URL:-http://localhost:8080}"
SLEEP_SECS="${DEVCAKE_HELLO_SLEEP:-2}"
MAX_POLLS="${DEVCAKE_HELLO_POLLS:-30}"
POLL_SLEEP="${DEVCAKE_HELLO_POLL_SLEEP:-3}"

echo "── stub-harness smoke: POST ${BASE_URL}/api/v1/debug/dispatch-hello"
RUN=$(curl -sf -u "$ADMIN_USER:$ADMIN_PASSWORD" -H 'X-DevCake-Request: 1' -X POST \
  "${BASE_URL}/api/v1/debug/dispatch-hello?sleep=${SLEEP_SECS}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['run_id'])")
echo "hello run_id=${RUN}"

STATE=""
for i in $(seq 1 "$MAX_POLLS"); do
  STATE=$(curl -sf -u "$ADMIN_USER:$ADMIN_PASSWORD" \
    "${BASE_URL}/api/v1/runs/${RUN}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")
  echo "  poll ${i}/${MAX_POLLS}: state=${STATE}"
  [ "$STATE" = "finished" ] && break
  [ "$STATE" = "failed" ] || [ "$STATE" = "timed_out" ] && {
    echo "hello run $STATE" >&2
    exit 1
  }
  sleep "$POLL_SLEEP"
done
[ "$STATE" = "finished" ] || {
  echo "hello run stuck in ${STATE:-unknown}" >&2
  exit 1
}
echo "hello run finished ✓"
