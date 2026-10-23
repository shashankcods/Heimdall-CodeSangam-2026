import pytest

from heimdall.ingest.models import Span
from heimdall.queries.traces import get_trace, list_services, list_traces
from heimdall.store.schema import connect

MS = 1_000_000  # nanoseconds per millisecond
T1 = "a" * 32
T2 = "b" * 32
T3 = "c" * 32
WINDOW = dict(from_ms=0, to_ms=10_000)  # explicit range: test times are near the epoch


def span(trace_id, span_id, parent=None, service="gateway", name="op",
         start_ms=0, end_ms=10, status="OK", http_status=None, attributes=None):
    return Span(
        trace_id=trace_id, span_id=span_id, parent_span_id=parent,
        service=service, name=name, kind="SERVER",
        start_time=start_ms * MS, end_time=end_ms * MS, duration_ns=(end_ms - start_ms) * MS,
        status_code=status, http_status=http_status, attributes=attributes or {},
    )


def insert(conn, spans):
    for s in spans:
        row = s.to_row()
        conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def cur(tmp_path):
    return connect(str(tmp_path / "test.duckdb")).cursor()


@pytest.fixture
def order_trace():
    """The DESIGN.md example: gateway -> order -> (db insert, payment). Payment fails."""
    return [
        span(T1, "s1", None, "gateway", "POST /order", 1000, 3400, "ERROR", 500),
        span(T1, "s2", "s1", "order-service", "createOrder", 1025, 3375, "ERROR", 500),
        span(T1, "s3", "s2", "order-service", "INSERT orders", 1040, 1140),
        span(T1, "s4", "s2", "payment-service", "chargeCard", 1450, 3350, "ERROR", 504,
             attributes={"payment.provider": "stripe"}),
    ]


# --- list_traces -----------------------------------------------------------

def test_list_traces_summarizes_one_row_per_trace(cur, order_trace):
    insert(cur, order_trace)
    [t] = list_traces(cur, **WINDOW)
    assert t == {
        "trace_id": T1,
        "root_service": "gateway",
        "root_name": "POST /order",
        "start_ms": 1000.0,
        "duration_ms": 2400.0,
        "span_count": 4,
        "service_count": 3,
        "error_count": 3,
        "has_error": True,
        "http_status": 500,
    }


def test_list_traces_newest_first_and_limit(cur):
    insert(cur, [span(T1, "s1", start_ms=100), span(T2, "s1", start_ms=300), span(T3, "s1", start_ms=200)])
    assert [t["trace_id"] for t in list_traces(cur, **WINDOW)] == [T2, T3, T1]
    assert [t["trace_id"] for t in list_traces(cur, limit=2, **WINDOW)] == [T2, T3]


def test_duplicate_spans_are_counted_once(cur, order_trace):
    insert(cur, order_trace + order_trace[2:])  # SDK retry resent s3 and s4
    [t] = list_traces(cur, **WINDOW)
    assert t["span_count"] == 4
    assert t["error_count"] == 3


def test_missing_root_falls_back_to_earliest_span(cur):
    insert(cur, [  # parent "s1" never arrived
        span(T1, "s3", "s1", "order-service", "INSERT orders", 1040, 1140),
        span(T1, "s2", "s1", "order-service", "createOrder", 1025, 3375, http_status=201),
    ])
    [t] = list_traces(cur, **WINDOW)
    assert (t["root_service"], t["root_name"], t["http_status"]) == ("order-service", "createOrder", 201)


def test_filter_by_service_matches_any_span_in_trace(cur, order_trace):
    insert(cur, order_trace + [span(T2, "x1", None, "inventory", start_ms=5000)])
    assert [t["trace_id"] for t in list_traces(cur, service="payment-service", **WINDOW)] == [T1]
    assert list_traces(cur, service="nope", **WINDOW) == []


def test_filter_errors_only(cur, order_trace):
    insert(cur, order_trace + [span(T2, "x1", start_ms=5000)])
    assert [t["trace_id"] for t in list_traces(cur, errors_only=True, **WINDOW)] == [T1]


def test_filter_min_duration(cur, order_trace):
    insert(cur, order_trace + [span(T2, "x1", start_ms=5000, end_ms=5010)])  # 10 ms trace
    assert [t["trace_id"] for t in list_traces(cur, min_duration_ms=1000, **WINDOW)] == [T1]
    assert len(list_traces(cur, min_duration_ms=10, **WINDOW)) == 2  # inclusive


def test_time_range_matches_on_trace_start_only(cur):
    insert(cur, [
        span(T1, "s1", None, start_ms=900, end_ms=1500),   # trace starts BEFORE the window
        span(T1, "s2", "s1", start_ms=1100, end_ms=1200),  # but has a child inside it
        span(T2, "s1", None, start_ms=1200, end_ms=1300),
    ])
    traces = list_traces(cur, from_ms=1000, to_ms=2000)
    assert [t["trace_id"] for t in traces] == [T2]  # T1 not shown, not even partially


def test_default_window_is_last_hour(cur):
    insert(cur, [span(T1, "s1")])  # 1970: far outside the last hour
    assert list_traces(cur) == []


# --- get_trace -------------------------------------------------------------

def test_get_trace_returns_flat_spans_sorted_by_start(cur, order_trace):
    insert(cur, list(reversed(order_trace)))
    trace = get_trace(cur, T1)
    assert [s["span_id"] for s in trace["spans"]] == ["s1", "s2", "s3", "s4"]
    assert (trace["trace_id"], trace["start_ms"], trace["duration_ms"]) == (T1, 1000.0, 2400.0)


def test_get_trace_span_shape(cur, order_trace):
    insert(cur, order_trace)
    s4 = get_trace(cur, T1)["spans"][3]
    assert s4 == {
        "span_id": "s4",
        "parent_span_id": "s2",
        "service": "payment-service",
        "name": "chargeCard",
        "kind": "SERVER",
        "start_ms": 1450.0,
        "end_ms": 3350.0,
        "duration_ms": 1900.0,
        "status_code": "ERROR",
        "status_msg": None,
        "http_status": 504,
        "route": None,
        "attributes": {"payment.provider": "stripe"},  # a real dict, not a JSON string
    }


def test_get_trace_removes_duplicates(cur, order_trace):
    insert(cur, order_trace + order_trace)
    assert len(get_trace(cur, T1)["spans"]) == 4


def test_get_trace_keeps_orphan_spans(cur):
    insert(cur, [span(T1, "s3", parent="missing")])
    assert get_trace(cur, T1)["spans"][0]["parent_span_id"] == "missing"


def test_get_trace_unknown_id_returns_none(cur):
    assert get_trace(cur, "f" * 32) is None


# --- list_services ---------------------------------------------------------

def test_list_services_distinct_sorted_in_range(cur, order_trace):
    insert(cur, order_trace + [span(T2, "x1", None, "late-service", start_ms=50_000)])
    assert list_services(cur, **WINDOW) == ["gateway", "order-service", "payment-service"]
