"""End-to-End integration and reliability tests for Heimdall.

Covers:
1. Empty database handling across all 7 endpoints.
2. Query validation bounds (from_ms > to_ms, negative limits, invalid regex, limit clamping).
3. Pipeline resilience (QueueFull backpressure, bounded subscriber queue overflow, subscriber cleanup).
4. Full distributed 3-service trace and log correlation (gateway -> order -> payment).
5. Fault injection and error-rate calculations in service-map and traces.
6. Concurrent ingestion and querying under DuckDB transaction isolation.
"""

import asyncio
import concurrent.futures
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan, Status

from heimdall.api.main import app
from heimdall.ingest.models import LogRecord as ModelLogRecord, Span as ModelSpan
from heimdall.ingest.pipeline import BatchWriter, QueueFull, Subscriber
from tests.test_decoder import kv, otlp_span, request_bytes
from tests.test_decoder_logs import logs_request_bytes, otlp_log

PROTOBUF = {"Content-Type": "application/x-protobuf"}
MS = 1_000_000  # nanoseconds in 1 ms
EPOCH_WINDOW = {"from_ms": 0, "to_ms": 1_000_000}

# Unique trace and span IDs for E2E testing
E2E_TRACE_ID = bytes.fromhex("11112222333344445555666677778888")
ERR_TRACE_ID = bytes.fromhex("99998888777766665555444433332222")

GW_SERVER_ID = bytes.fromhex("0000000000000001")
GW_CLIENT_ID = bytes.fromhex("0000000000000002")
ORD_SERVER_ID = bytes.fromhex("0000000000000003")
ORD_INTERNAL_ID = bytes.fromhex("0000000000000004")
ORD_CLIENT_ID = bytes.fromhex("0000000000000005")
PAY_SERVER_ID = bytes.fromhex("0000000000000006")
PAY_INTERNAL_ID = bytes.fromhex("0000000000000007")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "integration.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


def wait_for_traces(client, expected, params=None, timeout=2.0):
    params = params if params is not None else EPOCH_WINDOW
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/traces", params=params)
        assert r.status_code == 200
        traces = r.json().get("traces", [])
        if len(traces) >= expected:
            return traces
        time.sleep(0.02)
    raise AssertionError(f"expected {expected} traces, got {len(traces)}")


