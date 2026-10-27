import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from heimdall.api.main import app
from tests.test_decoder import otlp_span, request_bytes

PROTOBUF = {"Content-Type": "application/x-protobuf"}
EPOCH_WINDOW = {"from_ms": 0, "to_ms": 10_000}
MS = 1_000_000
GW, ORDER, PAY = bytes.fromhex("0000000000000001"), bytes.fromhex("0000000000000002"), bytes.fromhex("0000000000000003")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


def get_map(client, params=EPOCH_WINDOW):
    r = client.get("/api/service-map", params=params)
    assert r.status_code == 200
    return r.json()


def ingest_chain(client, payment_error=False, start_ns=1 * MS):
    """One request through gateway -> order-service -> payment-service, sent as OTLP."""
    pay_status = Status(code=Status.STATUS_CODE_ERROR) if payment_error else Status()
    body = request_bytes({
        "gateway": [otlp_span(span_id=GW, start=start_ns, end=start_ns + 100 * MS)],
        "order-service": [otlp_span(span_id=ORDER, parent=GW, name="POST /orders",
                                    start=start_ns + 10 * MS, end=start_ns + 90 * MS)],
        "payment-service": [otlp_span(span_id=PAY, parent=ORDER, name="POST /charge",
                                      start=start_ns + 30 * MS, end=start_ns + 50 * MS, status=pay_status)],
    })
    assert client.post("/v1/traces", content=body, headers=PROTOBUF).status_code == 200


def wait_for_edges(client, n, params=EPOCH_WINDOW):
    deadline = time.monotonic() + 2
    while True:
        result = get_map(client, params)
        if len(result["edges"]) >= n:
            return result
        assert time.monotonic() < deadline, f"expected {n} edges, got {result}"
        time.sleep(0.02)


def test_service_map_contract_shape(client):
    ingest_chain(client, payment_error=True)
    result = wait_for_edges(client, 2)
    assert set(result) == {"nodes", "edges"}
    assert [n["id"] for n in result["nodes"]] == ["gateway", "order-service", "payment-service"]
    pay_edge = next(e for e in result["edges"] if e["target"] == "payment-service")
    assert pay_edge == {
        "source": "order-service",
        "target": "payment-service",
        "call_count": 1,
        "avg_duration_ms": 20.0,
        "p95_duration_ms": 20.0,
        "error_count": 1,
        "error_rate": 1.0,
    }
    assert set(result["nodes"][0]) == {
        "id", "request_count", "error_count", "error_rate", "avg_duration_ms", "p95_duration_ms",
    }


def test_default_window_uses_now(client):
    ingest_chain(client, start_ns=time.time_ns() - 200 * MS)
    result = wait_for_edges(client, 2, params={})
    assert {(e["source"], e["target"]) for e in result["edges"]} == {
        ("gateway", "order-service"), ("order-service", "payment-service"),
    }


def test_empty_map(client):
    assert get_map(client) == {"nodes": [], "edges": []}
