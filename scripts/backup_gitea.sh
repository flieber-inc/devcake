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

# Resolve the output to a real host path and split dir/base (audit D5 #17):
# the old code concatenated the raw arg after /out inside the container, so an
# absolute or ../ path wrote into the EPHEMERAL container fs and vanished on
# --rm — exit 0, "wrote" message, no file. We bind-mount the target DIRECTORY
# and write the basename into it, so any host path the operator can reach works.
OUT_DIR="$(cd "$(dirname "$OUT")" && pwd)"
OUT_BASE="$(basename "$OUT")"

if docker ps --format '{{.Names}}' | grep -q gitea; then
  echo "NOTE: the gitea service is running — a live sqlite snapshot can tear." >&2
  echo "      For a guaranteed-consistent backup: docker compose stop gitea," >&2
  echo "      re-run this script, then docker compose up -d." >&2
fi

# --user "$(id -u):$(id -g)" so the credential tarball is owned by the invoking
# operator, not root, and umask 077 so it is not world-readable (audit D5 #19).
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$VOLUME":/src:ro -v "$OUT_DIR":/out alpine \
  sh -c "umask 077 && tar czf '/out/$OUT_BASE' -C /src ."
echo "wrote $OUT_DIR/$OUT_BASE — contains repo content AND Gitea's credential DB; store like a password export"
