# syntax=docker/dockerfile:1
# DevCake orchestrator (FastAPI). Multi-stage: build venv once, run slim.
# CI still runs pytest inside this image (scripts/ci_suite.sh) — tests ship with it.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ── deps stage (cached while requirements.txt is unchanged) ──────────────────
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
    pip install -r requirements.txt

# ── runtime ─────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="devcake-app" \
      org.opencontainers.image.description="DevCake orchestrator API" \
      org.opencontainers.image.source="https://github.com/fidecastro/devcake"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DEVCAKE_DATA_DIR=/data

# Non-root runtime. Named volumes inherit ownership of /data from the image
# on first create (Docker copies image contents into an empty volume).
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --shell /usr/sbin/nologin --create-home app \
    && mkdir -p /data \
    && chown app:app /data

WORKDIR /srv

COPY --from=deps /opt/venv /opt/venv
COPY --chown=app:app devcake/ ./devcake/
COPY --chown=app:app tests/ ./tests/

USER app

EXPOSE 8000

# Exec form so uvicorn is PID 1 (signals + graceful shutdown).
CMD ["uvicorn", "devcake.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
