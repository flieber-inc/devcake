#!/usr/bin/env bash
# Host-baker pidfile + post-launch liveness helpers (sourced by devcake up).
# Stdlib/sh only — never imported into the Python baker package.
# ADR-0034: one path for singular baker-launch diagnosis (not duplicated
# inline in callers that need the same messages).
#
# Supervisor routing (founder constraint — Linux AND macOS):
#   darwin  → launchd LaunchAgent (KeepAlive / RunAtLoad)
#   linux + systemd --user → user unit (on-failure restart policy)
#   else    → flock-guarded respawn loop (DEGRADED — never bare nohup baker)

# Unit / label names
DEVCAKE_BAKER_UNIT="${DEVCAKE_BAKER_UNIT:-devcake-baker.service}"
DEVCAKE_BAKER_LAUNCHD_LABEL="${DEVCAKE_BAKER_LAUNCHD_LABEL:-com.devcake.baker}"

# Platform: darwin | linux | other
devcake_baker_platform() {
  local u
  u="$(uname -s 2>/dev/null || echo unknown)"
  case "$u" in
    Darwin) echo darwin ;;
    Linux)  echo linux ;;
    *)      echo other ;;
  esac
}

# Loud banner when falling back to the flock-guarded respawn loop.
# Printed to stderr so operators cannot miss the degraded path.
# When reason names enable-linger (no-user-session path), print the sharper
# remedy by name so WSL-with-systemd operators are not left on the generic tip.
devcake_baker_degraded_gap() {
  local reason="${1:-neither systemd nor launchd available}"
  echo "══════════════════════════════════════════════════════════════" >&2
  echo "── DEGRADED host baker supervision (${reason})" >&2
  echo "   Installing a flock-guarded respawn loop — it restarts the" >&2
  echo "   baker on exit, but nothing restarts the respawn supervisor" >&2
  echo "   itself if YOU kill it. Prefer:" >&2
  echo "     Linux:  systemd --user + loginctl enable-linger \"\$USER\"" >&2
  echo "     macOS:  launchd LaunchAgent (login session / Docker Desktop)" >&2
  echo "   Then re-run devcake up to install the native supervisor." >&2
  case "$reason" in
    *enable-linger*)
      echo "   Sharper fix (systemd present, no user session manager):" >&2
      echo "     loginctl enable-linger \"\$USER\"" >&2
      echo "     then restart your session (or the host), then devcake up" >&2
      echo "     — devcake up will install the native systemd user unit." >&2
      ;;
  esac
  echo "══════════════════════════════════════════════════════════════" >&2
}

# Backward-compatible alias (older tests / docs may still name this).
devcake_baker_unsupervised_gap() {
  devcake_baker_degraded_gap "$@"
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

# systemctl binary exists but the user session manager is unreachable
# (typical: WSL systemd=true without linger / no user bus).
# Usage: → 0 when that is the case, 1 otherwise (incl. no systemctl at all).
devcake_baker_systemd_user_session_missing() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user show-environment >/dev/null 2>&1 && return 1
  return 0
}

# Linux DEGRADED reason for devcake up fallthrough. Distinguishes:
#   install/start failed | no user session (linger) | binary unavailable.
# Echoes one reason line on stdout.
devcake_baker_linux_degraded_reason() {
  if devcake_baker_systemd_available; then
    echo "systemd user unit install/start failed"
  elif devcake_baker_systemd_user_session_missing; then
    echo "systemd present but no user session — run: loginctl enable-linger \"\$USER\", restart session/host, then devcake up"
  else
    echo "user systemd unavailable"
  fi
}

# macOS launchd is available when we are on Darwin and launchctl exists.
devcake_baker_launchd_available() {
  [[ "$(devcake_baker_platform)" == "darwin" ]] || return 1
  command -v launchctl >/dev/null 2>&1 || return 1
  return 0
}

# Stop a prior flock-respawn supervisor (if any) so it cannot race a native
# systemd/launchd install. Best-effort — missing pidfile is fine.
devcake_baker_stop_respawn_supervisor() {
  local factory_dir="$1"
  local respawn_pidfile="${factory_dir}/watch.respawn.pid"
  local old=""
  [[ -f "$respawn_pidfile" ]] || return 0
  old="$(cat "$respawn_pidfile" 2>/dev/null || true)"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    echo "── stopping degraded respawn supervisor (pid ${old}) before native install"
    kill "$old" 2>/dev/null || true
    sleep 0.3
  fi
  rm -f "$respawn_pidfile"
}

