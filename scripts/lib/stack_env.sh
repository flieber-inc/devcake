#!/usr/bin/env bash
# Shared stack-env derivation (ADR-0034: one path per singular process).
# up.sh and ci_compose_for_dispatch.sh both need DOCKER_GID and
# DEVCAKE_WS_HOST; each used to derive them independently — a standing
# drift surface between the deploy ritual and the CI bring-up (2026-08-12
# audit). Sourced, not executed: POLICY stays with the caller (up.sh fails
# hard, CI falls back to permissive defaults) — only the DERIVATION is one.

# gid of the docker socket, validated numeric; empty output + rc 1 when the
# socket is missing/unreadable — the CALLER decides whether that is fatal.
devcake_docker_gid() {
  local sock="${1:-/var/run/docker.sock}" gid=""
  [[ -e "$sock" ]] || return 1
  if gid=$(stat -c '%g' "$sock" 2>/dev/null); then
    :
  elif gid=$(stat -f '%g' "$sock" 2>/dev/null); then
    : # BSD/macOS
  else
    return 1
  fi
  [[ "$gid" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$gid"
}

# DEVCAKE_WS_HOST resolution order (ADR-0025: bind sources resolve on the
# daemon host, so the value must be host-absolute): existing .env value wins
# (operator relocation), else <repo>/workspaces. Prints the value; the
# CALLER validates absoluteness with its own error copy.
devcake_ws_host() {
  local env_file="${1:-.env}" repo_root="${2:-$(pwd)}" ws=""
  ws="$(grep -E '^DEVCAKE_WS_HOST=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- || true)"
  if [[ -z "$ws" ]]; then
    ws="${repo_root}/workspaces"
  fi
  printf '%s\n' "$ws"
}
