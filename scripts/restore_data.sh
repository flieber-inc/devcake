#!/usr/bin/env bash
# Restore the app's /data volume from a backup_data.sh tarball. REFUSES while
# the app service is running, and REPLACES the volume's current contents —
# but never destructively: the payload validates the whole archive and its
# kind marker first, moves the current contents aside, and deletes the aside
# only after a clean extract (2026-08-12 audit OPS-H1; on failure the
# previous contents survive at the printed .pre-restore-* path).
#
# After restore: run files reflect the backup's moment — the app's boot
# reconciliation (docs/04 §6) orphans anything whose container no longer
# exists, so a stale-run backup self-heals on the next start.
#
# Usage: docker compose stop app && scripts/restore_data.sh <backup.tar.gz>
set -euo pipefail
cd "$(dirname "$0")/.."

# Digest-pinned (ISSUES #29 rider, OPS-M1): root + RW on the data volume.
ALPINE_IMAGE="${DEVCAKE_ALPINE_IMAGE:-alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce}"

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
# Basename via ENV VAR, never shell-interpolated (re-audit #5 pattern). The
# payload (scripts/lib/restore_payload.sh — shared with the pytest restore
# drill) owns preflight/kind-check/move-aside/extract ordering.
docker run --rm -e TARFILE="$TARFILE" -e KIND=data \
  -v "$VOLUME":/dst -v "$TARDIR":/in:ro \
  -v "$(pwd)/scripts/lib":/lib:ro \
  "$ALPINE_IMAGE" sh /lib/restore_payload.sh
echo "restored $VOLUME from $TARBALL (verified before extract) — docker compose up -d to restart the stack"