# Shared: write .factory/baker.env (mode 0600) with OO ingest + DEVCAKE_*.
# Usage: devcake_baker_write_env_file <factory_dir> <logfile>
devcake_baker_write_env_file() {
  local factory_dir="$1" logfile="$2" env_file
  env_file="${factory_dir}/baker.env"
  mkdir -p "$factory_dir" || return 1
  umask 077
  {
    printf 'DEVCAKE_OO_URL=%s\n' "${DEVCAKE_OO_URL:-http://127.0.0.1:5080}"
    printf 'DEVCAKE_TAG=%s\n' "${DEVCAKE_TAG:-latest}"
    printf 'DEVCAKE_FACTORY_LOG=%s\n' "$logfile"
    printf 'DEVCAKE_FACTORY_DIR=%s\n' "$factory_dir"
    [[ -n "${OO_ORG:-}" ]] && printf 'OO_ORG=%s\n' "$OO_ORG"
    [[ -n "${OO_INGEST_EMAIL:-}" ]] && printf 'OO_INGEST_EMAIL=%s\n' "$OO_INGEST_EMAIL"
    [[ -n "${OO_INGEST_PASSWORD:-}" ]] && printf 'OO_INGEST_PASSWORD=%s\n' "$OO_INGEST_PASSWORD"
  } >"$env_file"
  chmod 600 "$env_file" || true
}

# Resolve the `devcake` console script (uv tool / pipx userspace install).
# Prints the absolute path on success; returns 1 with a loud tip on failure.
# Usage: devcake_baker_resolve_cli
# Resolve the full baker command for launchers/units: the installed CLI
# (`<devcake> baker run`) or — transition fallback per ADR-0038 Decision 5 —
# the deprecated `<python3> -m dev_factory`, with a loud upgrade tip. Always
# succeeds; prints the command. Usage: devcake_baker_resolve_entry
devcake_baker_resolve_entry() {
  local bin=""
  if bin="$(command -v devcake 2>/dev/null)"; then
    printf '%s baker run' "$bin"
    return 0
  fi
  echo "── \`devcake\` not on PATH — using deprecated \`python3 -m dev_factory\` (transition fallback)." >&2
  echo "   Install the host CLI to retire this path: uv tool install <repo>  (ADR-0038 Decision 5/6)." >&2
  printf '%s -m dev_factory' "$(command -v python3)"
}

devcake_baker_resolve_cli() {
  local bin=""
  bin="$(command -v devcake)" || {
    echo "── \`devcake\` not on PATH — install the host CLI before starting the baker:" >&2
    echo "   uv tool install /path/to/devcake   # or: pipx install /path/to/devcake" >&2
    echo "   then re-run devcake up (ADR-0038 Decision 5/6)" >&2
    return 1
  }
  printf '%s' "$bin"
}

