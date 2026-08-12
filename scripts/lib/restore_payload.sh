#!/bin/sh
# Container-side payload for restore_data.sh / restore_gitea.sh — POSIX sh
# (busybox in the alpine container; any sh in the pytest restore drill, which
# overrides DST/IN_DIR to run it against tmp dirs without docker).
#
# Ordering is the safety property (2026-08-12 audit OPS-H1 — the old payload
# was `find /dst -delete && tar xzf`, so a corrupt tarball destroyed the only
# copy):
#   1. validate the ENTIRE archive structure before touching $DST
#   2. verify the DEVCAKE_BACKUP_KIND marker matches (a gitea tarball must
#      never be extracted into the data volume); markerless legacy backups
#      warn and proceed — they predate the marker and stay restorable
#   3. move the current contents ASIDE (rename within the volume — no copy),
#      never delete them up front
#   4. extract; only after a clean extract is the move-aside removed
# On any failure the previous contents survive inside the volume under the
# printed .pre-restore-* path.
#
# Env: TARFILE  tarball basename (rides env, never shell-interpolated)
#      KIND     expected kind: "data" | "gitea"
#      DST      target dir (default /dst), IN_DIR tarball dir (default /in)
set -eu
DST="${DST:-/dst}"
IN_DIR="${IN_DIR:-/in}"
TAR="$IN_DIR/$TARFILE"

# 1 — full structural preflight: list every member before any mutation
tar tzf "$TAR" > /dev/null

# 2 — kind check (marker is the first member, so both commands are cheap)
if tar tzf "$TAR" | grep -qx 'DEVCAKE_BACKUP_KIND'; then
  found="$(tar xzf "$TAR" -O DEVCAKE_BACKUP_KIND)"
  if [ "$found" != "$KIND" ]; then
    echo "refusing: tarball kind is '$found', expected '$KIND' — wrong backup for this volume" >&2
    exit 2
  fi
else
  echo "WARNING: no DEVCAKE_BACKUP_KIND marker (legacy backup) — kind unverified" >&2
fi

# 3 — move aside, never delete-first; leftover .pre-restore-* dirs from an
# earlier failed attempt stay where they are instead of nesting
ASIDE="$DST/.pre-restore-$(date -u +%Y%m%d-%H%M%S)"
mkdir "$ASIDE"
trap 'echo "RESTORE FAILED — previous contents preserved at $ASIDE (inside the volume); discard the partial extract and re-run" >&2' EXIT
find "$DST" -mindepth 1 -maxdepth 1 ! -name '.pre-restore-*' -exec mv {} "$ASIDE/" \;

# 4 — extract, drop the marker from the restored tree, then discard the aside
tar xzf "$TAR" -C "$DST"
rm -f "$DST/DEVCAKE_BACKUP_KIND"
rm -rf "$ASIDE"
trap - EXIT
