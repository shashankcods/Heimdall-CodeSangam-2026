import json
import pytest

from heimdall.ingest.models import LogRecord
from heimdall.queries.logs import search_logs
from heimdall.store.schema import connect

MS = 1_000_000  # nanoseconds per millisecond


def log_row(
    timestamp_ms: float = 1000.0,
    service: str = "order-service",
    severity: str = "INFO",
    body: str = "processing order",
    trace_id: str | None = "a" * 32,
    span_id: str | None = "b" * 16,
    attributes: dict | None = None,
) -> LogRecord:
    return LogRecord(
        timestamp=int(timestamp_ms * MS),
        service=service,
        severity=severity,
        body=body,
        trace_id=trace_id,
        span_id=span_id,
        attributes=attributes or {},
    )


def insert_logs(conn, records: list[LogRecord]):
    for r in records:
        row = r.to_row()
        conn.execute(f"INSERT INTO logs VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def cur(tmp_path):
    return connect(str(tmp_path / "test.duckdb")).cursor()


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------

def test_search_logs_empty_table(cur):
    results = search_logs(cur)
    assert results == []


def test_search_logs_all_records_descending(cur):
    insert_logs(cur, [
        log_row(timestamp_ms=100.0, body="first"),
        log_row(timestamp_ms=300.0, body="third"),
        log_row(timestamp_ms=200.0, body="second"),
    ])
    results = search_logs(cur)
    assert [r["body"] for r in results] == ["third", "second", "first"]
    assert results[0]["timestamp_ms"] == 300.0
    assert results[0]["timestamp"] == 300.0


def test_search_logs_query_substring_case_insensitive(cur):
    insert_logs(cur, [
        log_row(body="Payment PROCESSED successfully"),
        log_row(body="Order created"),
        log_row(body="payment failed"),
    ])
    results = search_logs(cur, query="payment")
    assert len(results) == 2
    assert {r["body"] for r in results} == {
        "Payment PROCESSED successfully",
        "payment failed",
    }


def test_search_logs_query_special_characters_literal(cur):
    # Verify that % and _ are treated as literal characters and not SQL LIKE wildcards
    insert_logs(cur, [
        log_row(body="Discount of 20% applied"),
        log_row(body="Discount of 200 dollars applied"),
        log_row(body="user_name updated"),
        log_row(body="username updated"),
    ])
    assert len(search_logs(cur, query="20%")) == 1
    assert search_logs(cur, query="20%")[0]["body"] == "Discount of 20% applied"

    assert len(search_logs(cur, query="user_name")) == 1
    assert search_logs(cur, query="user_name")[0]["body"] == "user_name updated"


def test_search_logs_regex_matching(cur):
    insert_logs(cur, [
        log_row(body="Order #1234 completed"),
        log_row(body="Order #ABCD failed"),
        log_row(body="Healthcheck ping"),
    ])
    results = search_logs(cur, regex=r"Order #\d+")
    assert len(results) == 1
    assert results[0]["body"] == "Order #1234 completed"


def test_search_logs_invalid_regex_raises_value_error(cur):
    with pytest.raises(ValueError) as exc:
        search_logs(cur, regex=r"[unclosed")
    assert "Invalid regular expression" in str(exc.value)


def test_search_logs_service_filter(cur):
    insert_logs(cur, [
        log_row(service="gateway", body="g1"),
        log_row(service="order", body="o1"),
        log_row(service="payment", body="p1"),
    ])
    results = search_logs(cur, service="order")
    assert len(results) == 1
    assert results[0]["body"] == "o1"
    assert results[0]["service"] == "order"


def test_search_logs_severity_filter_case_insensitive(cur):
    insert_logs(cur, [
        log_row(severity="INFO", body="i1"),
        log_row(severity="WARN", body="w1"),
        log_row(severity="ERROR", body="e1"),
        log_row(severity="FATAL", body="f1"),
    ])
    assert len(search_logs(cur, severity="error")) == 1
    assert search_logs(cur, severity="error")[0]["body"] == "e1"
    assert len(search_logs(cur, severity="WARN")) == 1
    assert search_logs(cur, severity="WARN")[0]["body"] == "w1"


def test_search_logs_trace_id_filter_case_insensitive(cur):
    t1 = "4bf92f3577b34da6a3ce929d0e0e4736"
    t2 = "c0ffee" * 5 + "00"
    insert_logs(cur, [
        log_row(trace_id=t1, body="log for t1"),
        log_row(trace_id=t2, body="log for t2"),
        log_row(trace_id=None, body="log outside request"),
    ])
    # Search lowercase
    assert len(search_logs(cur, trace_id=t1)) == 1
    assert search_logs(cur, trace_id=t1)[0]["body"] == "log for t1"
    # Search uppercase
    assert len(search_logs(cur, trace_id=t1.upper())) == 1
    assert search_logs(cur, trace_id=t1.upper())[0]["body"] == "log for t1"


def test_search_logs_time_range_inclusive(cur):
    insert_logs(cur, [
        log_row(timestamp_ms=100.0, body="before"),
        log_row(timestamp_ms=200.0, body="start_bound"),
        log_row(timestamp_ms=300.0, body="inside"),
        log_row(timestamp_ms=400.0, body="end_bound"),
        log_row(timestamp_ms=500.0, body="after"),
    ])
    results = search_logs(cur, from_ms=200.0, to_ms=400.0)
    assert [r["body"] for r in results] == ["end_bound", "inside", "start_bound"]


def test_search_logs_invalid_time_range_raises_value_error(cur):
    with pytest.raises(ValueError) as exc:
        search_logs(cur, from_ms=500.0, to_ms=200.0)
    assert "from_ms cannot be greater than to_ms" in str(exc.value)


def test_search_logs_limit_enforced_and_capped(cur):
    for i in range(10):
        insert_logs(cur, [log_row(timestamp_ms=100.0 + i, body=f"log-{i}")])

    results = search_logs(cur, limit=3)
    assert len(results) == 3
    assert results[0]["body"] == "log-9"
    assert results[1]["body"] == "log-8"
    assert results[2]["body"] == "log-7"


def test_search_logs_invalid_limit_raises_value_error(cur):
    with pytest.raises(ValueError) as exc:
        search_logs(cur, limit=0)
    assert "limit must be greater than or equal to 1" in str(exc.value)


def test_search_logs_attributes_parsed_as_dict(cur):
    insert_logs(cur, [
        log_row(body="with attrs", attributes={"customer.id": 1234, "env": "prod"}),
        log_row(body="empty attrs", attributes={}),
    ])
    results = search_logs(cur)
    by_body = {r["body"]: r for r in results}
    assert by_body["with attrs"]["attributes"] == {"customer.id": 1234, "env": "prod"}
    assert by_body["empty attrs"]["attributes"] == {}


def test_search_logs_deterministic_secondary_ordering(cur):
    # Same timestamp: rowid DESC determines stable ordering
    insert_logs(cur, [
        log_row(timestamp_ms=1000.0, body="row1"),
        log_row(timestamp_ms=1000.0, body="row2"),
        log_row(timestamp_ms=1000.0, body="row3"),
    ])
    results = search_logs(cur)
    assert [r["body"] for r in results] == ["row3", "row2", "row1"]


def test_search_logs_empty_or_whitespace_filters_ignored(cur):
    insert_logs(cur, [log_row(body="test message")])
    results = search_logs(cur, query="", regex="", service="", severity="", trace_id="")
    assert len(results) == 1
    assert results[0]["body"] == "test message"
