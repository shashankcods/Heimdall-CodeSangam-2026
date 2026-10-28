import gzip
import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from heimdall.api.main import app
from heimdall.ingest.pipeline import QueueFull
from tests.test_decoder import CHILD_ID, ROOT_ID, kv, otlp_span, request_bytes

PROTOBUF = {"Content-Type": "application/x-protobuf"}
EPOCH_WINDOW = {"from_ms": 0, "to_ms": 10_000}  # otlp_span() defaults to times near 1970


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:  # `with` runs the lifespan: DB + writer start/stop
        yield c


def wait_for_spans(client, expected, timeout=2.0):
    """All stored spans, read back through the trace API."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        traces = client.get("/api/traces", params=EPOCH_WINDOW).json()["traces"]
        spans = [s for t in traces for s in client.get(f"/api/traces/{t['trace_id']}").json()["spans"]]
        if len(spans) >= expected:
            return spans
        time.sleep(0.02)
    raise AssertionError(f"expected {expected} spans, got {len(spans)}")


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_ingest_returns_empty_protobuf_response(client):
    r = client.post("/v1/traces", content=request_bytes({"gateway": [otlp_span()]}), headers=PROTOBUF)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-protobuf"
    ExportTraceServiceResponse().ParseFromString(r.content)  # must be valid protobuf


def test_ingested_spans_can_be_read_back(client):
    body = request_bytes({
        "gateway": [otlp_span(attrs=[kv("http.response.status_code", 500)], start=2_000_000, end=5_000_000)],
        "order-service": [otlp_span(span_id=CHILD_ID, parent=ROOT_ID, name="createOrder")],
    })
    client.post("/v1/traces", content=body, headers=PROTOBUF)
    spans = {s["service"]: s for s in wait_for_spans(client, 2)}
    assert spans["gateway"]["http_status"] == 500
    assert spans["gateway"]["start_ms"] == 2.0  # ns -> ms
    assert spans["gateway"]["duration_ms"] == 3.0
    assert spans["order-service"]["parent_span_id"] == ROOT_ID.hex()


def test_gzip_body_accepted(client):
    body = gzip.compress(request_bytes({"gateway": [otlp_span()]}))
    r = client.post("/v1/traces", content=body, headers={**PROTOBUF, "Content-Encoding": "gzip"})
    assert r.status_code == 200
    wait_for_spans(client, 1)


def test_invalid_protobuf_returns_400(client):
    r = client.post("/v1/traces", content=b"not a protobuf", headers=PROTOBUF)
    assert r.status_code == 400


def test_json_content_type_returns_415(client):
    r = client.post("/v1/traces", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 415


def test_full_queue_returns_429_with_retry_after(client, monkeypatch):
    def always_full(records):
        raise QueueFull("full")

    monkeypatch.setattr(app.state.span_writer, "submit", always_full)
    r = client.post("/v1/traces", content=request_bytes({"gateway": [otlp_span()]}), headers=PROTOBUF)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "1"


def test_debug_spans_endpoint_is_gone(client):
    assert client.get("/api/spans").status_code == 404