# Shared: write .factory/baker-run.sh that sources baker.env and execs the baker.
# Usage: devcake_baker_write_runner <repo> <factory_dir> <logfile>
devcake_baker_write_runner() {
  local repo="$1" factory_dir="$2" logfile="$3"
  local runner pythonpath
  runner="${factory_dir}/baker-run.sh"
  local baker_exec=""
  baker_exec="$(devcake_baker_resolve_entry)"
  pythonpath="${repo}/scripts:${repo}/app"
  mkdir -p "$factory_dir" || return 1
  cat >"$runner" <<EOF
#!/usr/bin/env bash
# Generated by devcake up — sources baker.env then execs the baker entry
# (devcake baker run, or the deprecated dev_factory transition fallback).
set -euo pipefail
cd "${repo}"
FACTORY_DIR="${factory_dir}"
ENV_FILE="\${FACTORY_DIR}/baker.env"
if [[ -f "\$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "\$ENV_FILE"
  set +a
fi
export PYTHONUNBUFFERED=1
export PYTHONPATH="${pythonpath}"
export DEVCAKE_FACTORY_DIR="\${FACTORY_DIR}"
export DEVCAKE_FACTORY_LOG="${logfile}"
exec ${baker_exec}
EOF
  chmod 700 "$runner" || return 1
}

# Render the in-repo unit template into ~/.config/systemd/user/, write the
# EnvironmentFile (OO ingest + DEVCAKE_*), daemon-reload, enable --now.
# Pidfile is refreshed from MainPID after start (secondary liveness signal).
#
# Usage: devcake_baker_systemd_install <repo> <factory_dir> <logfile> <pidfile>
# Returns 0 on success (pidfile written); 1 on failure (caller falls back).
devcake_baker_systemd_install() {
  local repo="$1" factory_dir="$2" logfile="$3" pidfile="$4"
  local unit_src unit_dst env_file devcake_bin pythonpath unit_dir
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

  local baker_exec=""
  baker_exec="$(devcake_baker_resolve_entry)"
  pythonpath="${repo}/scripts:${repo}/app"

  devcake_baker_stop_respawn_supervisor "$factory_dir"
  devcake_baker_write_env_file "$factory_dir" "$logfile" || return 1
  # Also export DEVCAKE_FACTORY_DIR into the unit via EnvironmentFile.

  rendered="$(
    sed \
      -e "s|@REPO@|${repo}|g" \
      -e "s|@BAKER_EXEC@|${baker_exec}|g" \
      -e "s|@PYTHONPATH@|${pythonpath}|g" \
      -e "s|@ENV_FILE@|${env_file}|g" \
      -e "s|@WATCH_LOG@|${logfile}|g" \
      "$unit_src"
  )" || return 1
  printf '%s\n' "$rendered" >"$unit_dst" || return 1

  systemctl --user daemon-reload || return 1
  # enable --now refreshes an already-installed unit on subsequent devcake up.
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
  devcake_baker_displace_orphans "$factory_dir" "$main_pid"
  return 0
}

# Install/refresh the macOS LaunchAgent and kickstart it.
# Usage: devcake_baker_launchd_install <repo> <factory_dir> <logfile> <pidfile>
devcake_baker_launchd_install() {
  local repo="$1" factory_dir="$2" logfile="$3" pidfile="$4"
  local plist_src plist_dst agents_dir runner uid domain tries
  local old_pid="" new_pid=""

  plist_src="${repo}/scripts/launchd/com.devcake.baker.plist"
  [[ -f "$plist_src" ]] || {
    echo "── baker LaunchAgent template missing: ${plist_src}" >&2
    return 1
  }
  agents_dir="${HOME}/Library/LaunchAgents"
  plist_dst="${agents_dir}/${DEVCAKE_BAKER_LAUNCHD_LABEL}.plist"
  runner="${factory_dir}/baker-run.sh"
  mkdir -p "$agents_dir" "$factory_dir" || return 1

  devcake_baker_stop_respawn_supervisor "$factory_dir"
  devcake_baker_write_env_file "$factory_dir" "$logfile" || return 1
  devcake_baker_write_runner "$repo" "$factory_dir" "$logfile" || return 1

  # Remember prior pid so we can detect a fresh write after kickstart.
  [[ -f "$pidfile" ]] && old_pid="$(cat "$pidfile" 2>/dev/null || true)"

  sed \
    -e "s|@REPO@|${repo}|g" \
    -e "s|@RUNNER@|${runner}|g" \
    -e "s|@WATCH_LOG@|${logfile}|g" \
    "$plist_src" >"$plist_dst" || return 1
  chmod 600 "$plist_dst" || true

  uid="$(id -u)"
  domain="gui/${uid}"

  # Prefer modern bootstrap/bootout; fall back to load -w for older macOS.
  if launchctl print "${domain}/${DEVCAKE_BAKER_LAUNCHD_LABEL}" >/dev/null 2>&1; then
    launchctl bootout "${domain}/${DEVCAKE_BAKER_LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  else
    launchctl unload -w "$plist_dst" >/dev/null 2>&1 || true
  fi
  if ! launchctl bootstrap "$domain" "$plist_dst" 2>/dev/null; then
    launchctl load -w "$plist_dst" || return 1
  fi
  launchctl enable "${domain}/${DEVCAKE_BAKER_LAUNCHD_LABEL}" >/dev/null 2>&1 || true
  launchctl kickstart -k "${domain}/${DEVCAKE_BAKER_LAUNCHD_LABEL}" >/dev/null 2>&1 \
    || launchctl start "$DEVCAKE_BAKER_LAUNCHD_LABEL" >/dev/null 2>&1 \
    || true

  # Baker writes pidfile via acquire_baker_singleton; wait for it to change.
  for tries in 1 2 3 4 5 6 7 8 9 10; do
    if [[ -f "$pidfile" ]]; then
      new_pid="$(cat "$pidfile" 2>/dev/null || true)"
      if [[ -n "$new_pid" && "$new_pid" != "0" && "$new_pid" != "$old_pid" ]]; then
        if kill -0 "$new_pid" 2>/dev/null; then
          echo "── host baker supervised by launchd (${DEVCAKE_BAKER_LAUNCHD_LABEL}, pid ${new_pid})"
          devcake_baker_displace_orphans "$factory_dir" "$new_pid"
          return 0
        fi
      fi
      # Same pid but alive after re-kick is also fine (fast restart).
      if [[ -n "$new_pid" && "$new_pid" != "0" ]] && kill -0 "$new_pid" 2>/dev/null; then
        if [[ "$tries" -ge 3 ]]; then
          echo "── host baker supervised by launchd (${DEVCAKE_BAKER_LAUNCHD_LABEL}, pid ${new_pid})"
          devcake_baker_displace_orphans "$factory_dir" "$new_pid"
          return 0
        fi
      fi
    fi
    sleep 0.4
  done
  echo "── launchd started ${DEVCAKE_BAKER_LAUNCHD_LABEL} but pidfile is unset" >&2
  return 1
}

# Install the flock-guarded respawn loop (DEGRADED path). Detaches the
# supervisor with nohup — the supervision mechanism is the loop + flock,
# not bare nohup of the baker.
#
# Usage: devcake_baker_respawn_install <repo> <factory_dir> <logfile> <pidfile>
devcake_baker_respawn_install() {
  local repo="$1" factory_dir="$2" logfile="$3" pidfile="$4"
  local respawn_src respawn_pidfile supervisor_pid old="" tries new_pid=""

  respawn_src="${repo}/scripts/lib/baker_respawn.sh"
  [[ -f "$respawn_src" ]] || {
    echo "── baker respawn script missing: ${respawn_src}" >&2
    return 1
  }
  chmod +x "$respawn_src" 2>/dev/null || true
  mkdir -p "$factory_dir" || return 1
  devcake_baker_write_env_file "$factory_dir" "$logfile" || return 1

  # Stop a prior respawn supervisor if we know its pid.
  respawn_pidfile="${factory_dir}/watch.respawn.pid"
  if [[ -f "$respawn_pidfile" ]]; then
    old="$(cat "$respawn_pidfile" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      echo "── restarting degraded respawn supervisor (was pid ${old})"
      kill "$old" 2>/dev/null || true
      sleep 0.3
    fi
    rm -f "$respawn_pidfile"
  fi

  # Detach the supervisor only — baker restarts are the loop's job.
  nohup bash "$respawn_src" "$repo" "$factory_dir" "$logfile" "$pidfile" \
    >>"$logfile" 2>&1 &
  supervisor_pid=$!
  # Wait for baker pidfile (written by the loop after starting python).
  for tries in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.4
    if ! kill -0 "$supervisor_pid" 2>/dev/null; then
      echo "── respawn supervisor died at launch (pid ${supervisor_pid})" >&2
      return 1
    fi
    new_pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$new_pid" && "$new_pid" != "0" ]] && kill -0 "$new_pid" 2>/dev/null; then
      echo "── host baker supervised by flock respawn loop (supervisor ${supervisor_pid}, baker ${new_pid})"
      # Keep the supervised baker; only when its pid is known (not the pending case).
      devcake_baker_displace_orphans "$factory_dir" "$new_pid"
      return 0
    fi
  done
  # Supervisor is up; baker may still be starting — accept supervisor pid as signal.
  # Skip post-install displace here: keep_pid would be the supervisor, and the
  # pre-start sweep already cleared leftovers; killing a just-started baker would race.
  if kill -0 "$supervisor_pid" 2>/dev/null; then
    printf '%s\n' "$supervisor_pid" >"$pidfile" || true
    echo "── host baker respawn supervisor running (pid ${supervisor_pid}; baker pid pending)"
    return 0
  fi
  return 1
}

# True when cmdline looks like a host baker process:
#   - legacy: `python … -m dev_factory` / `-m dev_factory.…`
#   - CLI:    `devcake baker run` (absolute or on PATH; optional python wrapper)
# and is not the respawn supervisor script. Both forms must match so in-place
# upgrade can displace leftovers AND find newly supervised children
# (ADR-0038 Decision 5).
devcake_baker_cmdline_is_module() {
  local c="$1"
  case "$c" in
    *baker_respawn*) return 1 ;;
  esac
  case "$c" in
    *-m\ dev_factory\ *|*-m\ dev_factory|*-m\ dev_factory.*) return 0 ;;
    *devcake\ baker\ run\ *|*devcake\ baker\ run) return 0 ;;
  esac
  return 1
}

