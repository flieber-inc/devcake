#!/usr/bin/env bash
# Backup of the app's /data volume — THE primary backup target (docs/13 §8,
# 2026-08 evaluation: the gitea pair existed, the devcake_data twin did not).
#
# The tarball holds EVERY operator secret in plaintext (GUI secret store,
# ADR-0011: PMO keys, forge PATs, model credentials, profile snapshots) plus
# run state, config, and the audit log: treat it strictly like a
# password-manager export.
#
# Consistency: the app writes /data continuously (run files, events.jsonl).
# A live snapshot is crash-consistent (atomic tmp+rename writes throughout),
# but for a guaranteed-quiet backup: docker compose stop app, run this, then
# docker compose up -d.
#
# Usage: scripts/backup_data.sh [output.tar.gz]
# Default: ${XDG_DATA_HOME:-$HOME/.local/share}/devcake/backups/
# Override the default directory with DEVCAKE_BACKUP_DIR.
set -euo pipefail
cd "$(dirname "$0")/.."

VOLUME="${DEVCAKE_DATA_VOLUME:-devcake_devcake_data}"
if [[ $# -gt 0 ]]; then
  OUT="$1"
else
  BACKUP_DIR="${DEVCAKE_BACKUP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/devcake/backups}"
  install -d -m 700 "$BACKUP_DIR"
  OUT="$BACKUP_DIR/devcake-data-$(date -u +%Y%m%d-%H%M).tar.gz"
fi

docker volume inspect "$VOLUME" >/dev/null

# Bind-mount the target DIRECTORY and write the basename into it (audit D5
# #17 — a raw path after /out inside the container wrote into the ephemeral
# fs and vanished on --rm).
OUT_DIR="$(cd "$(dirname "$OUT")" && pwd)"
OUT_BASE="$(basename "$OUT")"

if docker ps --format '{{.Names}}' | grep -q devcake-app; then
  echo "NOTE: the app is running — the snapshot is crash-consistent only." >&2
  echo "      For a guaranteed-quiet backup: docker compose stop app," >&2
  echo "      re-run this script, then docker compose up -d." >&2
fi

# tar as root (container default) so 0600/0700 secret files under
# /data/secrets are readable regardless of the operator's host uid; basename
# rides an ENV VAR (never shell-interpolated — re-audit #5); umask 077 keeps
# the tarball non-world-readable; the chown hands it to the invoker (audit
# D5 #19).
docker run --rm \
  -e OUT_BASE="$OUT_BASE" -e OWNER="$(id -u):$(id -g)" \
  -v "$VOLUME":/src:ro -v "$OUT_DIR":/out alpine \
  sh -c 'umask 077 && tar czf "/out/$OUT_BASE" -C /src . && chown "$OWNER" "/out/$OUT_BASE"'
echo "wrote $OUT_DIR/$OUT_BASE — contains EVERY operator secret in plaintext; store like a password export"
