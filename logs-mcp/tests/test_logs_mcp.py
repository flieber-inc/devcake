"""devcake-logs-mcp backend contract: the Datadog Logs API wire shape, row
normalization/trimming, and the compact agent-facing formatting. server.py
(the only module importing the `mcp` SDK) is NOT imported here — the suite
must run with pytest+httpx alone; the binary is smoke-tested at image level
(`devcake-logs-mcp --selftest`)."""

import json

import httpx
import pytest

from logs_mcp.core import BackendError, LogRow, format_rows
from logs_mcp.datadog import DatadogBackend


# Shape per the Datadog Logs Search API v2 (POST /api/v2/logs/events/search):
# data[].attributes carries the event; custom attributes nest under
# attributes.attributes (where APM correlation puts dd.trace_id); the
# pagination cursor rides meta.page.after.
SEARCH_FIXTURE = {
    "data": [
        {
            "id": "AQAAA-1",
            "type": "log",
            "attributes": {
                "timestamp": "2026-07-16T10:03:01.000Z",
                "status": "error",
                "service": "payment-api",
                "host": "i-0abc",
                "message": "Payment failed: card declined",
                "attributes": {"dd": {"trace_id": "7031306439"},
                               "noisy": {"blob": "x" * 4000}},
                "tags": ["env:prod", "team:payments"],
            },
        },
        {
            # sparse event: normalization must tolerate missing fields
            "id": "AQAAA-2",
            "type": "log",
            "attributes": {"timestamp": "2026-07-16T10:03:02.000Z",
                           "message": "retrying"},
        },
    ],
    "meta": {"page": {"after": "next-cursor-123"}},
}


def _backend(handler, **kw):
    return DatadogBackend(api_key="k", app_key="a",
                          transport=httpx.MockTransport(handler), **kw)


def test_search_wire_contract_and_normalization():
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == \
            "https://api.datadoghq.com/api/v2/logs/events/search"
        assert request.headers["DD-API-KEY"] == "k"
        assert request.headers["DD-APPLICATION-KEY"] == "a"
        body = json.loads(request.content)
        assert body["filter"] == {"query": "service:payment-api status:error",
                                  "from": "now-15m", "to": "now"}
        assert body["page"] == {"limit": 25}
        assert body["sort"] == "-timestamp"
        return httpx.Response(200, json=SEARCH_FIXTURE)

    rows, cursor = _backend(handler).search(
        "service:payment-api status:error", "now-15m", "now", 25)
    assert calls, "backend never issued the request"   # mutation bar
    assert rows[0] == LogRow(ts="2026-07-16T10:03:01.000Z", status="error",
                             service="payment-api", host="i-0abc",
                             message="Payment failed: card declined",
                             log_id="AQAAA-1", trace_id="7031306439")
    # sparse events normalize to empty strings, never crash
    assert rows[1].service == "" and rows[1].trace_id == ""
    assert rows[1].message == "retrying"
    # the noisy attribute blob must NOT survive normalization anywhere
    assert all("blob" not in repr(r) for r in rows)
    assert cursor == "next-cursor-123"


def test_search_cursor_passthrough_and_limit_clamped_serverside():
    """Token efficiency is enforced in the backend, never left to the agent:
    an oversized limit clamps to 100; the pagination cursor from a prior
    page rides page.cursor; an exhausted result set returns cursor None."""
    def handler(request):
        body = json.loads(request.content)
        assert body["page"] == {"limit": 100, "cursor": "next-cursor-123"}
        return httpx.Response(200, json={"data": [], "meta": {"page": {}}})

    rows, cursor = _backend(handler).search("*", "now-1h", "now", 5000,
                                            cursor="next-cursor-123")
    assert rows == [] and cursor is None


def test_error_shapes_are_actionable():
    """Failures surface as BackendError with a message the AGENT can act on
    (server.py relays it as tool output — never a crash mid-mission)."""
    def handler(request):
        return httpx.Response(403, json={"errors": ["Forbidden"]})

    with pytest.raises(BackendError, match="403"):
        _backend(handler).search("*", "now-15m", "now", 10)
    # missing keys → the configuration hint at construction (server.py
    # builds the backend lazily per tool call and relays the message)
    with pytest.raises(BackendError, match="secret env vars"):
        DatadogBackend(api_key="", app_key="")


