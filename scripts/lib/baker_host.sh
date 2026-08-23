#!/usr/bin/env bash
# Host-baker pidfile + post-launch liveness helpers (sourced by up.sh).
# Stdlib/sh only — never imported into the Python baker package.
# ADR-0034: one path for singular baker-launch diagnosis (not duplicated
# inline in callers that need the same messages).

# Prepare the baker pidfile for a new launch:
# - absent → nothing
# - live PID → kill it (restart) and print the restart line
# - stale / empty / garbage → first-class diagnostic + rm -f
#
# Usage: devcake_baker_prepare_pidfile <pidfile>
devcake_baker_prepare_pidfile() {
  local pidfile="$1"
  local old=""
  [[ -f "$pidfile" ]] || return 0
  old="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    echo "── restarting host baker (was pid ${old})"
    kill "$old" 2>/dev/null || true
    sleep 0.3
    return 0
  fi
  if [[ -n "$old" ]]; then
    echo "── stale host baker PID file (pid ${old} not running) — removing ${pidfile}"
  else
    echo "── stale host baker PID file (empty or unreadable) — removing ${pidfile}"
  fi
  rm -f "$pidfile"
}

# Confirm a detached baker stays alive and its log progresses for ~seconds.
# Requires PID alive AND logfile size growth past the pre-launch baseline
# (stdout/stderr → watch.log). Pass baseline_bytes from BEFORE launch so the
# startup print is not raced into the baseline.
# On failure prints launch command, pidfile path, last log lines; returns 1.
#
# Usage: devcake_baker_wait_liveness <pid> <logfile> <pidfile> <launch_cmd> \
#          [seconds] [baseline_bytes]
devcake_baker_wait_liveness() {
  local pid="$1" logfile="$2" pidfile="$3" launch_cmd="$4"
  local seconds="${5:-12}"
  local baseline="${6-}"
  local size=0 elapsed=0 step=2
  [[ -f "$logfile" ]] || : >"$logfile"
  if [[ -z "$baseline" ]]; then
    baseline=$(wc -c <"$logfile" 2>/dev/null | tr -d ' ' || echo 0)
  fi
  while [[ "$elapsed" -lt "$seconds" ]]; do
    sleep "$step"
    elapsed=$((elapsed + step))
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "── host baker died during launch confirmation (pid ${pid})" >&2
      echo "   launch: ${launch_cmd}" >&2
      echo "   pidfile: ${pidfile}" >&2
      echo "   last log lines (${logfile}):" >&2
      tail -n 40 "$logfile" 2>/dev/null >&2 || true
      echo "   tip: retry with ./up.sh --foreground-baker when the parent reaps detached children" >&2
      return 1
    fi
    size=$(wc -c <"$logfile" 2>/dev/null | tr -d ' ' || echo 0)
    if [[ "$size" -gt "$baseline" ]]; then
      echo "── host baker watching keep-set (pid ${pid} → ${logfile}; liveness confirmed)"
      return 0
    fi
  done
  echo "── host baker did not progress its log within ~${seconds}s (pid ${pid})" >&2
  echo "   launch: ${launch_cmd}" >&2
  echo "   pidfile: ${pidfile}" >&2
  echo "   last log lines (${logfile}):" >&2
  tail -n 40 "$logfile" 2>/dev/null >&2 || true
  echo "   tip: retry with ./up.sh --foreground-baker when the parent reaps detached children" >&2
  return 1
}
