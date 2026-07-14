# syntax=docker/dockerfile:1
#
# DevCake Dev harness images — multi-target Dockerfile.
# Shared `base` stage is built once (BuildKit) and reused by every harness.
#
# Targets: base | hello | claude-code | codex | grok-build
#
#   docker buildx bake -f docker-bake.hcl images
#   docker compose --profile images build
#
# Context must be ./images (needs common/dev_entrypoint.py).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim

# ── Node toolchain (copied into node-based harnesses; no nodesource mess) ────
FROM ${NODE_IMAGE} AS node-tools

# ── Shared Dev base: git, forge CLIs, Python relay deps, non-root user ───────
FROM ${PYTHON_IMAGE} AS base

LABEL org.opencontainers.image.title="devcake-dev-base" \
      org.opencontainers.image.description="Shared base for DevCake Dev harnesses" \
      org.opencontainers.image.source="https://github.com/fidecastro/devcake"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# One layer: system packages + GitHub CLI + GitLab CLI (docs/06 §7).
# Sorted packages; apt lists wiped in the same RUN (Docker best practices).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
         https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/gh.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v1.55.0/downloads/glab_1.55.0_linux_${arch}.deb" \
         -o /tmp/glab.deb \
    && apt-get install -y --no-install-recommends /tmp/glab.deb \
    && rm -f /tmp/glab.deb \
    && rm -rf /var/lib/apt/lists/*

# Python runtime deps for the shared entrypoint (Redis Streams + OTEL).
# Cache mount speeds rebuilds; mount is never committed into the image layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        redis==5.2.1 \
        opentelemetry-api==1.29.0 \
        opentelemetry-sdk==1.29.0 \
        opentelemetry-exporter-otlp-proto-http==1.29.0

# Non-root harness user (docs/07 §7; Claude Code refuses root).
# Create user before setting HOME so installers (e.g. Grok CLI) can write $HOME.
RUN useradd -m -u 1000 -d /home/dev -s /bin/bash dev \
    && mkdir -p /workspace \
    && chown -R dev:dev /home/dev /workspace

ENV HOME=/home/dev

COPY common/dev_entrypoint.py /dev_entrypoint.py

WORKDIR /workspace
ENTRYPOINT ["python", "/dev_entrypoint.py"]

# ── hello: CI stub — no git/forge CLIs needed ───────────────────────────────
FROM ${PYTHON_IMAGE} AS hello

LABEL org.opencontainers.image.title="devcake-dev-hello" \
      org.opencontainers.image.description="DevCake hello stub Dev (CI fixture)"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        redis==5.2.1 \
        opentelemetry-api==1.29.0 \
        opentelemetry-sdk==1.29.0 \
        opentelemetry-exporter-otlp-proto-http==1.29.0

RUN useradd -m -u 1000 dev

COPY --chown=dev:dev hello/hello_dev.py /hello_dev.py

USER dev
WORKDIR /home/dev
ENTRYPOINT ["python", "/hello_dev.py"]

# ── claude-code ─────────────────────────────────────────────────────────────
FROM base AS claude-code

LABEL org.opencontainers.image.title="devcake-dev-claude-code"

# Official Node image toolchain — smaller/faster than nodesource setup scripts.
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

USER dev
ENV DEVCAKE_HARNESS=claude-code

# ── codex ───────────────────────────────────────────────────────────────────
FROM base AS codex

LABEL org.opencontainers.image.title="devcake-dev-codex"

COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install -g @openai/codex \
    && npm cache clean --force

USER dev
ENV DEVCAKE_HARNESS=codex

# ── grok-build (no Node; CLI installed as the harness user) ─────────────────
FROM base AS grok-build

LABEL org.opencontainers.image.title="devcake-dev-grok-build"

USER dev
# Official installer (docs/08 §2; CLI verified ~v0.2.93)
RUN curl -fsSL https://x.ai/cli/install.sh | bash

ENV PATH="/home/dev/.grok/bin:/home/dev/.local/bin:${PATH}" \
    DEVCAKE_HARNESS=grok-build
