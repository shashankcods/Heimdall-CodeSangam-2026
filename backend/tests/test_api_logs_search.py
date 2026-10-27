import time

import pytest
from fastapi.testclient import TestClient

from heimdall.api.main import app
from tests.test_decoder import ROOT_ID, TRACE_ID, kv
from tests.test_decoder_logs import logs_request_bytes, otlp_log

PROTOBUF = {"Content-Type": "application/x-protobuf"}
MS = 1_000_000
EPOCH_WINDOW = {"from_ms": 0, "to_ms": 10_000}  # test log times are near 1970


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "api.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
    with TestClient(app) as c:
        yield c


def search(client, **params):
    r = client.get("/api/logs", params={**EPOCH_WINDOW, **params})
    assert r.status_code == 200, r.text
    return r.json()["logs"]


@pytest.fixture
def payment_logs(client):
    body = logs_request_bytes({
        "payment-service": [
            otlp_log("calling Stripe, amount=499", severity=9, time=1 * MS,
                     attrs=[kv("payment.provider", "stripe"), kv("retry", 0)]),
            otlp_log("Stripe timeout after 1.9s", severity=17, time=2 * MS,
                     attrs=[kv("payment.provider", "stripe"), kv("retry", 1)]),
        ],
        "gateway": [otlp_log("server listening", severity=9, time=500_000, trace_id=b"", span_id=b"")],
    })
    assert client.post("/v1/logs", content=body, headers=PROTOBUF).status_code == 200
    deadline = time.monotonic() + 2
    while len(search(client)) < 3:
        assert time.monotonic() < deadline, "logs never arrived"
        time.sleep(0.02)


def test_logs_contract_shape(client, payment_logs):
    newest = search(client)[0]
    assert newest == {
        "timestamp_ms": 2.0,
        "service": "payment-service",
        "severity": "ERROR",
        "body": "Stripe timeout after 1.9s",
        "trace_id": TRACE_ID.hex(),
        "span_id": ROOT_ID.hex(),
        "attributes": {"payment.provider": "stripe", "retry": 1},
    }


def test_filters_through_http(client, payment_logs):
    assert len(search(client, q="stripe")) == 2
    assert len(search(client, q=r"timeout after \d", regex="true")) == 1
    assert len(search(client, service="gateway")) == 1
    assert len(search(client, severity="WARN")) == 1
    assert len(search(client, trace_id=TRACE_ID.hex())) == 2


def test_repeated_tag_params_all_must_match(client, payment_logs):
    r = client.get("/api/logs", params=[*EPOCH_WINDOW.items(),
                                        ("tag", "payment.provider:stripe"), ("tag", "retry:1")])
    assert [l["body"] for l in r.json()["logs"]] == ["Stripe timeout after 1.9s"]


def test_tag_value_may_contain_colons(client):
    body = logs_request_bytes({"svc": [otlp_log(attrs=[kv("url", "http://pay:8003/charge")])]})
    client.post("/v1/logs", content=body, headers=PROTOBUF)
    deadline = time.monotonic() + 2
    while not search(client, tag="url:http://pay:8003/charge"):
        assert time.monotonic() < deadline, "tag with colons never matched"
        time.sleep(0.02)


@pytest.mark.parametrize("params, message", [
    ({"q": "missing (", "regex": "true"}, "invalid regex"),
    ({"severity": "LOUD"}, "unknown severity"),
    ({"tag": "no-colon-here"}, "key:value"),
    ({"tag": ":value-without-key"}, "key:value"),
])
def test_bad_search_params_return_400(client, params, message):
    r = client.get("/api/logs", params=params)
    assert r.status_code == 400
    assert message in r.json()["detail"]


def test_limit_validation(client, payment_logs):
    assert len(search(client, limit=1)) == 1
    assert client.get("/api/logs", params={"limit": 100_000}).status_code == 200  # capped
    assert client.get("/api/logs", params={"limit": 0}).status_code == 422


def test_default_window_uses_now(client):
    body = logs_request_bytes({"svc": [otlp_log("fresh log", time=time.time_ns())]})
    client.post("/v1/logs", content=body, headers=PROTOBUF)
    deadline = time.monotonic() + 2
    while not client.get("/api/logs").json()["logs"]:  # no from/to: last hour
        assert time.monotonic() < deadline, "recent log not returned by default window"
        time.sleep(0.02)
