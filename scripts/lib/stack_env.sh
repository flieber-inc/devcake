#!/usr/bin/env bash
# Shared stack-env derivation (ADR-0034: one path per singular process).
# devcake up and ci_compose_for_dispatch.sh both need DOCKER_GID and
# DEVCAKE_WS_HOST; each used to derive them independently — a standing
# drift surface between the deploy ritual and the CI bring-up (2026-08-12
# audit). Sourced, not executed: POLICY stays with the caller (devcake up fails
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

# gid of the docker socket as a Linux container sees the bind-mounted path
# (CAKE-128). Host-side stat is wrong on Docker Desktop (symlink / VM
# remapping). Probe with the same bind the stack will use; image pin is
# grepped from docker-compose.yml so there is no second digest to drift.
# Same contract as devcake_docker_gid: numeric stdout, rc 1 on any failure.
devcake_docker_gid_incontainer() {
  local sock="${1:-/var/run/docker.sock}" compose="" image="" gid="" root=""
  [[ -e "$sock" ]] || return 1
  # scripts/lib → repo root (host) or /srv (pytest: scripts → /srv/repo-scripts,
  # compose bind → /srv/docker-compose.yml).
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  for compose in "${root}/docker-compose.yml" "./docker-compose.yml" \
                 "/srv/docker-compose.yml"; do
    [[ -f "$compose" ]] && break
    compose=""
  done
  [[ -n "$compose" ]] || return 1
  # Extract the digest-pinned redis:7-alpine@sha256:… token already in compose.
  image="$(
    grep -E 'image:[[:space:]]*redis:7-alpine@sha256:[0-9a-f]{64}' "$compose" \
      | head -1 \
      | sed -E 's/.*image:[[:space:]]*(redis:7-alpine@sha256:[0-9a-f]{64}).*/\1/'
  )"
  [[ "$image" =~ ^redis:7-alpine@sha256:[0-9a-f]{64}$ ]] || return 1
  gid="$(
    docker run --rm -v "${sock}:/var/run/docker.sock:ro" "$image" \
      stat -c %g /var/run/docker.sock 2>/dev/null
  )" || return 1
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
