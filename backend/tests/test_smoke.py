import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

# Add repository root to sys.path so demo can be imported
repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from demo.sdk_smoke import (
    BackendUnreachableError,
    DemoServiceStartupError,
    IngestionTimeoutError,
    check_logs,
    poll_for_spans,
    verify_smoke_checks,
    wait_for_backend_health,
    wait_for_server_started,
)


# ---------------------------------------------------------------------------
# Backend health check tests
# ---------------------------------------------------------------------------

def test_wait_for_backend_health_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/health"
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        # Should complete quickly without raising
        wait_for_backend_health(client, "http://test-server:4318", timeout=0.5, poll_interval=0.01)


def test_wait_for_backend_health_timeout_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(BackendUnreachableError) as exc_info:
            wait_for_backend_health(client, "http://test-server:4318", timeout=0.1, poll_interval=0.02)

    msg = str(exc_info.value)
    assert "http://test-server:4318/api/health" in msg
    assert "Ensure Heimdall is running" in msg
    assert "Connection refused" in msg


def test_wait_for_backend_health_timeout_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(BackendUnreachableError) as exc_info:
            wait_for_backend_health(client, "http://test-server:4318", timeout=0.1, poll_interval=0.02)

    msg = str(exc_info.value)
    assert "HTTP 503" in msg


def test_wait_for_backend_health_timeout_unexpected_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "degraded"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(BackendUnreachableError) as exc_info:
            wait_for_backend_health(client, "http://test-server:4318", timeout=0.1, poll_interval=0.02)

    assert "Unexpected JSON" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Demo service startup tests
# ---------------------------------------------------------------------------

class DummyServer:
    def __init__(self, started: bool = False):
        self.started = started


def test_wait_for_server_started_success():
    server = DummyServer(started=True)
    wait_for_server_started(server, port=8001, timeout=0.5, poll_interval=0.01)


def test_wait_for_server_started_timeout():
    server = DummyServer(started=False)
    with pytest.raises(DemoServiceStartupError) as exc_info:
        wait_for_server_started(server, port=8001, timeout=0.05, poll_interval=0.01)

    assert "port 8001" in str(exc_info.value)
    assert "already in use" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bounded span polling tests
# ---------------------------------------------------------------------------

def test_poll_for_spans_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/spans"
        spans = [
            {"service": "test-svc", "trace_id": f"t{i}", "start_ms": 100 + i}
            for i in range(6)
        ]
        return httpx.Response(200, json={"spans": spans})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        spans = poll_for_spans(
            client,
            "http://test-server:4318",
            service="test-svc",
            min_spans=6,
            min_traces=6,
            timeout=0.5,
            poll_interval=0.02,
        )

    assert len(spans) == 6


def test_poll_for_spans_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"spans": []})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(IngestionTimeoutError) as exc_info:
            poll_for_spans(
                client,
                "http://test-server:4318",
                service="test-svc",
                min_spans=6,
                min_traces=6,
                timeout=0.1,
                poll_interval=0.02,
            )

    msg = str(exc_info.value)
    assert "Telemetry did not become queryable" in msg
    assert "service 'test-svc'" in msg
    assert "expected at least 6 spans and 6 traces" in msg


def test_poll_for_spans_rerun_filtering():
    def handler(request: httpx.Request) -> httpx.Response:
        # Returns 6 old spans (start_ms=100) and 6 new spans (start_ms=500)
        old_spans = [{"service": "test-svc", "trace_id": f"old_{i}", "start_ms": 100} for i in range(6)]
        new_spans = [{"service": "test-svc", "trace_id": f"new_{i}", "start_ms": 500} for i in range(6)]
        return httpx.Response(200, json={"spans": old_spans + new_spans})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        spans = poll_for_spans(
            client,
            "http://test-server:4318",
            service="test-svc",
            min_spans=6,
            min_traces=6,
            run_start_ms=400,
            timeout=0.5,
            poll_interval=0.02,
        )

    # Only new spans should be returned
    assert len(spans) == 6
    assert all(s["start_ms"] >= 400 for s in spans)
    assert {s["trace_id"] for s in spans} == {f"new_{i}" for i in range(6)}


def test_poll_for_spans_never_accepts_stale_telemetry():
    # Database has 10 stale spans for test-svc (start_ms=100)
    stale_spans = [{"service": "test-svc", "trace_id": f"stale_{i}", "start_ms": 100} for i in range(10)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"spans": stale_spans})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        # Polling with run_start_ms=500 should never accept stale spans and must time out
        with pytest.raises(IngestionTimeoutError) as exc_info:
            poll_for_spans(
                client,
                "http://test-server:4318",
                service="test-svc",
                min_spans=6,
                min_traces=6,
                run_start_ms=500,
                timeout=0.1,
                poll_interval=0.02,
            )

    msg = str(exc_info.value)
    assert "Found 0 spans across 0 traces for service 'test-svc' (after run_start_ms=500.0)" in msg
    assert "expected at least 6 spans and 6 traces" in msg