def wait_for_logs(client, expected, params=None, timeout=2.0):
    params = params or {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/logs", params=params)
        assert r.status_code == 200
        logs = r.json().get("logs", [])
        if len(logs) >= expected:
            return logs
        time.sleep(0.02)
    raise AssertionError(f"expected {expected} logs, got {len(logs)}")


def wait_for_service_map(client, expected_edges, params=None, timeout=2.0):
    params = params if params is not None else EPOCH_WINDOW
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get("/api/service-map", params=params)
        assert r.status_code == 200
        data = r.json()
        if len(data.get("edges", [])) >= expected_edges:
            return data
        time.sleep(0.02)
    raise AssertionError(f"expected {expected_edges} edges, got {len(data.get('edges', []))}")


# ---------------------------------------------------------------------------
# 1. Empty Database Handling Across All Endpoints
# ---------------------------------------------------------------------------

def test_empty_database_all_endpoints(client):
    """Verify all 7 query endpoints return correct schema and status on an empty database."""
    # Health check
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.json() == {"status": "ok"}

    # Services
    r_services = client.get("/api/services")
    assert r_services.status_code == 200
    assert r_services.json() == {"services": []}

    # Traces list
    r_traces = client.get("/api/traces")
    assert r_traces.status_code == 200
    assert r_traces.json() == {"traces": []}

    # Unknown trace details (404)
    r_unknown_trace = client.get("/api/traces/1234567890abcdef1234567890abcdef")
    assert r_unknown_trace.status_code == 404
    assert r_unknown_trace.json() == {"detail": "trace not found"}

    # Spans debug endpoint
    r_spans = client.get("/api/spans")
    assert r_spans.status_code == 200
    assert r_spans.json() == {"spans": []}

    # Logs
    r_logs = client.get("/api/logs")
    assert r_logs.status_code == 200
    assert r_logs.json() == {"logs": []}

    # Service map
    r_smap = client.get("/api/service-map")
    assert r_smap.status_code == 200
    assert r_smap.json() == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# 2. Query Validation and Edge Cases Across Endpoints
# ---------------------------------------------------------------------------

def test_query_validation_from_ms_greater_than_to_ms(client):
    """Ensure consistent 422 error across all time-filtered query endpoints when from_ms > to_ms."""
    bad_window = {"from_ms": 5000, "to_ms": 1000}

    # /api/traces
    r_traces = client.get("/api/traces", params=bad_window)
    assert r_traces.status_code == 422
    assert "from_ms cannot be greater than to_ms" in r_traces.json()["detail"]

    # /api/services
    r_services = client.get("/api/services", params=bad_window)
    assert r_services.status_code == 422
    assert "from_ms cannot be greater than to_ms" in r_services.json()["detail"]

    # /api/logs
    r_logs = client.get("/api/logs", params=bad_window)
    assert r_logs.status_code == 422
    assert "from_ms cannot be greater than to_ms" in r_logs.json()["detail"]

    # /api/service-map
    r_smap = client.get("/api/service-map", params=bad_window)
    assert r_smap.status_code == 422
    assert "from_ms cannot be greater than to_ms" in r_smap.json()["detail"]


def test_query_validation_limits_and_parameters(client):
    """Validate limit constraints and clamping behavior."""
    # Zero or negative limits return 422
    assert client.get("/api/traces", params={"limit": 0}).status_code == 422
    assert client.get("/api/logs", params={"limit": 0}).status_code == 422
    assert client.get("/api/spans", params={"limit": 0}).status_code == 422
    assert client.get("/api/traces", params={"min_duration_ms": -10}).status_code == 422

    # Invalid regex returns 422
    r_regex = client.get("/api/logs", params={"regex": "*invalid_regex"})
    assert r_regex.status_code == 422
    assert "Invalid regular expression" in r_regex.json()["detail"]

    # Oversized limits are accepted and safely clamped without error
    assert client.get("/api/traces", params={"limit": 999999}).status_code == 200
    assert client.get("/api/logs", params={"limit": 999999}).status_code == 200
    assert client.get("/api/spans", params={"limit": 999999}).status_code == 200


# ---------------------------------------------------------------------------
# 3. Pipeline Resilience, Memory Bounds & Subscriber Overflow
# ---------------------------------------------------------------------------

def test_subscriber_bounded_queue_overflow_policy():
    """Verify Subscriber drops oldest queued record on overflow without blocking."""
    sub = Subscriber(maxsize=3)
    assert sub.dropped == 0

    # Put 3 items to fill queue
    sub.put({"id": 1})
    sub.put({"id": 2})
    sub.put({"id": 3})
    assert sub.queue.qsize() == 3
    assert sub.dropped == 0

    # 4th item forces drop of item 1
    sub.put({"id": 4})
    assert sub.queue.qsize() == 3
    assert sub.dropped == 1

    # 5th item forces drop of item 2
    sub.put({"id": 5})
    assert sub.queue.qsize() == 3
    assert sub.dropped == 2

    # Read remaining items: should be 3, 4, 5
    items = [sub.queue.get_nowait()["id"] for _ in range(3)]
    assert items == [3, 4, 5]

    # Test sentinel None with full queue
    sub.put({"id": 6})
    sub.put({"id": 7})
    sub.put({"id": 8})
    sub.put(None)  # shutdown sentinel
    assert sub.queue.qsize() == 3
    # Verify None is queued at the end
    retrieved = []
    while not sub.queue.empty():
        retrieved.append(sub.queue.get_nowait())
    assert retrieved[-1] is None


def test_batch_writer_backpressure_queue_full():
    """Verify BatchWriter enforces max_pending record limit and raises QueueFull."""
    conn = app.state.conn
    writer = BatchWriter(conn, "spans", max_pending=5, flush_interval=60.0)

    # Dummy span helper
    def make_span(i):
        return ModelSpan(
            trace_id="a" * 32, span_id=f"{i:016x}", parent_span_id=None,
            service="test", name="op", kind="SERVER",
            start_time=1000, end_time=2000, duration_ns=1000,
            status_code="OK", http_status=200, attributes={},
        )

    # Submit 4 records into queue
    writer.submit([make_span(1), make_span(2), make_span(3), make_span(4)])
    assert writer.pending == 4
    assert writer.stats.received == 4
    assert writer.stats.rejected == 0

    # Next batch of 2 records exceeds max_pending=5 -> raises QueueFull
    with pytest.raises(QueueFull, match="spans ingest queue is full"):
        writer.submit([make_span(5), make_span(6)])

    assert writer.stats.rejected == 2
    assert writer.pending == 4


def test_stream_logs_disconnect_cleans_subscribers(client):
    """Verify client disconnect on SSE stream unregisters the subscriber from BatchWriter."""
    writer = app.state.log_writer
    initial_subscribers = len(writer._subscribers)

    with client.stream("GET", "/api/logs/stream") as stream:
        assert stream.status_code == 200
        assert len(writer._subscribers) == initial_subscribers + 1

    # After stream block ends, generator finally block removes subscriber
    deadline = time.monotonic() + 1.0
    while len(writer._subscribers) > initial_subscribers and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(writer._subscribers) == initial_subscribers


# ---------------------------------------------------------------------------
# 4. End-to-End Correlated Distributed Tracing & Logs
# ---------------------------------------------------------------------------

def test_e2e_distributed_3service_trace_and_log_correlation(client):
    """Verify gateway -> order -> payment distributed trace and correlated logs."""
    # Construct 7 spans representing the full multi-service transaction:
    # gateway (SERVER) -> gateway (CLIENT) -> order (SERVER) -> order (INTERNAL) -> order (CLIENT) -> payment (SERVER) -> payment (INTERNAL)
    spans_payload = {
        "gateway": [
            otlp_span(
                span_id=GW_SERVER_ID, parent=b"", name="POST /order",
                kind=OtlpSpan.SPAN_KIND_SERVER, start=100 * MS, end=250 * MS,
                attrs=[kv("http.route", "/order"), kv("http.response.status_code", 200)],
            ),
            otlp_span(
                span_id=GW_CLIENT_ID, parent=GW_SERVER_ID, name="POST http://order:8002/order",
                kind=OtlpSpan.SPAN_KIND_CLIENT, start=110 * MS, end=240 * MS,
                attrs=[kv("peer.service", "order")],
            ),
        ],
        "order": [
            otlp_span(
                span_id=ORD_SERVER_ID, parent=GW_CLIENT_ID, name="POST /order",
                kind=OtlpSpan.SPAN_KIND_SERVER, start=115 * MS, end=235 * MS,
                attrs=[kv("order.id", "ord-101")],
            ),
            otlp_span(
                span_id=ORD_INTERNAL_ID, parent=ORD_SERVER_ID, name="validateOrder",
                kind=OtlpSpan.SPAN_KIND_INTERNAL, start=120 * MS, end=140 * MS,
            ),
            otlp_span(
                span_id=ORD_CLIENT_ID, parent=ORD_SERVER_ID, name="POST http://payment:8003/pay",
                kind=OtlpSpan.SPAN_KIND_CLIENT, start=145 * MS, end=230 * MS,
                attrs=[kv("peer.service", "payment")],
            ),
        ],
        "payment": [
            otlp_span(
                span_id=PAY_SERVER_ID, parent=ORD_CLIENT_ID, name="POST /pay",
                kind=OtlpSpan.SPAN_KIND_SERVER, start=150 * MS, end=225 * MS,
                attrs=[kv("payment.amount", 99.99)],
            ),
            otlp_span(
                span_id=PAY_INTERNAL_ID, parent=PAY_SERVER_ID, name="authorizeCard",
                kind=OtlpSpan.SPAN_KIND_INTERNAL, start=155 * MS, end=220 * MS,
                attrs=[kv("payment.provider", "stripe")],
            ),
        ],
    }

    # Override trace_id across all spans to match E2E_TRACE_ID
    for service_spans in spans_payload.values():
        for s in service_spans:
            s.trace_id = E2E_TRACE_ID

    # Correlated log records with matching trace_id and span_id
    logs_payload = {
        "gateway": [
            otlp_log(
                "Ingress accepted customer order",
                severity=9, time=105 * MS, trace_id=E2E_TRACE_ID, span_id=GW_SERVER_ID,
                attrs=[kv("service", "gateway")],
            ),
        ],
        "order": [
            otlp_log(
                "Order ord-101 validated successfully",
                severity=9, time=135 * MS, trace_id=E2E_TRACE_ID, span_id=ORD_SERVER_ID,
                attrs=[kv("service", "order"), kv("order.id", "ord-101")],
            ),
        ],
        "payment": [
            otlp_log(
                "Card authorization completed via stripe",
                severity=9, time=200 * MS, trace_id=E2E_TRACE_ID, span_id=PAY_SERVER_ID,
                attrs=[kv("service", "payment"), kv("status", "approved")],
            ),
        ],
    }

    # Ingest traces and logs
    r_traces = client.post("/v1/traces", content=request_bytes(spans_payload), headers=PROTOBUF)
    assert r_traces.status_code == 200
    r_logs = client.post("/v1/logs", content=logs_request_bytes(logs_payload), headers=PROTOBUF)
    assert r_logs.status_code == 200

    # Wait for data to persist
    traces = wait_for_traces(client, 1)
    assert len(traces) == 1
    t = traces[0]
    assert t["trace_id"] == E2E_TRACE_ID.hex()
    assert t["root_service"] == "gateway"
    assert t["root_name"] == "POST /order"
    assert t["span_count"] == 7
    assert t["service_count"] == 3
    assert t["error_count"] == 0
    assert t["has_error"] is False

    # Verify trace details (waterfall contract)
    r_trace = client.get(f"/api/traces/{E2E_TRACE_ID.hex()}")
    assert r_trace.status_code == 200
    trace_data = r_trace.json()
    assert len(trace_data["spans"]) == 7
    span_names = {s["name"] for s in trace_data["spans"]}
    assert span_names == {
        "POST /order", "POST http://order:8002/order",
        "validateOrder", "POST http://payment:8003/pay",
        "POST /pay", "authorizeCard",
    }

    # Verify correlated logs query by trace_id
    logs = wait_for_logs(client, 3, params={"trace_id": E2E_TRACE_ID.hex()})
    assert len(logs) == 3
    # Newest first
    assert [l["service"] for l in logs] == ["payment", "order", "gateway"]
    assert "Card authorization" in logs[0]["body"]
    assert "Order ord-101" in logs[1]["body"]
    assert "Ingress accepted" in logs[2]["body"]

    # Verify service map topology
    smap = wait_for_service_map(client, 2)
    node_ids = {n["id"] for n in smap["nodes"]}
    assert node_ids == {"gateway", "order", "payment"}

    # Assert edges: gateway -> order and order -> payment
    edge_map = {(e["source"], e["target"]): e for e in smap["edges"]}
    assert ("gateway", "order") in edge_map
    assert ("order", "payment") in edge_map

    gw_ord = edge_map[("gateway", "order")]
    assert gw_ord["call_count"] == 1
    assert gw_ord["error_count"] == 0
    assert gw_ord["error_rate"] == 0.0

    ord_pay = edge_map[("order", "payment")]
    assert ord_pay["call_count"] == 1
    assert ord_pay["error_count"] == 0
    assert ord_pay["error_rate"] == 0.0


# ---------------------------------------------------------------------------
# 5. Fault Injection & Error Rate Semantics
# ---------------------------------------------------------------------------

def test_fault_injection_and_error_rate_topology(client):
    """Verify injected error in payment updates error rates and edge highlights."""
    # Ingest a failed trace: payment fails with 500
    err_spans = {
        "gateway": [
            otlp_span(
                span_id=GW_SERVER_ID, parent=b"", name="POST /order",
                start=300 * MS, end=400 * MS,
                status=Status(code=Status.STATUS_CODE_ERROR, message="downstream error"),
                attrs=[kv("http.response.status_code", 500)],
            ),
        ],
        "order": [
            otlp_span(
                span_id=ORD_SERVER_ID, parent=GW_SERVER_ID, name="POST /order",
                start=310 * MS, end=390 * MS,
                status=Status(code=Status.STATUS_CODE_ERROR, message="payment failed"),
                attrs=[kv("http.response.status_code", 500)],
            ),
        ],
        "payment": [
            otlp_span(
                span_id=PAY_SERVER_ID, parent=ORD_SERVER_ID, name="POST /pay",
                start=320 * MS, end=380 * MS,
                status=Status(code=Status.STATUS_CODE_ERROR, message="internal provider error"),
                attrs=[kv("http.response.status_code", 500)],
            ),
        ],
    }

    for service_spans in err_spans.values():
        for s in service_spans:
            s.trace_id = ERR_TRACE_ID

    err_logs = {
        "payment": [
            otlp_log(
                "Stripe API network timeout 500",
                severity=17, time=350 * MS, trace_id=ERR_TRACE_ID, span_id=PAY_SERVER_ID,
            ),
        ],
    }

    client.post("/v1/traces", content=request_bytes(err_spans), headers=PROTOBUF)
    client.post("/v1/logs", content=logs_request_bytes(err_logs), headers=PROTOBUF)

    # Wait for traces
    wait_for_traces(client, 1, params={**EPOCH_WINDOW, "service": "payment"})

    # errors_only filter on /api/traces isolates the error trace
    err_traces = client.get("/api/traces", params={**EPOCH_WINDOW, "errors_only": "true"}).json()["traces"]
    assert len(err_traces) == 1
    assert err_traces[0]["trace_id"] == ERR_TRACE_ID.hex()
    assert err_traces[0]["has_error"] is True

    # Error log is retrievable by severity
    err_log_res = client.get("/api/logs", params={"severity": "ERROR"}).json()["logs"]
    assert any("Stripe API network timeout 500" in l["body"] for l in err_log_res)

    # Service map edge order -> payment reflects error
    smap = wait_for_service_map(client, 2)
    edge_map = {(e["source"], e["target"]): e for e in smap["edges"]}
    ord_pay = edge_map[("order", "payment")]
    assert ord_pay["error_count"] >= 1
    assert ord_pay["error_rate"] > 0.0


# ---------------------------------------------------------------------------
# 6. Concurrent Ingest and Querying Under DuckDB
# ---------------------------------------------------------------------------

def test_concurrent_ingest_and_querying(client):
    """Verify DuckDB concurrency: concurrent span ingestion and queries complete cleanly."""
    def do_ingest(i):
        trace_id = bytes.fromhex(f"{i:032x}")
        body = request_bytes({
            "service-concurrent": [
                otlp_span(
                    span_id=bytes.fromhex(f"{i:016x}"),
                    parent=b"",
                    name=f"concurrent-{i}",
                    start=(500 + i) * MS,
                    end=(510 + i) * MS,
                ),
            ],
        })
        r = client.post("/v1/traces", content=body, headers=PROTOBUF)
        assert r.status_code == 200

    def do_query():
        r1 = client.get("/api/traces", params=EPOCH_WINDOW)
        r2 = client.get("/api/services")
        r3 = client.get("/api/service-map", params=EPOCH_WINDOW)
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = []
        for i in range(1, 15):
            futures.append(executor.submit(do_ingest, i))
            futures.append(executor.submit(do_query))

        for f in concurrent.futures.as_completed(futures):
            f.result()  # Will raise if any thread encountered an exception
