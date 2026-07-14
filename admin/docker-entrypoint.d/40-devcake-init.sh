#!/bin/sh
# Renders the runtime config (/config.js) for the SPA and builds the htpasswd.
# Runs via the stock nginx image's /docker-entrypoint.d mechanism.
set -eu

: "${ADMIN_USER:?ADMIN_USER is required (docs/11 §5)}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required (docs/11 §5)}"

htpasswd -bc /etc/nginx/.htpasswd "$ADMIN_USER" "$ADMIN_PASSWORD" >/dev/null 2>&1

cat > /usr/share/nginx/html/config.js <<EOF
// generated at container start from env (docs/13 §2)
window.DEVCAKE = {
  daguUrl: "${DAGU_UI_URL:-http://localhost:8525}",
  ooUrl: "${OO_UI_URL:-http://localhost:5080}"
};
EOF
