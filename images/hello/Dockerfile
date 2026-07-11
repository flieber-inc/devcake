FROM python:3.12-slim

RUN pip install --no-cache-dir \
    redis==5.2.1 \
    opentelemetry-api==1.29.0 \
    opentelemetry-sdk==1.29.0 \
    opentelemetry-exporter-otlp-proto-http==1.29.0

COPY hello_dev.py /hello_dev.py
ENTRYPOINT ["python", "/hello_dev.py"]