def test_poll_for_spans_waits_for_gradually_arriving_spans():
    call_count = 0
    stale = [{"service": "test-svc", "trace_id": f"old_{i}", "start_ms": 100} for i in range(4)]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Poll 1: 2 spans arrive
            current = [{"service": "test-svc", "trace_id": f"new_{i}", "start_ms": 600} for i in range(2)]
        elif call_count == 2:
            # Poll 2: 4 spans arrive
            current = [{"service": "test-svc", "trace_id": f"new_{i}", "start_ms": 600} for i in range(4)]
        else:
            # Poll 3: all 6 spans arrive
            current = [{"service": "test-svc", "trace_id": f"new_{i}", "start_ms": 600} for i in range(6)]
        return httpx.Response(200, json={"spans": stale + current})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        spans = poll_for_spans(
            client,
            "http://test-server:4318",
            service="test-svc",
            min_spans=6,
            min_traces=6,
            run_start_ms=500,
            timeout=1.0,
            poll_interval=0.02,
        )

    assert len(spans) == 6
    assert all(s["start_ms"] >= 500 for s in spans)
    assert {s["trace_id"] for s in spans} == {f"new_{i}" for i in range(6)}
    assert call_count >= 3


def test_poll_for_spans_times_out_on_incomplete_current_run():
    # Only 5 spans arrive for the current run, when 6 are expected
    stale = [{"service": "test-svc", "trace_id": f"old_{i}", "start_ms": 100} for i in range(6)]
    current = [{"service": "test-svc", "trace_id": f"new_{i}", "start_ms": 600} for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"spans": stale + current})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(IngestionTimeoutError) as exc_info:
            poll_for_spans(
                client,
                "http://test-server:4318",
                service="test-svc",
                min_spans=6,
                min_traces=6,
                run_start_ms=500,
                timeout=0.1,
                poll_interval=0.02,
            )

    msg = str(exc_info.value)
    assert "Found 5 spans across 5 traces for service 'test-svc' (after run_start_ms=500.0)" in msg
    assert "expected at least 6 spans and 6 traces" in msg


# ---------------------------------------------------------------------------
# Verification assertion tests
# ---------------------------------------------------------------------------

def make_sample_spans():
    """Build a synthetic set of spans satisfying all 6 smoke checks."""
    spans: list[dict[str, Any]] = []
    for i in range(1, 7):
        trace_id = f"trace_{i:04d}"
        root_span_id = f"root_{i:04d}"
        child_span_id = f"child_{i:04d}"

        # Root span
        spans.append({
            "service": "test-service",
            "trace_id": trace_id,
            "span_id": root_span_id,
            "parent_span_id": None,
            "name": f"GET /order/{i}",
            "http_status": 200 if i % 3 != 0 else 502,
            "status_code": "OK" if i % 3 != 0 else "ERROR",
            "start_ms": 1000 + i * 10,
            "duration_ms": 5.0,
        })
        # Child span
        spans.append({
            "service": "test-service",
            "trace_id": trace_id,
            "span_id": child_span_id,
            "parent_span_id": root_span_id,
            "name": "chargeCard",
            "http_status": None,
            "status_code": "OK" if i % 3 != 0 else "ERROR",
            "start_ms": 1002 + i * 10,
            "duration_ms": 2.0,
        })
    return spans


def test_verify_smoke_checks_all_pass():
    spans = make_sample_spans()
    checks = verify_smoke_checks(spans, expected_requests=6)
    assert all(checks.values()), f"Expected all checks to pass, got: {checks}"


def test_verify_smoke_checks_detects_wrong_trace_count():
    spans = make_sample_spans()
    # Expect 7 traces but only 6 are present
    checks = verify_smoke_checks(spans, expected_requests=7)
    assert checks["one trace per request"] is False
    assert checks["spans arrived"] is True


def test_verify_smoke_checks_detects_missing_errors():
    spans = make_sample_spans()
    for s in spans:
        s["status_code"] = "OK"

    checks = verify_smoke_checks(spans, expected_requests=6)
    assert checks["errors recorded"] is False


def test_verify_smoke_checks_detects_missing_roots():
    spans = make_sample_spans()
    for s in spans:
        s["parent_span_id"] = "some-parent-id"

    checks = verify_smoke_checks(spans, expected_requests=6)
    assert checks["roots have no parent"] is False


# ---------------------------------------------------------------------------
# Log query check tests
# ---------------------------------------------------------------------------

def test_check_logs_404_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/logs"
        return httpx.Response(404, json={"detail": "Not Found"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        status, log_checks = check_logs(client, "http://test-server:4318", "test-svc", expected_requests=6)

    assert "not implemented yet" in status
    assert log_checks == {}


def test_check_logs_200_evaluated():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/logs"
        # 6 requests + 2 errors + 1 startup = 9 logs
        logs = []
        for i in range(1, 7):
            logs.append({"service": "test-svc", "severity": "INFO", "trace_id": f"t{i}", "body": f"order {i}"})
            if i % 3 == 0:
                logs.append({"service": "test-svc", "severity": "ERROR", "trace_id": f"t{i}", "body": f"declined {i}"})
        logs.append({"service": "test-svc", "severity": "INFO", "trace_id": None, "body": "startup"})
        return httpx.Response(200, json={"logs": logs})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        status, log_checks = check_logs(client, "http://test-server:4318", "test-svc", expected_requests=6)

    assert status == "completed"
    assert log_checks["logs arrived"] is True
    assert log_checks["logs carry trace IDs"] is True
    assert log_checks["error logs recorded"] is True
