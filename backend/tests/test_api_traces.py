import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.trace.v1.trace_pb2 import Status

from heimdall.api.main import app
from tests.test_decoder import CHILD_ID, ROOT_ID, TRACE_ID, kv, otlp_span, request_bytes

PROTOBUF = {"Content-Type": "application/x-protobuf"}
EPOCH_WINDOW = {"from_ms": 0, "to_ms": 10_000}  # otlp_span() defaults to times near 1970
MS = 1_000_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


def ingest(client, spans_by_service):
    r = client.post("/v1/traces", content=request_bytes(spans_by_service), headers=PROTOBUF)
    assert r.status_code == 200


def wait_for_traces(client, expected, params=EPOCH_WINDOW, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        traces = client.get("/api/traces", params=params).json()["traces"]
        if len(traces) >= expected:
            return traces
        time.sleep(0.02)
    raise AssertionError(f"expected {expected} traces, got {len(traces)}")


@pytest.fixture
def order_trace(client):
    """gateway POST /order (500) -> payment chargeCard (ERROR). 1 ms .. 4 ms."""
    ingest(client, {
        "gateway": [otlp_span(
            start=1 * MS, end=4 * MS,
            attrs=[kv("http.response.status_code", 500), kv("http.route", "/order")],
            status=Status(code=Status.STATUS_CODE_ERROR),
        )],
        "payment-service": [otlp_span(
            span_id=CHILD_ID, parent=ROOT_ID, name="chargeCard",
            start=2 * MS, end=3 * MS,
            attrs=[kv("payment.provider", "stripe")],
            status=Status(code=Status.STATUS_CODE_ERROR, message="card declined"),
        )],
    })
    wait_for_traces(client, 1)


# --- GET /api/traces ---------------------------------------------------------

def test_list_traces_contract_shape(client, order_trace):
    [t] = client.get("/api/traces", params=EPOCH_WINDOW).json()["traces"]
    assert t == {
        "trace_id": TRACE_ID.hex(),
        "root_service": "gateway",
        "root_name": "POST /order",
        "start_ms": 1.0,
        "duration_ms": 3.0,
        "span_count": 2,
        "service_count": 2,
        "error_count": 2,
        "has_error": True,
        "http_status": 500,
    }


def test_list_traces_filters_pass_through(client, order_trace):
    def ids(**params):
        r = client.get("/api/traces", params={**EPOCH_WINDOW, **params})
        assert r.status_code == 200
        return [t["trace_id"] for t in r.json()["traces"]]

    assert ids(service="payment-service") == [TRACE_ID.hex()]
    assert ids(service="nope") == []
    assert ids(errors_only="true") == [TRACE_ID.hex()]
    assert ids(min_duration_ms=3) == [TRACE_ID.hex()]
    assert ids(min_duration_ms=3.5) == []


def test_list_traces_default_window_uses_now(client):
    now = time.time_ns()
    ingest(client, {"gateway": [otlp_span(start=now - 50 * MS, end=now)]})
    traces = wait_for_traces(client, 1, params={})  # no from/to: last hour
    assert traces[0]["duration_ms"] == 50.0


def test_list_traces_limit_validation(client, order_trace):
    assert client.get("/api/traces", params={**EPOCH_WINDOW, "limit": 100_000}).status_code == 200  # capped
    assert client.get("/api/traces", params={"limit": 0}).status_code == 422
    assert client.get("/api/traces", params={"min_duration_ms": -1}).status_code == 422


# --- GET /api/traces/{trace_id} ----------------------------------------------

def test_get_trace_contract_shape(client, order_trace):
    r = client.get(f"/api/traces/{TRACE_ID.hex()}")
    assert r.status_code == 200
    body = r.json()
    assert (body["trace_id"], body["start_ms"], body["duration_ms"]) == (TRACE_ID.hex(), 1.0, 3.0)
    assert [s["name"] for s in body["spans"]] == ["POST /order", "chargeCard"]
    child = body["spans"][1]
    assert child["parent_span_id"] == ROOT_ID.hex()
    assert child["status_msg"] == "card declined"
    assert child["attributes"] == {"payment.provider": "stripe"}
    assert set(child) == {
        "span_id", "parent_span_id", "service", "name", "kind", "start_ms", "end_ms",
        "duration_ms", "status_code", "status_msg", "http_status", "route", "attributes",
    }


def test_get_trace_id_is_case_insensitive(client, order_trace):
    assert client.get(f"/api/traces/{TRACE_ID.hex().upper()}").status_code == 200


def test_get_unknown_trace_returns_404(client):
    r = client.get(f"/api/traces/{'f' * 32}")
    assert r.status_code == 404
    assert r.json() == {"detail": "trace not found"}


# --- GET /api/services --------------------------------------------------------

def test_services(client, order_trace):
    r = client.get("/api/services", params=EPOCH_WINDOW)
    assert r.json() == {"services": ["gateway", "payment-service"]}
