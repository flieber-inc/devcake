FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir \
        redis==5.2.1 \
        opentelemetry-api==1.29.0 \
        opentelemetry-sdk==1.29.0 \
        opentelemetry-exporter-otlp-proto-http==1.29.0

# non-root harness user (docs/07 §7; claude refuses --dangerously-skip-permissions as root)
RUN useradd -m -u 1000 dev && mkdir -p /workspace && chown dev:dev /workspace

COPY dev_entrypoint.py /dev_entrypoint.py
WORKDIR /workspace
USER dev
ENV HOME=/home/dev
ENTRYPOINT ["python", "/dev_entrypoint.py"]