# Best-effort absolute cwd for a pid.
# Linux: /proc/<pid>/cwd. Darwin / no-/proc: lsof -d cwd (when available).
# Prints the path or empty; never fails the caller.
# Usage: devcake_baker_process_cwd <pid>
devcake_baker_process_cwd() {
  local pid="$1" cwd=""
  if [[ -L "/proc/${pid}/cwd" ]]; then
    cwd="$(readlink "/proc/${pid}/cwd" 2>/dev/null || true)"
  elif command -v lsof >/dev/null 2>&1; then
    # lsof -Fn: lines like "p<pid>" / "n/path". Take the first n-record.
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null \
      | sed -n 's/^n//p' | head -n 1)" || true
  fi
  printf '%s' "$cwd"
}

# True when a candidate baker targets factory_abs.
# When DEVCAKE_FACTORY_DIR is non-empty it is decisive: equal → match,
# unequal → no match (do not fall through to cwd/cmdline). When unset/empty,
# allow cwd (= repo whose .factory is factory_abs) or cmdline containing
# factory_abs — true pre-env leftovers.
# Usage: devcake_baker_targets_factory <cmdline> <env_factory> <cwd> <factory_abs> <repo_abs>
devcake_baker_targets_factory() {
  local c="$1" ef="$2" cw="$3" factory_abs="$4" repo_abs="$5"
  local ef_abs=""
  if [[ -n "$ef" ]]; then
    ef_abs="$(cd "$ef" 2>/dev/null && pwd -P)" || ef_abs="$ef"
    [[ "$ef_abs" == "$factory_abs" ]] && return 0
    return 1
  fi
  [[ -n "$cw" && -n "$repo_abs" && "$cw" == "$repo_abs" ]] && return 0
  case "$c" in
    *"$factory_abs"*) return 0 ;;
  esac
  return 1
}

