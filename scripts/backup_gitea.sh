#!/usr/bin/env bash
# Full-fidelity backup of the internal Gitea (ADR-0013 decision 5): the
# gitea_data volume holds repo content WITH git history/branches/PRs plus
# Gitea's sqlite DB — API snapshots would be lossy, so the volume IS the
# backup. (The app's git binary, added by ADR-0024, exists for the source
# mirrors only — devcake_mirrors is a disposable cache and is deliberately
# NOT part of the backup set, docs/13 §8.)
#
# The tarball contains every internal repo AND Gitea's credential DB:
# treat it exactly like a /data backup — a password-manager export.
#
# Usage: scripts/backup_gitea.sh [output.tar.gz]
# Default: ${XDG_DATA_HOME:-$HOME/.local/share}/devcake/backups/
# Override the default directory with DEVCAKE_BACKUP_DIR.
set -euo pipefail
cd "$(dirname "$0")/.."

# Digest-pinned (2026-08-12 audit OPS-M1): this container
# runs as root with RW on the output dir — check_image_pins.py enforces.
ALPINE_IMAGE="${DEVCAKE_ALPINE_IMAGE:-alpine:3.22@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce}"

VOLUME="${GITEA_VOLUME:-devcake_gitea_data}"
if [[ $# -gt 0 ]]; then
  OUT="$1"
else
  BACKUP_DIR="${DEVCAKE_BACKUP_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/devcake/backups}"
  install -d -m 700 "$BACKUP_DIR"
  OUT="$BACKUP_DIR/devcake-gitea-$(date -u +%Y%m%d-%H%M).tar.gz"
fi

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

# tar runs as ROOT (the container default) so it can read gitea's rootless
# 0700 data dirs (git/, custom/, jwt/private.pem — owned by the in-container
# uid 1000, unreadable to a host operator whose uid != 1000; re-audit #4).
# The payload (scripts/lib/backup_payload.sh — shared with the pytest restore
# drill) owns umask/marker/verify/chown; the basename rides an ENV VAR, never
# interpolated into the shell string (re-audit #5).
docker run --rm \
  -e OUT_BASE="$OUT_BASE" -e OWNER="$(id -u):$(id -g)" -e KIND=gitea \
  -v "$VOLUME":/src:ro -v "$OUT_DIR":/out \
  -v "$(pwd)/scripts/lib":/lib:ro \
  "$ALPINE_IMAGE" sh /lib/backup_payload.sh
echo "wrote $OUT_DIR/$OUT_BASE (verified readable) — contains repo content AND Gitea's credential DB; store like a password export"
