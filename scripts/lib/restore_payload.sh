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
#      never be extracted into the data volume); no marker → refuse
#   3. move the current contents ASIDE (rename within the volume — no copy),
#      never delete them up front
#   4. extract; on failure put the aside BACK over $DST; only a clean extract
#      discards the aside
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
  echo "refusing: no DEVCAKE_BACKUP_KIND marker — not a DevCake backup" >&2
  exit 2
fi

# 3 — move aside, never delete-first; leftover .pre-restore-* dirs from an
# earlier failed attempt stay where they are instead of nesting. PID in the
# name so two restores in the same UTC second do not collide.
ASIDE="$DST/.pre-restore-$(date -u +%Y%m%d-%H%M%S)-$$"
mkdir "$ASIDE"
restore_aside() {
  # best-effort: put the previous tree back on its original paths; keep
  # older .pre-restore-* leftovers (same exclusion as the move-aside step)
  find "$DST" -mindepth 1 -maxdepth 1 \
    ! -name "$(basename "$ASIDE")" ! -name '.pre-restore-*' -exec rm -rf {} +
  find "$ASIDE" -mindepth 1 -maxdepth 1 -exec mv {} "$DST/" \;
  rmdir "$ASIDE" 2>/dev/null || true
}
trap 'restore_aside; echo "RESTORE FAILED — previous contents put back under $DST" >&2' EXIT
find "$DST" -mindepth 1 -maxdepth 1 ! -name '.pre-restore-*' -exec mv {} "$ASIDE/" \;

# 4 — extract, drop the marker from the restored tree, then discard the aside
tar xzf "$TAR" -C "$DST"
rm -f "$DST/DEVCAKE_BACKUP_KIND"
rm -rf "$ASIDE"
trap - EXIT
