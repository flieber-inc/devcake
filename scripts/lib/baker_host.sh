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

# Best-effort: push launch-failure evidence into the app /data mailbox so
# OpenObserve (outbox drain) and the admin dead-baker alert (bake status)
# see why the baker died. Never stamps heartbeat_at — liveness stays false.
# Failures of docker/python here must not change the caller's exit path.
#
# Usage: devcake_baker_emit_launch_failure <logfile> <launch_cmd>
devcake_baker_emit_launch_failure() {
  local logfile="$1" launch_cmd="$2"
  (
    set +e
    local excerpt first_line outbox_detail status_detail ns outbox_json status_json
    excerpt="$(tail -n 15 "$logfile" 2>/dev/null || true)"
    first_line="$(printf '%s\n' "$excerpt" | sed '/^[[:space:]]*$/d' | head -n 1)"
    [[ -n "$first_line" ]] || first_line="(no log output)"
    # Cap short status detail to one line / sane length.
    first_line="$(printf '%s' "$first_line" | tr '\n' ' ' | cut -c1-200)"
    status_detail="host baker died at launch: ${first_line}"
    outbox_detail="$(printf 'launch: %s\n\n--- watch.log (last 15 lines) ---\n%s' \
      "$launch_cmd" "$excerpt")"
    outbox_detail="$(printf '%s' "$outbox_detail" | cut -c1-2000)"
    ns="$(date +%s%N 2>/dev/null || date +%s)"
    outbox_json="$(DETAIL="$outbox_detail" python3 -c '
import json, os
from datetime import datetime, timezone
print(json.dumps({
    "ts": datetime.now(timezone.utc).isoformat(),
    "event": "launch_failed",
    "detail": os.environ["DETAIL"],
}, separators=(",", ":")))
' 2>/dev/null)" || return 0
    status_json="$(DETAIL="$status_detail" python3 -c '
import json, os
print(json.dumps({
    "state": "error",
    "detail": os.environ["DETAIL"],
}, separators=(",", ":")))
' 2>/dev/null)" || return 0
    [[ -n "$outbox_json" && -n "$status_json" ]] || return 0
    docker compose exec -T app mkdir -p /data/harness_outbox >/dev/null 2>&1 || true
    printf '%s\n' "$outbox_json" \
      | docker compose exec -T app tee \
          "/data/harness_outbox/${ns}-launch_failed.jsonl" >/dev/null 2>&1 || true
    printf '%s\n' "$status_json" \
      | docker compose exec -T app tee /data/harness_bake_status.json >/dev/null 2>&1 || true
  ) || true
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
      devcake_baker_emit_launch_failure "$logfile" "$launch_cmd"
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
  devcake_baker_emit_launch_failure "$logfile" "$launch_cmd"
  return 1
}
