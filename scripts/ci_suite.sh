#!/usr/bin/env bash
# DevCake CI suite (docs/16 M7): deterministic, model-free, ~1 minute.
# Requires the compose stack running. Real-model acceptance is scripts/acceptance.py.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "── unit + live-redis tests (INV-1..6 coverage)"
docker compose exec -T -w /srv app python -m pytest tests/ -q

echo "── stub-harness smoke: full dispatch pipeline (Dagu → container → Redis → finalize)"
source <(grep -E "^ADMIN_(USER|PASSWORD)=" .env | sed 's/^/export /')
RUN=$(curl -sf -u "$ADMIN_USER:$ADMIN_PASSWORD" -X POST \
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
