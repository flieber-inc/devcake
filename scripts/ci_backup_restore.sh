#!/usr/bin/env bash
# Real wrapper/volume/boot drill; only for disposable GitHub-hosted CI.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then
  echo "refusing: backup/restore drill requires a disposable GitHub-hosted runner" >&2
  exit 2
fi
cd "$(dirname "$0")/.."
: "${RUNNER_TEMP:?}" "${GITHUB_RUN_ID:?}" "${GITHUB_RUN_ATTEMPT:?}"
: "${ADMIN_USER:?}" "${ADMIN_PASSWORD:?}"
[[ "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT" =~ ^[0-9]+-[0-9]+$ ]]
ALPINE_IMAGE="alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
DRILL_DIR="$(mktemp -d "$RUNNER_TEMP/devcake-restore.XXXXXX")"
chmod 700 "$DRILL_DIR"
RESTORED_DATA="devcake-ci-restored-data-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
RESTORED_GITEA="devcake-ci-restored-gitea-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
OVERRIDE="$DRILL_DIR/compose-restored.yml"
cat > "$OVERRIDE" <<EOF
volumes:
  devcake_data:
    external: true
    name: $RESTORED_DATA
  gitea_data:
    external: true
    name: $RESTORED_GITEA
EOF
restored_compose() { docker compose -f docker-compose.yml -f "$OVERRIDE" "$@"; }
cleanup() {
  restored_compose down --remove-orphans >/dev/null 2>&1 || true
  docker volume rm "$RESTORED_DATA" "$RESTORED_GITEA" >/dev/null 2>&1 || true
  rm -rf "$DRILL_DIR"
}
trap cleanup EXIT

# Capture a completed run that must remain readable after boot reconciliation.
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" http://127.0.0.1:8080/api/v1/runs \
  > "$DRILL_DIR/runs.json"
RUN_ID=$(python3 - "$DRILL_DIR/runs.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
rows = data if isinstance(data, list) else data["runs"]
print(next(r["run_id"] for r in rows if r["state"] == "finished"))
PY
)
docker compose stop app gitea
snapshot() {
  docker run --rm -v "$1:/src:ro" "$ALPINE_IMAGE" sh -ec \
    'cd /src; find . -type f -exec sha256sum {} \; | sort; find . -exec stat -c "%a %u %g %N" {} \; | sort' > "$2"
}
snapshot devcake_devcake_data "$DRILL_DIR/data.before"
snapshot devcake_gitea_data "$DRILL_DIR/gitea.before"
bash scripts/backup_data.sh "$DRILL_DIR/data.tar.gz"
bash scripts/backup_gitea.sh "$DRILL_DIR/gitea.tar.gz"
test "$(stat -c %a "$DRILL_DIR/data.tar.gz")" = 600
test "$(stat -c %a "$DRILL_DIR/gitea.tar.gz")" = 600

docker volume create "$RESTORED_DATA" >/dev/null
docker volume create "$RESTORED_GITEA" >/dev/null
export DEVCAKE_DATA_VOLUME="$RESTORED_DATA" GITEA_VOLUME="$RESTORED_GITEA"
head -c 64 "$DRILL_DIR/data.tar.gz" > "$DRILL_DIR/corrupt.tar.gz"
for kind in data gitea; do
  if [ "$kind" = data ]; then
    volume="$RESTORED_DATA"; wrong=gitea
  else
    volume="$RESTORED_GITEA"; wrong=data
  fi
  docker run --rm -v "$volume:/dst" "$ALPINE_IMAGE" sh -ec \
    'echo synthetic-keep-me > /dst/sentinel; chmod 600 /dst/sentinel'
  snapshot "$volume" "$DRILL_DIR/target.before"
  for archive in "$DRILL_DIR/$wrong.tar.gz" "$DRILL_DIR/corrupt.tar.gz"; do
    if bash "scripts/restore_$kind.sh" "$archive" > "$DRILL_DIR/refusal.log" 2>&1; then
      echo "FAIL: $kind restore accepted an invalid archive" >&2
      exit 1
    fi
    snapshot "$volume" "$DRILL_DIR/target.after"
    cmp "$DRILL_DIR/target.before" "$DRILL_DIR/target.after"
  done
  bash "scripts/restore_$kind.sh" "$DRILL_DIR/$kind.tar.gz"
  snapshot "$volume" "$DRILL_DIR/$kind.after"
  cmp "$DRILL_DIR/$kind.before" "$DRILL_DIR/$kind.after"
  echo "$kind: wrong-kind/corrupt archives refused; valid restore preserved contents and permissions"
done

# Start the actual app + Gitea against the RESTORED volumes and wait for health.
restored_compose up -d --wait --wait-timeout 180 \
  fluentbit openobserve otel-collector redis dagu gitea app admin
curl -fsS -u "$ADMIN_USER:$ADMIN_PASSWORD" \
  "http://127.0.0.1:8080/api/v1/runs/$RUN_ID" > "$DRILL_DIR/restored-run.json"
python3 - "$DRILL_DIR/restored-run.json" "$RUN_ID" <<'PY'
import json, sys
run = json.load(open(sys.argv[1]))
assert run["run_id"] == sys.argv[2] and run["state"] == "finished", run["state"]
PY
bash scripts/ci_dispatch_hello.sh
restored_compose exec -T app python - < scripts/contract_tests_forge.py
restored_compose exec -T app python - < scripts/contract_tests_pmo.py
echo "backup/restore drill passed: restored run readable, stack healthy, new dispatch finished"
