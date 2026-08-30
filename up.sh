#!/usr/bin/env bash
# Thin shim → `devcake up` (ADR-0038 Decision 4 / CAKE-177).
#
# Bring-up logic lives in the installable CLI (`cli/devcake_cli/`). This file
# remains for muscle memory and docs during the transition window:
#
#   ./up.sh --bake
#   ./up.sh --dry-run
#   ./up.sh --foreground-baker
#
# Install the CLI once from a checkout:
#   uv tool install .
#   # or: pipx install .
# then re-run this shim (or call `devcake up` directly).
set -euo pipefail
cd "$(dirname "$0")"

if command -v devcake >/dev/null 2>&1; then
  exec devcake up "$@"
fi

cat >&2 <<'EOF'
error: `devcake` is not on PATH.

The bring-up path moved into the DevCake host CLI (ADR-0038). Install it from
this checkout, then re-run ./up.sh (or call `devcake up` directly):

  uv tool install .
  # or: pipx install .
  # or: pip install .

Docs: docs/adr/0038-devcake-cli-scope-command-surface-and-agent-operability.md
EOF
exit 1
