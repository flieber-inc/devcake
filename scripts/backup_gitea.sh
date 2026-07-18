#!/usr/bin/env bash
# Full-fidelity backup of the internal Gitea (ADR-0013 decision 5): the
# gitea_data volume holds repo content WITH git history/branches/PRs plus
# Gitea's sqlite DB — the app container has no git binary and API snapshots
# would be lossy, so the volume IS the backup.
#
# The tarball contains every internal repo AND Gitea's credential DB:
# treat it exactly like a /data backup — a password-manager export.
#
# Usage: scripts/backup_gitea.sh [output.tar.gz]
set -euo pipefail
cd "$(dirname "$0")/.."

VOLUME="${GITEA_VOLUME:-devcake_gitea_data}"
OUT="${1:-devcake-gitea-$(date -u +%Y%m%d-%H%M).tar.gz}"

docker volume inspect "$VOLUME" >/dev/null

if docker ps --format '{{.Names}}' | grep -q gitea; then
  echo "NOTE: the gitea service is running — a live sqlite snapshot can tear." >&2
  echo "      For a guaranteed-consistent backup: docker compose stop gitea," >&2
  echo "      re-run this script, then docker compose up -d." >&2
fi

docker run --rm -v "$VOLUME":/src:ro -v "$(pwd)":/out alpine \
  tar czf "/out/$OUT" -C /src .
echo "wrote $OUT — contains repo content AND Gitea's credential DB; store like a password export"
