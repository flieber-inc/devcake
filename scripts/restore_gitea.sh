#!/usr/bin/env bash
# Restore the internal Gitea volume from a backup_gitea.sh tarball
# (ADR-0013 decision 5). REFUSES while the gitea service is running, and
# REPLACES the volume's current contents.
#
# Usage: docker compose stop gitea && scripts/restore_gitea.sh <backup.tar.gz>
set -euo pipefail
cd "$(dirname "$0")/.."

VOLUME="${GITEA_VOLUME:-devcake_gitea_data}"
TARBALL="${1:?usage: restore_gitea.sh <devcake-gitea-*.tar.gz>}"
[[ -f "$TARBALL" ]] || { echo "no such file: $TARBALL" >&2; exit 1; }

if docker ps --format '{{.Names}}' | grep -q gitea; then
  echo "refusing: the gitea service is running — docker compose stop gitea first" >&2
  exit 1
fi
docker volume inspect "$VOLUME" >/dev/null

TARDIR="$(cd "$(dirname "$TARBALL")" && pwd)"
TARFILE="$(basename "$TARBALL")"
# Basename rides an ENV VAR, never interpolated into the shell string — the
# same hardening backup_gitea.sh got in re-audit #5; the restore twin had
# kept the interpolation (2026-08 evaluation).
docker run --rm -e TARFILE="$TARFILE" \
  -v "$VOLUME":/dst -v "$TARDIR":/in:ro alpine \
  sh -c 'find /dst -mindepth 1 -delete && tar xzf "/in/$TARFILE" -C /dst'
echo "restored $VOLUME from $TARBALL — docker compose up -d to restart the stack"