# Enumerate host baker processes targeting THIS factory dir.
# Prints "pid age" lines (age = ps etime, or "unknown"). Matching rule:
#   argv looks like `python … -m dev_factory` / `-m dev_factory.…` OR
#   `devcake baker run` (CLI entry), AND
#   DEVCAKE_FACTORY_DIR (environ) equals the resolved absolute factory dir
#   when set (decisive — unequal means not this factory); when unset/empty,
#   the process cwd is the repo whose `.factory` is that dir, OR the cmdline
#   contains that absolute factory path.
# Does NOT match baker_respawn.sh / systemd / launchd supervisors — only the
# baker child. Linux prefers /proc; Darwin (or no /proc) uses ps + lsof cwd.
#
# Usage: devcake_baker_list_factory_bakers <factory_dir>
devcake_baker_list_factory_bakers() {
  local factory_dir="$1"
  local factory_abs="" repo_abs="" pid age cmd cwd env_factory eww
  local cmdline_file environ_file

  factory_abs="$(cd "$factory_dir" 2>/dev/null && pwd -P)" || factory_abs=""
  [[ -n "$factory_abs" ]] || factory_abs="$factory_dir"
  repo_abs="$(cd "${factory_abs}/.." 2>/dev/null && pwd -P)" || repo_abs=""

  if [[ -d /proc/self ]]; then
    for cmdline_file in /proc/[0-9]*/cmdline; do
      [[ -e "$cmdline_file" ]] || continue
      pid="${cmdline_file%/cmdline}"
      pid="${pid##*/}"
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      cmd="$(tr '\0' ' ' <"$cmdline_file" 2>/dev/null | sed 's/[[:space:]]*$//')" || continue
      [[ -n "$cmd" ]] || continue
      devcake_baker_cmdline_is_module "$cmd" || continue
      env_factory=""
      environ_file="/proc/${pid}/environ"
      if [[ -r "$environ_file" ]]; then
        env_factory="$(tr '\0' '\n' <"$environ_file" 2>/dev/null \
          | sed -n 's/^DEVCAKE_FACTORY_DIR=//p' | head -n 1)" || true
      fi
      cwd="$(devcake_baker_process_cwd "$pid")"
      devcake_baker_targets_factory "$cmd" "$env_factory" "$cwd" \
        "$factory_abs" "$repo_abs" || continue
      age="$(ps -o etime= -p "$pid" 2>/dev/null | tr -d '[:space:]')" || age=""
      [[ -n "$age" ]] || age="unknown"
      printf '%s %s\n' "$pid" "$age"
    done
  else
    # Darwin / no usable /proc: ps pid + etime + command; best-effort environ
    # + cwd via lsof (devcake_baker_process_cwd).
    while read -r pid age cmd; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      [[ -n "$cmd" ]] || continue
      devcake_baker_cmdline_is_module "$cmd" || continue
      env_factory=""
      eww="$(ps eww -p "$pid" -o command= 2>/dev/null || true)"
      if [[ "$eww" == *"DEVCAKE_FACTORY_DIR="* ]]; then
        env_factory="$(printf '%s\n' "$eww" \
          | sed -n 's/.*DEVCAKE_FACTORY_DIR=\([^ ]*\).*/\1/p' | head -n 1)"
      fi
      cwd="$(devcake_baker_process_cwd "$pid")"
      devcake_baker_targets_factory "$cmd" "$env_factory" "$cwd" \
        "$factory_abs" "$repo_abs" || continue
      age="$(printf '%s' "$age" | tr -d '[:space:]')"
      [[ -n "$age" ]] || age="unknown"
      printf '%s %s\n' "$pid" "$age"
    done < <(ps -ax -o pid=,etime=,command= 2>/dev/null || true)
  fi
}

