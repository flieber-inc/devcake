#!/bin/sh
# Repairs the stock image's broken DOCKER_GID mechanism on the ubuntu base:
# its entrypoint calls alpine-only `addgroup`, so the docker group never gets
# created and `sudo -g "#$DOCKER_GID"` fails. Ubuntu has shadow-utils, so we
# create the group here (custom-init.d runs before the privilege drop).
# Keeps the daemon at uid 1000 — least privilege for docker.sock access.
#
# Invoked by compose's dagu entrypoint wrapper via `sh` (not exec) so a missing
# host +x bit cannot skip this hook — stock /entrypoint.sh only runs -x files
# and the bind is :ro. Idempotent if both paths fire.
set -eu
if [ "${DOCKER_GID:--1}" != "-1" ]; then
  getent group docker >/dev/null 2>&1 || groupadd -o -g "$DOCKER_GID" docker
  usermod -aG docker dagu || true
fi
