"""OpenTelemetry setup: OTLP HTTP straight to OpenObserve (docs/12 §1, no collector in v0).

The exporter is built in code rather than via OTEL_* env vars so the Basic-auth
header never needs the env-var percent-encoding dance.
"""

import base64
import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger("devcake.telemetry")

SERVICE_NAME = "devcake-app"
OO_URL = os.environ.get("OO_URL", "http://openobserve:5080")
OO_ORG = os.environ.get("OO_ORG", "default")


def _basic_auth() -> str:
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    return base64.b64encode(f"{email}:{password}".encode()).decode()


def setup_telemetry() -> trace.Tracer:
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    exporter = OTLPSpanExporter(
        endpoint=f"{OO_URL}/api/{OO_ORG}/v1/traces",
        headers={"Authorization": f"Basic {_basic_auth()}"},
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    log.info("telemetry: exporting OTLP traces to %s/api/%s/v1/traces", OO_URL, OO_ORG)
    return trace.get_tracer("devcake")
