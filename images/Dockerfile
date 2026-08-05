# syntax=docker/dockerfile:1
#
# DevCake Dev harness images — multi-target Dockerfile.
# Shared `base` stage is built once (BuildKit) and reused by every harness.
#
# Targets: base | hello | claude-code | codex | grok-build
#
#   docker buildx bake images
#
# Context must be ./images (needs common/dev_entrypoint.py + common/devcake_dev/).
# Pin harness CLIs below — bump intentionally, never float on :latest npm.
# Version-currency doctrine (docs/08, founder 2026-08-04): DevCake FOLLOWS
# the cutting edge of these CLIs — bump promptly, re-run the capture rig
# (scripts/harness_capture/) each bump, truth-sweep version-stamped claims.

# Digest-pinned (ISSUES #29). Bump: docker buildx imagetools inspect <image:tag>
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
# Pinned harness CLIs (npm view @anthropic-ai/claude-code / @openai/codex).
ARG CLAUDE_CODE_VERSION=2.1.221
ARG CODEX_VERSION=0.146.0
# Toolchain floor (ADR-0023): playwright pinned to the SPA suite's family
# (admin/spa/package.json playwright-core) so the whole repo speaks one
# browser stack; python doc/spreadsheet libs pinned like the entrypoint deps.
ARG PLAYWRIGHT_VERSION=1.61.1

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
# ADR-0023 capability floor, class A (root-only: compilers + system libs) and
# class C (high-frequency conveniences — every run that bootstraps ripgrep by
# hand is burning the turns ADR-0022 exists to save): build-essential unlocks
# native pip/npm builds; pandoc + poppler-utils are the document/spreadsheet
# floor (LibreOffice rejected: ~700 MB for fidelity nobody asked for).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        file \
        git \
        git-lfs \
        gnupg \
        jq \
        less \
        pandoc \
        poppler-utils \
        procps \
        ripgrep \
        sqlite3 \
        tini \
        unzip \
        zip \
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

# Python runtime deps for the shared entrypoint (Redis Streams + OTEL), plus
# the ADR-0023 spreadsheet/document floor (pandas/openpyxl — models reach for
# these before anything else) and uv (root-free env management for Devs).
# Cache mount speeds rebuilds; mount is never committed into the image layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        redis==5.2.1 \
        opentelemetry-api==1.29.0 \
        opentelemetry-sdk==1.29.0 \
        opentelemetry-exporter-otlp-proto-http==1.29.0 \
        openpyxl==3.1.5 \
        pandas==2.2.3 \
        uv==0.5.11

