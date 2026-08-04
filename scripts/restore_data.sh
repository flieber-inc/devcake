#!/usr/bin/env bash
# Restore the app's /data volume from a backup_data.sh tarball. REFUSES while
# the app service is running, and REPLACES the volume's current contents.
#
# After restore: run files reflect the backup's moment — the app's boot
# reconciliation (docs/04 §6) orphans anything whose container no longer
# exists, so a stale-run backup self-heals on the next start.
#
# Usage: docker compose stop app && scripts/restore_data.sh <backup.tar.gz>
set -euo pipefail
cd "$(dirname "$0")/.."

VOLUME="${DEVCAKE_DATA_VOLUME:-devcake_devcake_data}"
TARBALL="${1:?usage: restore_data.sh <devcake-data-*.tar.gz>}"
[[ -f "$TARBALL" ]] || { echo "no such file: $TARBALL" >&2; exit 1; }

if docker ps --format '{{.Names}}' | grep -q devcake-app; then
  echo "refusing: the app service is running — docker compose stop app first" >&2
  exit 1
fi
docker volume inspect "$VOLUME" >/dev/null

TARDIR="$(cd "$(dirname "$TARBALL")" && pwd)"
TARFILE="$(basename "$TARBALL")"
# Basename via ENV VAR, never shell-interpolated (re-audit #5 pattern).
docker run --rm -e TARFILE="$TARFILE" \
  -v "$VOLUME":/dst -v "$TARDIR":/in:ro alpine \
  sh -c 'find /dst -mindepth 1 -delete && tar xzf "/in/$TARFILE" -C /dst'
echo "restored $VOLUME from $TARBALL — docker compose up -d to restart the stack"
