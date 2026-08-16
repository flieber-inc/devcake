#!/usr/bin/env bash
# Bake-verb embryo: compile a harness target, then write a drift receipt.
# up.sh does not call this yet (Slice 4). docker-images.yml does.
set -euo pipefail
cd "$(dirname "$0")/../.."

TARGET="${1:?bake target (grok-build|claude-code|codex|…)}"
PIN_ARG="${2:?Dockerfile ARG name (GROK_VERSION|…)}"
TAG="${DEVCAKE_TAG:-latest}"
OUT="${DEVCAKE_RECEIPTS_DIR:-/tmp/harness_receipts}"

PIN=$(sed -n "s/^ARG ${PIN_ARG}=//p" images/Dockerfile)
test -n "$PIN"

docker buildx bake "$TARGET"
bash scripts/harness_probe/host_probe.sh "$TARGET" "$PIN" \
  "devcake/dev-${TARGET}:${TAG}" "$OUT"
echo "bake+probe: ${TARGET}@${PIN} → ${OUT}/${TARGET}@${PIN}.json"