def test_aggregate_wire_contract_and_bucket_normalization():
    """Facet counts (POST /api/v2/logs/analytics/aggregate): count grouped
    by one facet, sorted desc — buckets normalize to (key, count) pairs."""
    def handler(request):
        assert str(request.url) == \
            "https://api.datadoghq.com/api/v2/logs/analytics/aggregate"
        body = json.loads(request.content)
        assert body["filter"] == {"query": "status:error",
                                  "from": "now-1h", "to": "now"}
        assert body["compute"] == [{"aggregation": "count"}]
        assert body["group_by"] == [{"facet": "service", "limit": 10,
                                     "sort": {"aggregation": "count",
                                              "order": "desc"}}]
        return httpx.Response(200, json={"data": {"buckets": [
            {"by": {"service": "payment-api"}, "computes": {"c0": 812}},
            {"by": {"service": "checkout"}, "computes": {"c0": 133}},
        ]}})

    out = _backend(handler).aggregate("status:error", "now-1h", "now",
                                      "service", 10)
    assert out == [("payment-api", 812), ("checkout", 133)]


def test_dd_site_honored_for_eu():
    """DD_SITE is a plain non-secret literal in the mcp add line — an EU org
    must hit api.datadoghq.eu, never the US default."""
    def handler(request):
        assert str(request.url).startswith("https://api.datadoghq.eu/")
        return httpx.Response(200, json={"data": []})

    rows, _ = _backend(handler, site="datadoghq.eu").search(
        "*", "now-15m", "now", 5)
    assert rows == []


def test_format_rows_compact_golden_and_truncation():
    """The agent-facing shape: one plain-text line per event (the cheapest
    token shape), empty fields collapsed, message hard-truncated, and a
    footer that teaches pagination. Golden — format changes are deliberate."""
    rows = [LogRow(ts="2026-07-16T10:03:01Z", status="error",
                   service="payment-api", host="i-0abc",
                   message="Payment failed: card declined",
                   log_id="AQAAA-1", trace_id="7031306439"),
            LogRow(ts="2026-07-16T10:03:02Z", status="", service="",
                   host="", message="m" * 400, log_id="AQAAA-2", trace_id="")]
    out = format_rows(rows, cursor="tok")
    lines = out.splitlines()
    assert lines[0] == ("2026-07-16T10:03:01Z ERROR payment-api "
                        "Payment failed: card declined "
                        "(id=AQAAA-1 trace=7031306439)")
    assert lines[1] == ("2026-07-16T10:03:02Z " + "m" * 300 +
                        "… (id=AQAAA-2)")
    assert lines[-1] == "2 events shown. more: pass cursor='tok'"
    assert format_rows(rows[:1], cursor=None).splitlines()[-1] == \
        "1 events shown."
    # empty result reads as a definite answer, not an error
    assert "no logs matched" in format_rows([], cursor=None)


def test_sigv4_matches_aws_reference_vectors():
    """Hand-rolled SigV4 must match AWS's own signatures. Expected values are
    INDEPENDENT: vector A is the published AWS SigV4 test-suite 'get-vanilla'
    signature; B and C were generated from botocore 1.x (AWS's reference
    implementation) with the clock pinned to the same instant."""
    from datetime import datetime, timezone
    from logs_mcp.sigv4 import sign_headers
    now = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)
    key, secret = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"

    # A — AWS test suite 'get-vanilla'
    h = sign_headers(method="GET", url="https://example.amazonaws.com/",
                     region="us-east-1", service="service", headers={},
                     body=b"", access_key=key, secret_key=secret, now=now)
    assert h["X-Amz-Date"] == "20150830T123600Z"
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d"
        "763fbf31")

    # B — CloudWatch-Logs-shaped POST (botocore-generated expected value)
    body = (b'{"queryString":"fields @timestamp, @message | limit 5",'
            b'"logGroupNames":["/app/payment"],"startTime":1752624000,'
            b'"endTime":1752627600}')
    cw_headers = {"Content-Type": "application/x-amz-json-1.1",
                  "X-Amz-Target": "Logs_20140328.StartQuery"}
    h = sign_headers(method="POST", url="https://logs.us-east-1.amazonaws.com/",
                     region="us-east-1", service="logs", headers=cw_headers,
                     body=body, access_key=key, secret_key=secret, now=now)
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/logs/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date;x-amz-target, "
        "Signature=f4c7b407adb52ee18547b7087b5939aeb2ecee9e972371cf54f34373"
        "10497bfb")

    # C — same with an STS session token (botocore-generated expected value)
    h = sign_headers(method="POST", url="https://logs.us-east-1.amazonaws.com/",
                     region="us-east-1", service="logs", headers=cw_headers,
                     body=body, access_key=key, secret_key=secret,
                     session_token="THETOKEN", now=now)
    assert h["X-Amz-Security-Token"] == "THETOKEN"
    assert h["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIDEXAMPLE/20150830/us-east-1/logs/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;"
        "x-amz-target, "
        "Signature=b1025fcbd82965417de207e58a838d0155f19a90cf531ad54f2ff5c7"
        "e644235d")


