"""OpenTelemetry setup (docs/12 §1). The APP exports OTLP straight to
OpenObserve with the OO_INGEST_* service account; DEV containers export
unauthenticated to the inserted otel-collector (OTEL_COLLECTOR_URL), which
alone holds the OO credentials — Devs carry none at all (ISSUES #13).

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
# Dev-side OTLP target: the collector on devcake_runtime (docs/12 §1)
OTEL_COLLECTOR_URL = os.environ.get("OTEL_COLLECTOR_URL", "http://otel-collector:4318")


def _basic_auth() -> str:
    # the OO service account (never root — root stays admin-ops-only:
    # stream deletion, provisioning)
    email = os.environ.get("OO_INGEST_EMAIL", "")
    password = os.environ.get("OO_INGEST_PASSWORD", "")
    return base64.b64encode(f"{email}:{password}".encode()).decode()


async def push_oo_log(stream: str, record: dict) -> bool:
    """Ship one JSON record to an OpenObserve log stream (docs/12 §6). Never
    raises: shipping runs inside kill/finalize paths that must not break."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{OO_URL}/api/{OO_ORG}/{stream}/_json",
                headers={"Authorization": f"Basic {_basic_auth()}"},
                json=[record])
            resp.raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — telemetry shipping is fire-and-forget; failure logged, must never break kill/finalize
        log.warning("could not ship record to OO stream %s", stream, exc_info=True)
        return False


def setup_telemetry() -> trace.Tracer:
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    exporter = OTLPSpanExporter(
        endpoint=f"{OO_URL}/api/{OO_ORG}/v1/traces",
        headers={"Authorization": f"Basic {_basic_auth()}"},
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    # Design principle (founder, 2026-07-11): EVERYTHING is traced. Auto-span
    # every outbound HTTP call (Linear, GitHub/GitLab, Dagu, OpenObserve probes).
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    HTTPXClientInstrumentor().instrument()
    log.info("telemetry: exporting OTLP traces to %s/api/%s/v1/traces", OO_URL, OO_ORG)
    return trace.get_tracer("devcake")
