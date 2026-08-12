#!/bin/sh
# Container-side payload for backup_data.sh / backup_gitea.sh — POSIX sh
# (busybox in the alpine container; any sh in the pytest restore drill, which
# overrides SRC/OUT_DIR to run it against tmp dirs without docker).
#
# Ordering is the safety property (2026-08-12 audit OPS-H1): the tarball is
# structurally VERIFIED (full tzf walk) in the same run that wrote it, so a
# truncated/corrupt archive fails on backup day, not on restore day. A
# DEVCAKE_BACKUP_KIND marker (first archive member) records what volume kind
# the tarball holds; restore_payload.sh refuses a mismatched kind.
#
# Write is tmp+rename (2026-08-12 leftover): in-place czf to $OUT_BASE
# destroyed a previously verified same-name archive if tar died mid-write.
#
# Env: OUT_BASE  tarball basename (rides env, never shell-interpolated —
#                re-audit #5)
#      OWNER    uid:gid to hand the tarball to (audit D5 #19)
#      KIND     "data" | "gitea" — written into the marker
#      SRC      source dir (default /src), OUT_DIR output dir (default /out)
set -eu
SRC="${SRC:-/src}"
OUT_DIR="${OUT_DIR:-/out}"
umask 077

KIND_DIR="$(mktemp -d)"
printf '%s\n' "$KIND" > "$KIND_DIR/DEVCAKE_BACKUP_KIND"

PARTIAL="$OUT_DIR/$OUT_BASE.partial"
rm -f "$PARTIAL"
tar czf "$PARTIAL" -C "$KIND_DIR" DEVCAKE_BACKUP_KIND -C "$SRC" .

# verify: walk the ENTIRE archive — a backup that cannot be listed today
# could not have been restored tomorrow (write-only-backup audit finding)
tar tzf "$PARTIAL" > /dev/null

mv "$PARTIAL" "$OUT_DIR/$OUT_BASE"
chown "$OWNER" "$OUT_DIR/$OUT_BASE"
