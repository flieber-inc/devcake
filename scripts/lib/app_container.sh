#!/usr/bin/env bash
# Shared resolve for the compose `app` container (ADR-0034: one path).
# Compose names the container from the project name ({project}-app-1 /
# {project}_app_1); hardcoding `devcake-app-1` only works when the checkout
# directory is literally `devcake`. Override: DEVCAKE_APP_CONTAINER=<id|name>.
# Prints the id/name on stdout; empty when unset and no running `app` service.
# Callers decide whether empty is fatal (grok_login) or warn-only (ci_suite banner).

devcake_app_container() {
  if [[ -n "${DEVCAKE_APP_CONTAINER:-}" ]]; then
    printf '%s\n' "$DEVCAKE_APP_CONTAINER"
    return 0
  fi
  docker compose ps -q app 2>/dev/null | head -1
}
