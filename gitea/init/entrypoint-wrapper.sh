#!/bin/bash
# DevCake Gitea bootstrap wrapper (docs/16 M11, live-verified on
# 1.24.7-rootless): config generation MUST precede any gitea CLI call —
# `gitea migrate` fatals without app.ini, and app.ini only exists after the
# image's docker-setup.sh (environment-to-ini) has run. Order:
#   1. docker-setup.sh        — generates $GITEA_APP_INI from GITEA__* env
#   2. gitea migrate          — creates/updates the sqlite schema
#   3. admin user create      — idempotent ("already exists" tolerated)
#   4. exec the stock entrypoint (which re-runs docker-setup.sh harmlessly)
set -e

/usr/local/bin/docker-setup.sh
gitea -c "$GITEA_APP_INI" migrate
gitea -c "$GITEA_APP_INI" admin user create --admin \
    --username "$GITEA_ADMIN_USER" \
    --password "$GITEA_ADMIN_PASSWORD" \
    --email devcake-admin@devcake.invalid \
    --must-change-password=false \
  || echo "gitea bootstrap: admin user exists — ok"

exec /usr/bin/dumb-init -- /usr/local/bin/docker-entrypoint.sh