# SIGTERM every listed baker for this factory dir except keep_pid and $$.
# Pre-start: omit keep_pid (phrasing "pre-supervisor sweep").
# Post-install: pass the supervised child pid (phrasing "not the supervised child").
# Best-effort — a disappeared pid is not a failure. Log each kill with age.
#
# Usage: devcake_baker_displace_orphans <factory_dir> [keep_pid]
devcake_baker_displace_orphans() {
  local factory_dir="$1"
  local keep_pid="${2:-}"
  local pid age reason self
  self="$$"
  if [[ -n "$keep_pid" ]]; then
    reason="not the supervised child"
  else
    reason="pre-supervisor sweep"
  fi
  while read -r pid age; do
    [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$pid" == "$self" ]] && continue
    [[ -n "$keep_pid" && "$pid" == "$keep_pid" ]] && continue
    [[ -n "$age" ]] || age="unknown"
    echo "── displacing leftover host baker (pid ${pid}, age ${age}) — ${reason}"
    kill "$pid" 2>/dev/null || true
  done < <(devcake_baker_list_factory_bakers "$factory_dir" || true)
  sleep 0.3
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
  local size=0 elapsed=0 step=2 rotated=0
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
      if [[ "$rotated" -eq 1 ]]; then
        echo "   note: watch.log was rotated (shrunk) mid-launch — tail is of the current file" >&2
      fi
      echo "   last log lines (${logfile}):" >&2
      tail -n 40 "$logfile" 2>/dev/null >&2 || true
      echo "   tip: retry with devcake up --foreground-baker when the parent reaps detached children" >&2
      devcake_baker_emit_launch_failure "$logfile" "$launch_cmd"
      return 1
    fi
    size=$(wc -c <"$logfile" 2>/dev/null | tr -d ' ' || echo 0)
    # Copytruncate (rotate_watch_log) shrinks the live inode below the
    # pre-launch baseline — reset and keep waiting for post-rotate growth.
    if [[ "$size" -lt "$baseline" ]]; then
      rotated=1
      baseline=$size
      continue
    fi
    if [[ "$size" -gt "$baseline" ]]; then
      echo "── host baker watching keep-set (pid ${pid} → ${logfile}; liveness confirmed)"
      return 0
    fi
  done
  echo "── host baker did not progress its log within ~${seconds}s (pid ${pid})" >&2
  echo "   launch: ${launch_cmd}" >&2
  echo "   pidfile: ${pidfile}" >&2
  if [[ "$rotated" -eq 1 ]]; then
    echo "   note: watch.log was rotated (shrunk) mid-launch — tail is of the current file" >&2
  fi
  echo "   last log lines (${logfile}):" >&2
  tail -n 40 "$logfile" 2>/dev/null >&2 || true
  echo "   tip: retry with devcake up --foreground-baker when the parent reaps detached children" >&2
  devcake_baker_emit_launch_failure "$logfile" "$launch_cmd"
  return 1
}
