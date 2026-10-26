import gzip
import time

import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse

from heimdall.api.main import app
from heimdall.ingest.pipeline import QueueFull
from tests.test_decoder import TRACE_ID, otlp_span, request_bytes
from tests.test_decoder_logs import logs_request_bytes, otlp_log

PROTOBUF = {"Content-Type": "application/x-protobuf"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


def stored_logs(client, expected, timeout=2.0):
    """Read the logs table directly (no /api/logs yet; that's issue #5)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = app.state.conn.cursor()
        rows = cur.execute(
            "SELECT service, severity, body, trace_id FROM logs ORDER BY timestamp"
        ).fetchall()
        cur.close()
        if len(rows) >= expected:
            return rows
        time.sleep(0.02)
    raise AssertionError(f"expected {expected} logs, got {len(rows)}")


def test_ingest_logs_returns_empty_protobuf_response(client):
    r = client.post("/v1/logs", content=logs_request_bytes({"gateway": [otlp_log()]}), headers=PROTOBUF)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-protobuf"
    ExportLogsServiceResponse().ParseFromString(r.content)


def test_ingested_logs_are_stored(client):
    body = logs_request_bytes({"payment-service": [
        otlp_log("calling Stripe", severity=9, time=1_000),
        otlp_log("Stripe timeout", severity=17, time=2_000),
        otlp_log("server listening", severity=9, time=500, trace_id=b"", span_id=b""),
    ]})
    client.post("/v1/logs", content=body, headers=PROTOBUF)
    assert stored_logs(client, 3) == [
        ("payment-service", "INFO", "server listening", None),
        ("payment-service", "INFO", "calling Stripe", TRACE_ID.hex()),
        ("payment-service", "ERROR", "Stripe timeout", TRACE_ID.hex()),
    ]


def test_gzip_logs_accepted(client):
    body = gzip.compress(logs_request_bytes({"gateway": [otlp_log()]}))
    r = client.post("/v1/logs", content=body, headers={**PROTOBUF, "Content-Encoding": "gzip"})
    assert r.status_code == 200
    stored_logs(client, 1)


def test_invalid_logs_payload_returns_400(client):
    assert client.post("/v1/logs", content=b"not a protobuf", headers=PROTOBUF).status_code == 400


def test_json_logs_return_415(client):
    r = client.post("/v1/logs", content=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 415


def test_full_log_queue_returns_429_but_traces_still_accepted(client, monkeypatch):
    def always_full(records):
        raise QueueFull("full")

    monkeypatch.setattr(app.state.log_writer, "submit", always_full)
    r = client.post("/v1/logs", content=logs_request_bytes({"gateway": [otlp_log()]}), headers=PROTOBUF)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "1"

    # Separate writers: a full log queue doesn't block trace ingest.
    r = client.post("/v1/traces", content=request_bytes({"gateway": [otlp_span()]}), headers=PROTOBUF)
    assert r.status_code == 200


def test_logs_and_spans_go_to_their_own_tables(client):
    client.post("/v1/logs", content=logs_request_bytes({"gateway": [otlp_log()]}), headers=PROTOBUF)
    client.post("/v1/traces", content=request_bytes({"gateway": [otlp_span()]}), headers=PROTOBUF)
    stored_logs(client, 1)
    cur = app.state.conn.cursor()
    deadline = time.monotonic() + 2
    while cur.execute("SELECT count(*) FROM spans").fetchone()[0] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert cur.execute("SELECT count(*) FROM logs").fetchone()[0] == 1
    assert cur.execute("SELECT count(*) FROM spans").fetchone()[0] == 1
    cur.close()