# Node toolchain in the SHARED base (ADR-0023, class B): the tool floor must
# be harness-identical — grok Devs could not even `npm run dev` before this —
# and playwright needs it. HOME is still /root here, so root's npm never
# reads the dev user's .npmrc prefix redirect (set below).
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Browser floor (ADR-0023, class A — user-space can download a browser but
# can NEVER apt-install its shared libraries): playwright's pinned headless
# shell + its system deps, in a shared path so every matching-version
# playwright finds it without re-downloading. The `playwright` CLI itself
# goes on PATH globally at the same pin.
ARG PLAYWRIGHT_VERSION
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN npm install -g "playwright@${PLAYWRIGHT_VERSION}" \
    && playwright install --with-deps chromium-headless-shell \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# Non-root harness user (docs/07 §7; Claude Code refuses root).
# Create user before setting HOME so installers (e.g. Grok CLI) can write $HOME.
# The .npmrc prefix makes RUNTIME `npm i -g` a user-space self-serve (root
# builds are unaffected: they run with HOME=/root above).
# /opt/pw-browsers is DEV-OWNED (ADR-0023 fix round): PLAYWRIGHT_BROWSERS_PATH
# points every install there, so a read-only dir would brick any browser the
# base did not bake (measured: EACCES on __dirlock, AFTER the download).
# Writable is safe — the container is disposable, corruption dies with the run.
RUN useradd -m -u 1000 -d /home/dev -s /bin/bash dev \
    && mkdir -p /workspace /home/dev/.npm-global /home/dev/.local/bin \
    && printf 'prefix=/home/dev/.npm-global\n' > /home/dev/.npmrc \
    && chown -R dev:dev /home/dev /workspace /opt/pw-browsers

ENV HOME=/home/dev \
    PATH="/home/dev/.local/bin:/home/dev/.npm-global/bin:${PATH}"

# Build-time proof the floor actually works AS THE DEV USER — above all that
# the headless shell LAUNCHES (loads every shared lib; the exact failure the
# unbaked images hit) and the doc/spreadsheet imports resolve. The shell
# binary name depends on which mirror playwright used (classic build:
# chrome-linux/headless_shell; Chrome-for-Testing fallback:
# chrome-headless-shell-linux64/chrome-headless-shell) — the runtime registry
# resolves both, so the smoke must accept both (measured: local bake pulled
# classic, CI pulled CfT, same playwright pin).
RUN su dev -s /bin/sh -c ' \
        set -e; \
        node --version && npx --version; \
        playwright --version; \
        shell="$(find /opt/pw-browsers -type f \
            \( -name headless_shell -o -name chrome-headless-shell \) \
            | head -1)"; \
        test -n "$shell" || { echo "no headless shell under /opt/pw-browsers" >&2; exit 1; }; \
        "$shell" --version; \
        touch /opt/pw-browsers/.devcake-writable \
            && rm /opt/pw-browsers/.devcake-writable; \
        python -c "import pandas, openpyxl"; \
        uv --version; \
        command -v tini jq rg pandoc pdftotext sqlite3 unzip zip gcc make >/dev/null'

# Shared entrypoint façade + Dev-side package (domain/fault pure; more slices later).
# Package root is `/` so `import devcake_dev` works; ENTRYPOINT path unchanged.
COPY common/devcake_dev /devcake_dev
COPY common/dev_entrypoint.py /dev_entrypoint.py

WORKDIR /workspace
# tini as PID 1 (ADR-0023 fix round): the entrypoint reaps nothing, and
# browser process trees orphan children on unclean exits — over a multi-hour
# run (longer still under ADR-0022 continuations) zombies and stray
# headless-shells would otherwise accumulate. Dagu cannot pass --init
# (container schema rejects HostConfig fields, docs/07 §7).
ENTRYPOINT ["tini", "--", "python", "/dev_entrypoint.py"]

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

ARG CLAUDE_CODE_VERSION

# Node lives in base (ADR-0023). HOME=/root: base sets HOME=/home/dev, and
# root's npm would otherwise read the dev user's .npmrc prefix redirect and
# install the CLI into /home/dev/.npm-global root-owned.
RUN HOME=/root npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && HOME=/root npm cache clean --force

USER dev
ENV DEVCAKE_HARNESS=claude-code

# ── codex ───────────────────────────────────────────────────────────────────
FROM base AS codex

LABEL org.opencontainers.image.title="devcake-dev-codex"

ARG CODEX_VERSION

# Node lives in base (ADR-0023); HOME=/root for the same .npmrc reason as claude.
RUN HOME=/root npm install -g "@openai/codex@${CODEX_VERSION}" \
    && HOME=/root npm cache clean --force

USER dev
ENV DEVCAKE_HARNESS=codex

# ── grok-build (CLI installed as the harness user; Node now via base) ───────
FROM base AS grok-build

LABEL org.opencontainers.image.title="devcake-dev-grok-build"

USER dev
# VENDORED official installer (2026-08 evaluation: `curl | bash` from a
# moving URL was the one unpinned fetch in an otherwise digest-pinned tree —
# and sha-pinning the URL would break every bake the moment x.ai updates it).
# The checked-in copy is reviewed on bump like any other dependency:
#   curl -fsSL https://x.ai/cli/install.sh -o images/vendor/grok-install.sh
# then diff-review + commit. Installs the current stable channel CLI (the
# installer supports an explicit version arg if pinning becomes desirable).
COPY --chown=dev:dev vendor/grok-install.sh /tmp/grok-install.sh
RUN bash /tmp/grok-install.sh && rm /tmp/grok-install.sh

# base's PATH already carries ~/.local/bin and ~/.npm-global/bin (ADR-0023)
ENV PATH="/home/dev/.grok/bin:${PATH}" \
    DEVCAKE_HARNESS=grok-build
