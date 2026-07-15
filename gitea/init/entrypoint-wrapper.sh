#!/bin/bash
# DevCake Gitea bootstrap wrapper (docs/16 M11, live-verified on
# 1.24.7-rootless): config generation MUST precede any gitea CLI call —
# `gitea migrate` fatals without app.ini, and app.ini only exists after the
# image's docker-setup.sh (environment-to-ini) has run. Order:
#   1. docker-setup.sh        — generates $GITEA_APP_INI from GITEA__* env
#   2. gitea migrate          — creates/updates the sqlite schema
#   3. admin user ensure      — create, or password-resync if it exists
#   4. exec the stock entrypoint (which re-runs docker-setup.sh harmlessly)
#
# Failure posture (audit A27): a REAL admin-create failure kills the
# container (compose healthcheck surfaces it) instead of being swallowed;
# an existing admin gets its password resynced from .env so rotation works.
# Residual, accepted: the gitea CLI takes the password only on argv (no
# stdin/env intake) — exposure is bounded to this container's own PID
# namespace.
set -e

/usr/local/bin/docker-setup.sh
gitea -c "$GITEA_APP_INI" migrate

if out=$(gitea -c "$GITEA_APP_INI" admin user create --admin \
    --username "$GITEA_ADMIN_USER" \
    --password "$GITEA_ADMIN_PASSWORD" \
    --email devcake-admin@devcake.invalid \
    --must-change-password=false 2>&1); then
  echo "gitea bootstrap: admin user created"
elif echo "$out" | grep -qi "already exists"; then
  # rotation sync: .env is the source of truth for the admin credential
  gitea -c "$GITEA_APP_INI" admin user change-password \
      --username "$GITEA_ADMIN_USER" \
      --password "$GITEA_ADMIN_PASSWORD" \
      --must-change-password=false
  echo "gitea bootstrap: admin user exists — password synced from .env"
else
  echo "gitea bootstrap FAILED: $out" >&2
  exit 1
fi

exec /usr/bin/dumb-init -- /usr/local/bin/docker-entrypoint.sh
