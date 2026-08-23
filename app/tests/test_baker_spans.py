"""Baker outbox → one OpenObserve trace (plan slice 2c).

Public seams:
  span_record / probe_spans_from_receipt (host baker, no tracer)
  replay_baker_spans(records, tracer) — app poll emits real spans
"""

from __future__ import annotations

import sys
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


def _factory():
    roots = [
        Path(__file__).resolve().parents[2] / "scripts",
        Path("/srv/repo-scripts"),
    ]
    root = next((p for p in roots if (p / "dev_factory").is_dir()), None)
    assert root is not None
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import dev_factory.spans as spans
    return spans


def test_http_401_auth_row_shares_the_claim_trace_id():
    spans = _factory()
    tid, root = spans.new_ids()
    rec = {
        "rows": [{
            "name": "http_401", "required": True, "status": "fail",
            "cause": "auth",
            "expected": {"exit": 12, "class": "DEV_AUTH",
                         "reason": "terminal_error"},
            "observed": {"exit": 15, "class": "DEV_HARNESS_FAULT",
                         "reason": "terminal_error"},
        }],
    }
    kids = spans.probe_spans_from_receipt(
        rec, trace_id=tid, parent=root, start_ns=10, end_ns=20)
    assert len(kids) == 1
    assert kids[0]["trace_id"] == tid
    assert kids[0]["parent"] == root
    assert kids[0]["name"] == "baker.probe.http_401"
    assert kids[0]["cause"] == "auth"
    assert kids[0]["status"] == "error"


def test_replay_puts_claim_and_probe_on_the_same_trace(monkeypatch):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    from devcake.bake_status import replay_baker_spans

    spans_mod = _factory()
    tid, root = spans_mod.new_ids()
    kid = spans_mod.new_ids()[1]
    recs = [
        spans_mod.span_record(
            name="baker.reconcile", trace_id=tid, span_id=root,
            parent="", start_ns=1, end_ns=30, status="error"),
        spans_mod.span_record(
            name="baker.probe.http_401", trace_id=tid, span_id=kid,
            parent=root, start_ns=10, end_ns=20, status="error",
            cause="auth"),
    ]
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    replay_baker_spans(recs, provider.get_tracer("test"))
    finished = exporter.get_finished_spans()
    assert {s.name for s in finished} == {
        "baker.reconcile", "baker.probe.http_401"}
    want = int(tid, 16)
    assert all(s.context.trace_id == want for s in finished)
    probe = next(s for s in finished if s.name == "baker.probe.http_401")
    assert probe.attributes["devcake.baker.cause"] == "auth"
    assert probe.status.status_code.name == "ERROR"


def test_replay_ignores_launch_failed_non_span_records(monkeypatch):
    """CAKE-134: outbox launch_failed is OO-only; replay must not raise."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    from devcake.bake_status import replay_baker_spans

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    replay_baker_spans(
        [{"event": "launch_failed", "detail": "host crash", "ts": "t"}],
        provider.get_tracer("test"),
    )
    assert list(exporter.get_finished_spans()) == []