def test_cloudwatch_search_wire_contract_and_normalization():
    """CloudWatch Logs Insights: StartQuery → poll GetQueryResults until
    Complete → rows. Bare queries wrap as a message filter; @log's account
    prefix strips to the group name (the 'service'); Insights has no cursor
    so pagination is limit-only (cursor None)."""
    from datetime import datetime, timezone
    from logs_mcp.cloudwatch import CloudWatchBackend

    frm = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
    to = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)
    calls = []

    def handler(request):
        target = request.headers["X-Amz-Target"]
        calls.append(target)
        assert str(request.url) == "https://logs.eu-west-1.amazonaws.com/"
        assert request.headers["Content-Type"] == "application/x-amz-json-1.1"
        assert request.headers["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
        body = json.loads(request.content)
        if target == "Logs_20140328.StartQuery":
            assert body["logGroupNames"] == ["/app/payment"]
            assert body["startTime"] == int(frm.timestamp())
            assert body["endTime"] == int(to.timestamp())
            assert body["queryString"] == (
                "fields @timestamp, @message, @logStream, @log"
                ' | filter @message like "card declined"'
                " | sort @timestamp desc | limit 25")
            return httpx.Response(200, json={"queryId": "qid-1"})
        assert target == "Logs_20140328.GetQueryResults"
        assert body == {"queryId": "qid-1"}
        if calls.count("Logs_20140328.GetQueryResults") == 1:
            return httpx.Response(200, json={"status": "Running",
                                             "results": []})
        return httpx.Response(200, json={"status": "Complete", "results": [[
            {"field": "@timestamp", "value": "2026-07-16 10:03:01.000"},
            {"field": "@message", "value": "Payment failed: card declined"},
            {"field": "@logStream", "value": "app/i-0abc"},
            {"field": "@log", "value": "123456789012:/app/payment"},
            {"field": "@ptr", "value": "PTR-1"},
        ]]})

    b = CloudWatchBackend(access_key="AKIAEXAMPLE", secret_key="s",
                          region="eu-west-1", log_groups=("/app/payment",),
                          transport=httpx.MockTransport(handler),
                          poll_interval=0)
    rows, cursor = b.search("card declined", frm.isoformat(), to.isoformat(),
                            25)
    assert calls == ["Logs_20140328.StartQuery",
                     "Logs_20140328.GetQueryResults",
                     "Logs_20140328.GetQueryResults"]
    assert rows[0] == LogRow(ts="2026-07-16 10:03:01.000", status="",
                             service="/app/payment", host="app/i-0abc",
                             message="Payment failed: card declined",
                             log_id="PTR-1", trace_id="")
    assert cursor is None


def test_cloudwatch_aggregate_stats_query_and_buckets():
    """aggregate → a `stats count() by <facet>` Insights query; 'service'
    maps to @log (account prefix stripped in the buckets), counts sorted
    desc by the query itself."""
    from logs_mcp.cloudwatch import CloudWatchBackend
    seen = {}

    def handler(request):
        target = request.headers["X-Amz-Target"]
        body = json.loads(request.content)
        if target == "Logs_20140328.StartQuery":
            seen["query"] = body["queryString"]
            return httpx.Response(200, json={"queryId": "qid-2"})
        return httpx.Response(200, json={"status": "Complete", "results": [
            [{"field": "@log", "value": "123456789012:/app/payment"},
             {"field": "devcake_count", "value": "812"}],
            [{"field": "@log", "value": "123456789012:/app/checkout"},
             {"field": "devcake_count", "value": "133"}],
        ]})

    b = CloudWatchBackend(access_key="AKIAEXAMPLE", secret_key="s",
                          region="us-east-1", log_groups=("/app/payment",),
                          transport=httpx.MockTransport(handler),
                          poll_interval=0)
    out = b.aggregate("*", "now-1h", "now", "service", 10)
    assert seen["query"] == ("stats count() as devcake_count by @log"
                             " | sort devcake_count desc | limit 10")
    assert out == [("/app/payment", 812), ("/app/checkout", 133)]


def test_cloudwatch_behavior_edges():
    """The seams an operator/agent actually hits: relative-time parsing,
    log-group auto-discovery, failed queries, and the missing-creds hint."""
    from datetime import datetime, timezone
    from logs_mcp.cloudwatch import CloudWatchBackend, _epoch

    # relative times parse against an injected 'now'; garbage is actionable
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    assert _epoch("now", now) == int(now.timestamp())
    assert _epoch("now-15m", now) == int(now.timestamp()) - 900
    assert _epoch("now-2h", now) == int(now.timestamp()) - 7200
    with pytest.raises(BackendError, match="not understood"):
        _epoch("yesterday", now)

    # missing creds → the configuration hint at construction
    with pytest.raises(BackendError, match="secret env vars"):
        CloudWatchBackend(access_key="", secret_key="", region="")

    # no configured groups → DescribeLogGroups discovery feeds StartQuery
    calls = []

    def handler(request):
        target = request.headers["X-Amz-Target"]
        calls.append(target)
        body = json.loads(request.content)
        if target == "Logs_20140328.DescribeLogGroups":
            return httpx.Response(200, json={"logGroups": [
                {"logGroupName": "/discovered/a"}]})
        if target == "Logs_20140328.StartQuery":
            assert body["logGroupNames"] == ["/discovered/a"]
            return httpx.Response(200, json={"queryId": "qid-3"})
        return httpx.Response(200, json={"status": "Failed", "results": []})

    b = CloudWatchBackend(access_key="AKIAEXAMPLE", secret_key="s",
                          region="us-east-1",
                          transport=httpx.MockTransport(handler),
                          poll_interval=0)
    with pytest.raises(BackendError, match="failed"):
        b.search("*", "now-15m", "now", 5)
    assert calls[0] == "Logs_20140328.DescribeLogGroups"


def test_trace_filter_is_backend_specific():
    """Trace correlation is backend dialect and must live BELOW the seam
    (server.py composes queries via backend.trace_filter, never hardcoding
    Datadog syntax): Datadog filters the indexed @dd.trace_id facet;
    CloudWatch has no trace facet, so the id becomes a message-substring
    filter (X-Ray ids commonly appear in log lines)."""
    from logs_mcp.cloudwatch import CloudWatchBackend

    dd = _backend(lambda r: httpx.Response(200, json={"data": []}))
    assert dd.trace_filter("7031306439") == "@dd.trace_id:7031306439"
    cw = CloudWatchBackend(access_key="AKIAEXAMPLE", secret_key="s",
                           region="us-east-1", log_groups=("/g",))
    assert cw.trace_filter("1-6789-abc") == "1-6789-abc"


def test_make_backend_env_seam(monkeypatch):
    """The backend factory: server.py builds per tool call from env —
    DEVCAKE_LOGS_BACKEND selects the platform (default datadog), DD_* carry
    the credentials/site. Unknown platform → actionable BackendError; this
    is where loki/cloudwatch plug in later."""
    from logs_mcp.core import make_backend
    monkeypatch.setenv("DD_API_KEY", "k")
    monkeypatch.setenv("DD_APP_KEY", "a")
    monkeypatch.setenv("DD_SITE", "datadoghq.eu")
    monkeypatch.delenv("DEVCAKE_LOGS_BACKEND", raising=False)
    b = make_backend()
    assert isinstance(b, DatadogBackend)
    monkeypatch.setenv("DEVCAKE_LOGS_BACKEND", "loki")
    with pytest.raises(BackendError, match="loki"):
        make_backend()
    # missing keys surface the configuration hint through the same seam
    monkeypatch.setenv("DEVCAKE_LOGS_BACKEND", "datadog")
    monkeypatch.setenv("DD_API_KEY", "")
    with pytest.raises(BackendError, match="secret env vars"):
        make_backend()
    # cloudwatch backend: AWS_* env, optional DEVCAKE_LOGS_GROUPS csv
    from logs_mcp.cloudwatch import CloudWatchBackend
    monkeypatch.setenv("DEVCAKE_LOGS_BACKEND", "cloudwatch")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("DEVCAKE_LOGS_GROUPS", "/app/payment, /app/checkout")
    cw = make_backend()
    assert isinstance(cw, CloudWatchBackend)
    assert cw._groups == ("/app/payment", "/app/checkout")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    with pytest.raises(BackendError, match="secret env vars"):
        make_backend()
