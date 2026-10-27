import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from heimdall.api.main import app
from tests.test_decoder import CHILD_ID, ROOT_ID, TRACE_ID, kv, otlp_span, request_bytes

PROTOBUF = {"Content-Type": "application/x-protobuf"}
EPOCH_WINDOW = {"from_ms": 0, "to_ms": 10_000}
MS = 1_000_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api_topology.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


def ingest_spans(client, spans_by_service):
    r = client.post("/v1/traces", content=request_bytes(spans_by_service), headers=PROTOBUF)
    assert r.status_code == 200


def wait_for_edges(client, expected_edges, params=EPOCH_WINDOW, timeout=2.0):
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
# API tests
# ---------------------------------------------------------------------------

def test_get_service_map_empty(client):
    r = client.get("/api/service-map", params=EPOCH_WINDOW)
    assert r.status_code == 200
    assert r.json() == {"nodes": [], "edges": []}


def test_get_service_map_populated_nodes_and_edges(client):
    # Ingest gateway -> order-service (error)
    ingest_spans(client, {
        "gateway": [otlp_span(
            start=1 * MS, end=4 * MS,
            attrs=[kv("http.response.status_code", 500), kv("http.route", "/order")],
            status=Status(code=Status.STATUS_CODE_ERROR),
        )],
        "order-service": [otlp_span(
            span_id=CHILD_ID, parent=ROOT_ID, name="createOrder",
            start=2 * MS, end=3 * MS,
            status=Status(code=Status.STATUS_CODE_ERROR, message="internal failure"),
        )],
    })

    data = wait_for_edges(client, 1)
    assert data["nodes"] == [
        {"id": "gateway", "service": "gateway"},
        {"id": "order-service", "service": "order-service"},
    ]
    assert len(data["edges"]) == 1
    edge = data["edges"][0]
    assert edge["source"] == "gateway"
    assert edge["target"] == "order-service"
    assert edge["call_count"] == 1
    assert edge["avg_latency_ms"] == 1.0  # 3ms - 2ms = 1ms
    assert edge["error_count"] == 1
    assert edge["error_rate"] == 1.0


def test_get_service_map_time_window_filtering(client):
    now_ns = time.time_ns()
    # Ingest span now
    ingest_spans(client, {
        "gateway": [otlp_span(start=now_ns - 20 * MS, end=now_ns)],
        "payment": [otlp_span(span_id=CHILD_ID, parent=ROOT_ID, start=now_ns - 10 * MS, end=now_ns)],
    })

    # Default window (no params: last 1 hour) finds the recent spans
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        r = client.get("/api/service-map")
        assert r.status_code == 200
        if len(r.json().get("edges", [])) >= 1:
            break
        time.sleep(0.02)
    assert len(r.json()["edges"]) == 1
    assert r.json()["edges"][0]["source"] == "gateway"
    assert r.json()["edges"][0]["target"] == "payment"

    # A past window (epoch) does not find the current spans
    r_past = client.get("/api/service-map", params=EPOCH_WINDOW)
    assert r_past.status_code == 200
    assert r_past.json()["edges"] == []


def test_get_service_map_invalid_time_range_returns_422(client):
    r = client.get("/api/service-map", params={"from_ms": 5000, "to_ms": 1000})
    assert r.status_code == 422
    assert "from_ms cannot be greater than to_ms" in r.json()["detail"]


def test_existing_endpoints_unaffected(client):
    # Verify health, traces, spans, and logs still function normally
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/traces").status_code == 200
    assert client.get("/api/logs").status_code == 200
    assert client.get("/api/services").status_code == 200
    assert client.get("/api/spans").status_code == 200
