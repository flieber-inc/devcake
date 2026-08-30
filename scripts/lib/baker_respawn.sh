#!/usr/bin/env bash
# Degraded host-baker supervisor: flock-guarded respawn loop.
#
# Used when neither systemd --user nor launchd is available (e.g. WSL2 with
# systemd disabled). The baker itself still holds .factory/watch.lock via
# fcntl so a double-installed supervisor cannot double-run it.
#
# This script's flock (.factory/watch.respawn.lock) keeps a single respawn
# supervisor. Nothing restarts THIS supervisor if you kill it — that is the
# degraded gap; prefer Linux+systemd or macOS+launchd when possible.
#
# Usage: baker_respawn.sh <repo> <factory_dir> <logfile> <pidfile>
set -euo pipefail

REPO="${1:?repo path required}"
FACTORY_DIR="${2:?factory_dir required}"
LOGFILE="${3:?logfile required}"
PIDFILE="${4:?pidfile required}"

LOCKFILE="${FACTORY_DIR}/watch.respawn.lock"
ENV_FILE="${FACTORY_DIR}/baker.env"
PYTHON_BIN="$(command -v python3)" || {
  echo "devcake baker-respawn: python3 not found" >&2
  exit 1
}

mkdir -p "$FACTORY_DIR"
: >>"$LOGFILE"

# Exclusive supervisor lock (Linux flock(1); this path is Linux-without-systemd).
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "devcake baker-respawn: another respawn supervisor holds ${LOCKFILE} — exiting" >&2
  exit 0
fi
echo "$$" >"${FACTORY_DIR}/watch.respawn.pid"

export PYTHONUNBUFFERED=1
export PYTHONPATH="${REPO}/scripts:${REPO}/app"
export DEVCAKE_FACTORY_DIR="$FACTORY_DIR"
export DEVCAKE_FACTORY_LOG="$LOGFILE"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

cd "$REPO"
BACKOFF=2
echo "devcake baker-respawn: supervising baker (repo=${REPO})" >>"$LOGFILE"
while true; do
  echo "devcake baker-respawn: starting python -m dev_factory" >>"$LOGFILE"
  "$PYTHON_BIN" -m dev_factory >>"$LOGFILE" 2>&1 &
  baker_pid=$!
  printf '%s\n' "$baker_pid" >"$PIDFILE"
  set +e
  wait "$baker_pid"
  rc=$?
  set -e
  echo "devcake baker-respawn: baker exited rc=${rc}; restarting in ${BACKOFF}s" >>"$LOGFILE"
  sleep "$BACKOFF"
  if [[ "$BACKOFF" -lt 30 ]]; then
    BACKOFF=$((BACKOFF * 2))
    [[ "$BACKOFF" -gt 30 ]] && BACKOFF=30
  fi
done
