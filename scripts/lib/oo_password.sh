#!/usr/bin/env bash
# OpenObserve bootstrap password composition check (CAKE-131).
#
# Lockstep with docker-compose.yml openobserve pin v0.91.5
# (src/config/src/utils/password.rs). Update this rule when bumping the image.
#
# Sourced, not executed. Empty values are skipped — app boot still refuses
# empties via _refuse_insecure_passwords; this gate is composition-only.

# Return 0 iff password meets OO v0.91.5 strength (non-empty assumed).
oo_password_ok() {
  local pw="$1"
  local len=${#pw}
  if (( len < 8 || len > 128 )); then
    return 1
  fi
  # ASCII class checks — mirrors is_ascii_lowercase/uppercase/digit.
  [[ "$pw" =~ [a-z] ]] || return 1
  [[ "$pw" =~ [A-Z] ]] || return 1
  [[ "$pw" =~ [0-9] ]] || return 1
  # Anything not an ASCII letter/digit counts as special (symbols + non-ASCII).
  [[ "$pw" =~ [^a-zA-Z0-9] ]] || return 1
  return 0
}

# require_oo_password <VAR_NAME> <value>
# Empty → 0 (defer to app boot). Non-empty violation → stderr + return 1.
require_oo_password() {
  local var="$1"
  local value="${2-}"
  if [[ -z "$value" ]]; then
    return 0
  fi
  if oo_password_ok "$value"; then
    return 0
  fi
  echo "error: ${var} does not meet OpenObserve v0.91.5 password policy: must be 8-128 characters and contain at least one lowercase letter, one uppercase letter, one digit, and one special character" >&2
  return 1
}
