#!/bin/sh
# Repairs the stock image's broken DOCKER_GID mechanism on the ubuntu base:
# its entrypoint calls alpine-only `addgroup`, so the docker group never gets
# created and `sudo -g "#$DOCKER_GID"` fails. Ubuntu has shadow-utils, so we
# create the group here (custom-init.d runs before the privilege drop).
# Keeps the daemon at uid 1000 — least privilege for docker.sock access.
set -eu
if [ "${DOCKER_GID:--1}" != "-1" ]; then
  getent group docker >/dev/null 2>&1 || groupadd -o -g "$DOCKER_GID" docker
  usermod -aG docker dagu || true
fi
