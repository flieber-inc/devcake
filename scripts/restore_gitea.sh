#!/usr/bin/env bash
# Restore the internal Gitea volume from a backup_gitea.sh tarball
# (ADR-0013 decision 5). REFUSES while the gitea service is running, and
# REPLACES the volume's current contents — but never destructively: the
# payload validates the whole archive and its kind marker first, moves the
# current contents aside, and deletes the aside only after a clean extract
# (2026-08-12 audit OPS-H1; on failure the previous tree is put back on its
# original paths under the volume root — prior .pre-restore-* leftovers are
# kept, not nested or wiped).
#
# Usage: docker compose stop gitea && scripts/restore_gitea.sh <backup.tar.gz>
set -euo pipefail
cd "$(dirname "$0")/.."

# Digest-pinned (OPS-M1): root + RW on the gitea volume.
# GNU tar is required: BusyBox treats multiple -C options globally.
# DEVCAKE_ALPINE_IMAGE remains a legacy override; custom images need GNU tar.
BACKUP_IMAGE="${DEVCAKE_BACKUP_IMAGE:-${DEVCAKE_ALPINE_IMAGE:-debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171}}"

VOLUME="${GITEA_VOLUME:-devcake_gitea_data}"
TARBALL="${1:?usage: restore_gitea.sh <devcake-gitea-*.tar.gz>}"
[[ -f "$TARBALL" ]] || { echo "no such file: $TARBALL" >&2; exit 1; }

if [ -n "$(docker compose ps -q gitea 2>/dev/null)" ]; then
  echo "refusing: the gitea service is running — docker compose stop gitea first" >&2
  exit 1
fi
docker volume inspect "$VOLUME" >/dev/null

TARDIR="$(cd "$(dirname "$TARBALL")" && pwd)"
TARFILE="$(basename "$TARBALL")"
# Basename rides an ENV VAR, never interpolated into the shell string (re-
# audit #5). The payload (scripts/lib/restore_payload.sh — shared with the
# pytest restore drill) owns preflight/kind-check/move-aside/extract ordering.
docker run --rm -e TARFILE="$TARFILE" -e KIND=gitea \
  -v "$VOLUME":/dst -v "$TARDIR":/in:ro \
  -v "$(pwd)/scripts/lib":/devcake-scripts:ro \
  "$BACKUP_IMAGE" sh /devcake-scripts/restore_payload.sh
echo "restored $VOLUME from $TARBALL — docker compose up -d to restart the stack"
