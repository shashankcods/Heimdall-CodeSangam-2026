import pytest

from heimdall.ingest.models import LogRecord
from heimdall.queries.logs import InvalidSearch, search_logs
from heimdall.store.schema import connect

MS = 1_000_000
T1 = "a" * 32
WINDOW = dict(from_ms=0, to_ms=10_000)  # explicit range: test times are near the epoch


def log(body, ms, service="payment-service", severity="INFO", trace_id=None, span_id=None, attributes=None):
    return LogRecord(timestamp=ms * MS, service=service, severity=severity, body=body,
                     trace_id=trace_id, span_id=span_id, attributes=attributes or {})


def insert(conn, logs):
    for record in logs:
        row = record.to_row()
        conn.execute(f"INSERT INTO logs VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def cur(tmp_path):
    c = connect(str(tmp_path / "test.duckdb")).cursor()
    insert(c, [
        log("server listening on :8001", 100, service="gateway"),
        log("calling Stripe, amount=499", 1000, trace_id=T1, span_id="s4",
            attributes={"payment.provider": "stripe", "retry": 0}),
        log("Stripe timeout after 1.9s", 2000, severity="ERROR", trace_id=T1, span_id="s4",
            attributes={"payment.provider": "stripe", "retry": 1}),
        log("retrying payment", 2100, severity="WARN", trace_id=T1, span_id="s4"),
        log("order created", 3000, service="order-service", severity="DEBUG"),
    ])
    return c


def bodies(logs):
    return [l["body"] for l in logs]


def test_newest_first_with_contract_shape(cur):
    logs = search_logs(cur, **WINDOW)
    assert bodies(logs)[0] == "order created"
    assert logs[2] == {
        "timestamp_ms": 2000.0,
        "service": "payment-service",
        "severity": "ERROR",
        "body": "Stripe timeout after 1.9s",
        "trace_id": T1,
        "span_id": "s4",
        "attributes": {"payment.provider": "stripe", "retry": 1},  # a real dict
    }


def test_limit(cur):
    assert bodies(search_logs(cur, limit=2, **WINDOW)) == ["order created", "retrying payment"]


def test_text_search_is_case_insensitive_substring(cur):
    assert bodies(search_logs(cur, q="STRIPE", **WINDOW)) == [
        "Stripe timeout after 1.9s", "calling Stripe, amount=499",
    ]


def test_text_search_treats_regex_characters_literally(cur):
    assert search_logs(cur, q="1.9s", **WINDOW)[0]["body"] == "Stripe timeout after 1.9s"
    assert search_logs(cur, q="amount=4.9", **WINDOW) == []  # "." is not a wildcard here


def test_regex_search(cur):
    assert bodies(search_logs(cur, q=r"timeout after \d+\.\ds", regex=True, **WINDOW)) == [
        "Stripe timeout after 1.9s",
    ]
    assert bodies(search_logs(cur, q="^(server|order)", regex=True, **WINDOW)) == [
        "order created", "server listening on :8001",
    ]


def test_invalid_regex_raises_invalid_search(cur):
    with pytest.raises(InvalidSearch, match="invalid regex"):
        search_logs(cur, q="missing (", regex=True, **WINDOW)


def test_filter_by_service(cur):
    assert bodies(search_logs(cur, service="gateway", **WINDOW)) == ["server listening on :8001"]


@pytest.mark.parametrize("level, expected_count", [
    ("DEBUG", 5), ("INFO", 4), ("warn", 2), ("ERROR", 1), ("FATAL", 0),
])
def test_severity_is_a_minimum_level(cur, level, expected_count):
    assert len(search_logs(cur, severity=level, **WINDOW)) == expected_count


def test_unknown_severity_raises(cur):
    with pytest.raises(InvalidSearch, match="unknown severity"):
        search_logs(cur, severity="LOUD", **WINDOW)


def test_filter_by_trace_id_case_insensitive(cur):
    assert len(search_logs(cur, trace_id=T1.upper(), **WINDOW)) == 3


def test_tag_filters_all_must_match(cur):
    assert len(search_logs(cur, tags=[("payment.provider", "stripe")], **WINDOW)) == 2
    assert bodies(search_logs(cur, tags=[("payment.provider", "stripe"), ("retry", "1")], **WINDOW)) == [
        "Stripe timeout after 1.9s",
    ]
    assert search_logs(cur, tags=[("payment.provider", "paypal")], **WINDOW) == []


def test_filters_combine(cur):
    logs = search_logs(cur, q="stripe", severity="ERROR", service="payment-service", trace_id=T1, **WINDOW)
    assert bodies(logs) == ["Stripe timeout after 1.9s"]


def test_time_range(cur):
    assert bodies(search_logs(cur, from_ms=1500, to_ms=2500)) == [
        "retrying payment", "Stripe timeout after 1.9s",
    ]


def test_default_window_is_last_hour(cur):
    assert search_logs(cur) == []  # fixture logs are from 1970
