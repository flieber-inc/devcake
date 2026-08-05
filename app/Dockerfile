# syntax=docker/dockerfile:1
# DevCake orchestrator (FastAPI).
# Targets: runtime (prod, default) | test (pytest + tests/ for CI)
#
#   docker buildx bake app        # prod — no pytest, no tests/
#   docker buildx bake app-test   # CI test image

# Digest-pinned (ISSUES #29). Bump: docker buildx imagetools inspect <image:tag>
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

# ── prod deps (cached while requirements.txt is unchanged) ───────────────────
FROM ${PYTHON_IMAGE} AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip==26.2.1 \
    && pip install -r requirements.txt

# ── prod + test deps ─────────────────────────────────────────────────────────
FROM deps AS deps-dev

COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.txt

# ── runtime (production) ─────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="devcake-app" \
      org.opencontainers.image.description="DevCake orchestrator API" \
      org.opencontainers.image.source="https://github.com/fidecastro/devcake"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DEVCAKE_DATA_DIR=/data

# git + git-lfs: the repo source mirror (ADR-0024) — the app's ONLY
# subprocess consumer (adapters/git.py). /mirrors pre-created app-owned so
# docker's named-volume copy-up initializes `devcake_mirrors` with the right
# ownership at first start (uid 1000 writes syncs; Devs mount it RO).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        git-lfs \
    && rm -rf /var/lib/apt/lists/* \
    && git lfs install --system

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --shell /usr/sbin/nologin --create-home app \
    && mkdir -p /data /mirrors \
    && chown app:app /data /mirrors

WORKDIR /srv

COPY --from=deps /opt/venv /opt/venv
COPY --chown=app:app devcake/ ./devcake/

USER app

EXPOSE 8000

CMD ["uvicorn", "devcake.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── test image (CI) — prod code + pytest + tests/ ────────────────────────────
FROM runtime AS test

LABEL org.opencontainers.image.title="devcake-app-test" \
      org.opencontainers.image.description="DevCake app + pytest suite"

USER root
COPY --from=deps-dev /opt/venv /opt/venv
COPY --chown=app:app tests/ ./tests/
COPY --chown=app:app ruff.toml ./ruff.toml
USER app

# Default for `docker run devcake/app-test` — override as needed.
CMD ["python", "-m", "pytest", "tests/", "-q"]
