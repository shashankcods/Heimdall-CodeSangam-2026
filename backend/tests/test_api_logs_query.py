import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from heimdall.api.main import app
from heimdall.ingest.models import LogRecord
from tests.test_decoder import TRACE_ID
from tests.test_decoder_logs import logs_request_bytes, otlp_log

PROTOBUF = {"Content-Type": "application/x-protobuf"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api_logs.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


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


# ---------------------------------------------------------------------------
# GET /api/logs tests
# ---------------------------------------------------------------------------

def test_get_logs_empty(client):
    r = client.get("/api/logs")
    assert r.status_code == 200
    assert r.json() == {"logs": []}


def test_get_logs_returns_ingested_logs(client):
    body = logs_request_bytes({"order-service": [
        otlp_log("order created", severity=9, time=1_000_000_000),
        otlp_log("payment failed", severity=17, time=2_000_000_000),
    ]})
    client.post("/v1/logs", content=body, headers=PROTOBUF)

    logs = wait_for_logs(client, 2)
    assert len(logs) == 2
    assert logs[0]["body"] == "payment failed"
    assert logs[0]["severity"] == "ERROR"
    assert logs[0]["service"] == "order-service"
    assert logs[0]["trace_id"] == TRACE_ID.hex()
    assert logs[0]["timestamp_ms"] == 2000.0
    assert logs[0]["timestamp"] == 2000.0

    assert logs[1]["body"] == "order created"
    assert logs[1]["severity"] == "INFO"


def test_get_logs_filters_pass_through(client):
    body = logs_request_bytes({
        "gateway": [otlp_log("route matched", severity=9, time=1_000_000_000)],
        "payment": [otlp_log("chargeCard declined 502", severity=17, time=2_000_000_000)],
    })
    client.post("/v1/logs", content=body, headers=PROTOBUF)
    wait_for_logs(client, 2)

    # service filter
    r = client.get("/api/logs", params={"service": "payment"})
    assert len(r.json()["logs"]) == 1
    assert r.json()["logs"][0]["service"] == "payment"

    # query filter
    r = client.get("/api/logs", params={"query": "declined"})
    assert len(r.json()["logs"]) == 1
    assert r.json()["logs"][0]["service"] == "payment"

    # regex filter
    r = client.get("/api/logs", params={"regex": r"declined\s+\d+"})
    assert len(r.json()["logs"]) == 1
    assert r.json()["logs"][0]["body"] == "chargeCard declined 502"

    # severity filter
    r = client.get("/api/logs", params={"severity": "error"})
    assert len(r.json()["logs"]) == 1
    assert r.json()["logs"][0]["severity"] == "ERROR"

    # trace_id filter
    r = client.get("/api/logs", params={"trace_id": TRACE_ID.hex()})
    assert len(r.json()["logs"]) == 2

    # non-matching filter
    r = client.get("/api/logs", params={"service": "unknown"})
    assert len(r.json()["logs"]) == 0


def test_get_logs_invalid_regex_returns_422(client):
    r = client.get("/api/logs", params={"regex": "[unclosed"})
    assert r.status_code == 422
    assert "Invalid regular expression" in r.json()["detail"]


def test_get_logs_invalid_time_range_returns_422(client):
    r = client.get("/api/logs", params={"from_ms": 5000, "to_ms": 1000})
    assert r.status_code == 422
    assert "from_ms cannot be greater than to_ms" in r.json()["detail"]


def test_get_logs_limit_validation_and_capping(client):
    body = logs_request_bytes({"gateway": [
        otlp_log(f"log {i}", time=1_000_000_000 + i * 1_000_000) for i in range(5)
    ]})
    client.post("/v1/logs", content=body, headers=PROTOBUF)
    wait_for_logs(client, 5)

    # limit=0 returns 422
    assert client.get("/api/logs", params={"limit": 0}).status_code == 422
    # limit=2 returns 2 rows
    r = client.get("/api/logs", params={"limit": 2})
    assert len(r.json()["logs"]) == 2
    # limit=100000 is capped and returns 5 rows
    r = client.get("/api/logs", params={"limit": 100000})
    assert len(r.json()["logs"]) == 5


# ---------------------------------------------------------------------------
# GET /api/logs/stream SSE tests
# ---------------------------------------------------------------------------

def test_stream_logs_receives_new_log_events(client):
    # Connect to stream in a separate thread/streaming context
    with client.stream("GET", "/api/logs/stream") as stream:
        assert stream.status_code == 200
        assert "text/event-stream" in stream.headers["content-type"]

        # Ingest a log while stream is open
        body = logs_request_bytes({"order-service": [otlp_log("live stream test", severity=9)]})
        client.post("/v1/logs", content=body, headers=PROTOBUF)

        lines = []
        for line in stream.iter_lines():
            if line.startswith(":"):  # ignore ping comments
                continue
            lines.append(line)
            if len(lines) >= 2:  # event: log and data: {...}
                break

        assert "event: log" in lines[0]
        assert "data: " in lines[1]
        payload = json.loads(lines[1].removeprefix("data: "))
        assert payload["body"] == "live stream test"
        assert payload["service"] == "order-service"
        assert payload["severity"] == "INFO"


def test_stream_logs_subscriber_cleanup_on_disconnect(client):
    initial_subs = len(app.state.log_writer._subscribers)
    assert initial_subs == 0

    with client.stream("GET", "/api/logs/stream") as stream:
        assert len(app.state.log_writer._subscribers) == 1

    # After exiting with block, subscriber should be unsubscribed
    # Allow generator finally block to turn
    deadline = time.monotonic() + 1.0
    while len(app.state.log_writer._subscribers) > 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(app.state.log_writer._subscribers) == 0


def test_stream_logs_multiple_subscribers(client):
    sub1 = app.state.log_writer.subscribe(maxsize=10)
    sub2 = app.state.log_writer.subscribe(maxsize=10)

    try:
        body = logs_request_bytes({"order-service": [otlp_log("multi-sub test")]})
        client.post("/v1/logs", content=body, headers=PROTOBUF)

        # Wait for flush
        time.sleep(0.1)

        item1 = sub1.queue.get_nowait()
        item2 = sub2.queue.get_nowait()
        assert item1 is not None and item1["body"] == "multi-sub test"
        assert item2 is not None and item2["body"] == "multi-sub test"
    finally:
        app.state.log_writer.unsubscribe(sub1)
        app.state.log_writer.unsubscribe(sub2)


# ---------------------------------------------------------------------------
# CORS tests
# ---------------------------------------------------------------------------

def test_cors_preflight_and_headers(client):
    # Preflight OPTIONS request
    r = client.options(
        "/api/logs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "GET" in r.headers.get("access-control-allow-methods", "")

    # GET request with origin header
    r = client.get("/api/logs", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
