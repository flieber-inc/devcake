#!/usr/bin/env bash
# Run the drift probe INSIDE a baked harness image. Part of the bake verb.
# Usage: host_probe.sh <template> <cli_version> [image] [receipts_dir] [digest]
set -euo pipefail
cd "$(dirname "$0")/../.."

TEMPLATE="${1:?template}"
VERSION="${2:?cli_version}"
IMAGE="${3:-devcake/dev-${TEMPLATE}:latest}"
OUT="${4:-${DEVCAKE_RECEIPTS_DIR:-/tmp/harness_receipts}}"
if [[ -n "${5:-}" ]]; then
  DIGEST="$5"
elif [[ -n "${DEVCAKE_APP_DIGEST:-}" && "${DEVCAKE_APP_DIGEST}" != "DEVCAKE_APP_DIGEST_UNSET" ]]; then
  DIGEST="$DEVCAKE_APP_DIGEST"
else
  DIGEST="$(python3 scripts/app_digest.py)"
fi

mkdir -p "$OUT"
# Image user is uid 1000; receipts must be writable by that user.
chmod a+rwx "$OUT" 2>/dev/null || true

docker run --rm \
  --user 1000:1000 \
  -e DEVCAKE_RUN_ID=PROBE-1 \
  -e REDIS_URL=redis://127.0.0.1:6399/0 \
  -e REDIS_USER=probe \
  -e REDIS_PASSWORD=probe \
  -e PYTHONPATH=/opt/devcake-scripts \
  -v "$(pwd)/scripts:/opt/devcake-scripts:ro" \
  -v "${OUT}:/data/harness_receipts" \
  --entrypoint python \
  "${IMAGE}" \
  -m harness_probe.probe \
  --template "${TEMPLATE}" \
  --cli-version "${VERSION}" \
  --digest "${DIGEST}" \
  --out /data/harness_receipts
echo "probe receipt: ${OUT}/${TEMPLATE}@${VERSION}.json"
