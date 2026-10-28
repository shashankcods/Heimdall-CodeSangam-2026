import time

import pytest
from fastapi.testclient import TestClient

from heimdall.api.main import app
from heimdall.ingest.models import Span

MS = 1_000_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    with TestClient(app) as c:
        yield c


def insert_op(name, count, dur_ms, minutes_ago, errors=0, prefix="a"):
    now = time.time_ns()
    for i in range(count):
        start = now - (minutes_ago * 60_000 - i * 1000) * MS
        row = Span(trace_id=f"{prefix}{i:031x}", span_id=f"{prefix}{i:015x}", service="payment-service",
                   name=name, kind="SERVER", start_time=start, end_time=start + dur_ms * MS,
                   duration_ns=dur_ms * MS, status_code="ERROR" if i < errors else "OK").to_row()
        app.state.conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))


def test_anomalies_endpoint(client):
    insert_op("chargeCard", 30, 25, minutes_ago=30, prefix="b")
    insert_op("chargeCard", 20, 1900, minutes_ago=3, errors=20, prefix="r")
    body = client.get("/api/anomalies").json()
    assert set(body) == {"recent_window", "baseline_window", "anomalies"}
    [a] = body["anomalies"]
    assert a["name"] == "chargeCard" and a["error_spike"] is True
    assert set(a) == {
        "service", "name", "recent_p99_ms", "baseline_p99_ms", "p99_ratio", "recent_count",
        "baseline_count", "recent_error_rate", "baseline_error_rate", "error_spike", "example_trace_id",
    }


def test_anomalies_empty_when_nothing_changed(client):
    assert client.get("/api/anomalies").json()["anomalies"] == []


@pytest.mark.parametrize("params", [{"recent_minutes": 0}, {"min_ratio": 0.5}, {"limit": 0}, {"limit": 1000}])
def test_anomalies_param_validation(client, params):
    assert client.get("/api/anomalies", params=params).status_code == 422
