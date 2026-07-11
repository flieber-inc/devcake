FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
         https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/gh.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex \
    && pip install --no-cache-dir \
        redis==5.2.1 \
        opentelemetry-api==1.29.0 \
        opentelemetry-sdk==1.29.0 \
        opentelemetry-exporter-otlp-proto-http==1.29.0

# GitLab CLI (docs/06 §7)
RUN curl -fsSL https://gitlab.com/gitlab-org/cli/-/releases/v1.55.0/downloads/glab_1.55.0_linux_$(dpkg --print-architecture).deb -o /tmp/glab.deb \
    && apt-get update && apt-get install -y /tmp/glab.deb && rm -f /tmp/glab.deb && rm -rf /var/lib/apt/lists/*

# non-root harness user (docs/07 §7)
RUN useradd -m -u 1000 dev && mkdir -p /workspace && chown dev:dev /workspace

COPY common/dev_entrypoint.py /dev_entrypoint.py
WORKDIR /workspace
USER dev
ENV HOME=/home/dev
ENV DEVCAKE_HARNESS=codex
ENTRYPOINT ["python", "/dev_entrypoint.py"]
