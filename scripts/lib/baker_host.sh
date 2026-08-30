#!/usr/bin/env bash
# Host-baker pidfile + post-launch liveness helpers (sourced by up.sh).
# Stdlib/sh only — never imported into the Python baker package.
# ADR-0034: one path for singular baker-launch diagnosis (not duplicated
# inline in callers that need the same messages).

# Unit name installed under ~/.config/systemd/user/
DEVCAKE_BAKER_UNIT="${DEVCAKE_BAKER_UNIT:-devcake-baker.service}"

# Loud gap banner when user systemd cannot supervise the baker.
# Printed to stderr so operators cannot miss the unsupervised path.
devcake_baker_unsupervised_gap() {
  local reason="${1:-user systemd unavailable}"
  echo "══════════════════════════════════════════════════════════════" >&2
  echo "── UNSUPERVISED host baker (${reason})" >&2
  echo "   Falling back to nohup — the baker will NOT restart if this" >&2
  echo "   terminal/session dies, and nothing watches the pidfile." >&2
  echo "   Prefer a Linux host with a working user systemd bus, then:" >&2
  echo "     loginctl enable-linger \"\$USER\"   # survive logout" >&2
  echo "     ./up.sh                           # installs the user unit" >&2
  echo "══════════════════════════════════════════════════════════════" >&2
}

# Probe whether systemctl --user can manage units. Must not false-green:
# missing binary, failed show-environment, or inactive user bus → unavailable.
#
# Usage: devcake_baker_systemd_available  → 0 if usable, 1 otherwise
devcake_baker_systemd_available() {
  command -v systemctl >/dev/null 2>&1 || return 1
  # show-environment talks to the user manager; fails without a user bus.
  systemctl --user show-environment >/dev/null 2>&1 || return 1
  return 0
}

# Render the in-repo unit template into ~/.config/systemd/user/, write the
# EnvironmentFile (OO ingest + DEVCAKE_*), daemon-reload, enable --now.
# Pidfile is refreshed from MainPID after start (secondary liveness signal).
#
# Usage: devcake_baker_systemd_install <repo> <factory_dir> <logfile> <pidfile>
# Returns 0 on success (pidfile written); 1 on failure (caller falls back).
devcake_baker_systemd_install() {
  local repo="$1" factory_dir="$2" logfile="$3" pidfile="$4"
  local unit_src unit_dst env_file python_bin pythonpath unit_dir
  local rendered main_pid tries

  unit_src="${repo}/scripts/systemd/devcake-baker.service"
  [[ -f "$unit_src" ]] || {
    echo "── baker unit template missing: ${unit_src}" >&2
    return 1
  }
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  unit_dst="${unit_dir}/${DEVCAKE_BAKER_UNIT}"
  env_file="${factory_dir}/baker.env"
  mkdir -p "$unit_dir" "$factory_dir" || return 1

  python_bin="$(command -v python3)" || return 1
  pythonpath="${repo}/scripts:${repo}/app"

  # Env file holds secrets off the process argv / unit body (mode 0600).
  umask 077
  {
    printf 'DEVCAKE_OO_URL=%s\n' "${DEVCAKE_OO_URL:-http://127.0.0.1:5080}"
    printf 'DEVCAKE_TAG=%s\n' "${DEVCAKE_TAG:-latest}"
    printf 'DEVCAKE_FACTORY_LOG=%s\n' "$logfile"
    [[ -n "${OO_ORG:-}" ]] && printf 'OO_ORG=%s\n' "$OO_ORG"
    [[ -n "${OO_INGEST_EMAIL:-}" ]] && printf 'OO_INGEST_EMAIL=%s\n' "$OO_INGEST_EMAIL"
    [[ -n "${OO_INGEST_PASSWORD:-}" ]] && printf 'OO_INGEST_PASSWORD=%s\n' "$OO_INGEST_PASSWORD"
  } >"$env_file"
  chmod 600 "$env_file" || true

  rendered="$(
    sed \
      -e "s|@REPO@|${repo}|g" \
      -e "s|@PYTHON@|${python_bin}|g" \
      -e "s|@PYTHONPATH@|${pythonpath}|g" \
      -e "s|@ENV_FILE@|${env_file}|g" \
      -e "s|@WATCH_LOG@|${logfile}|g" \
      "$unit_src"
  )" || return 1
  printf '%s\n' "$rendered" >"$unit_dst" || return 1

  systemctl --user daemon-reload || return 1
  # enable --now refreshes an already-installed unit on subsequent ./up.sh.
  systemctl --user enable --now "$DEVCAKE_BAKER_UNIT" || \
    systemctl --user restart "$DEVCAKE_BAKER_UNIT" || return 1

  # Wait briefly for MainPID; write pidfile as the secondary signal.
  main_pid=""
  for tries in 1 2 3 4 5; do
    main_pid="$(systemctl --user show -p MainPID --value "$DEVCAKE_BAKER_UNIT" 2>/dev/null || true)"
    if [[ -n "$main_pid" && "$main_pid" != "0" ]]; then
      break
    fi
    sleep 0.4
  done
  if [[ -z "$main_pid" || "$main_pid" == "0" ]]; then
    echo "── systemd started ${DEVCAKE_BAKER_UNIT} but MainPID is unset" >&2
    return 1
  fi
  printf '%s\n' "$main_pid" >"$pidfile" || return 1
  echo "── host baker supervised by systemd --user (${DEVCAKE_BAKER_UNIT}, pid ${main_pid})"
  return 0
}

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
    ns="$(date +%s%N 2>/dev/null || date +%s)"
    outbox_json="$(DETAIL="$outbox_detail" python3 -c '
import json, os
from datetime import datetime, timezone
print(json.dumps({
    "ts": datetime.now(timezone.utc).isoformat(),
    "event": "launch_failed",
    # Total cap, character-safe (shell cut -c on multiline input caps per line)
    "detail": os.environ["DETAIL"][:2000],
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
