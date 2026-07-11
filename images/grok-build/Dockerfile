FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
         https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/gh.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        redis==5.2.1 \
        opentelemetry-api==1.29.0 \
        opentelemetry-sdk==1.29.0 \
        opentelemetry-exporter-otlp-proto-http==1.29.0

# non-root harness user (docs/07 §7)
RUN useradd -m -u 1000 dev && mkdir -p /workspace && chown dev:dev /workspace

USER dev
ENV HOME=/home/dev
# Grok Build CLI via the official installer (docs/08 §2; pinned CLI verified v0.2.93)
RUN curl -fsSL https://x.ai/cli/install.sh | bash
ENV PATH="/home/dev/.grok/bin:/home/dev/.local/bin:${PATH}"

COPY --chown=dev:dev common/dev_entrypoint.py /dev_entrypoint.py
WORKDIR /workspace
ENV DEVCAKE_HARNESS=grok-build
ENTRYPOINT ["python", "/dev_entrypoint.py"]
