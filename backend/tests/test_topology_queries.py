import pytest

from heimdall.ingest.models import Span
from heimdall.queries.topology import service_map
from heimdall.store.schema import connect

MS = 1_000_000
WINDOW = dict(from_ms=0, to_ms=10_000)  # explicit range: test times are near the epoch


def span(trace_id, span_id, parent, service, name, start_ms, dur_ms, kind="SERVER", status="OK"):
    return Span(
        trace_id=trace_id, span_id=span_id, parent_span_id=parent, service=service, name=name,
        kind=kind, start_time=start_ms * MS, end_time=(start_ms + dur_ms) * MS,
        duration_ns=dur_ms * MS, status_code=status,
    )


def order_trace(trace_id, start_ms=1000, payment_ok=True, payment_ms=20):
    """gateway -> order-service -> payment-service, plus same-service and SDK-internal spans."""
    t, ok = trace_id, payment_ok
    return [
        span(t, "g1", None, "gateway", "POST /order", start_ms, 100 + payment_ms, status="OK" if ok else "ERROR"),
        span(t, "g2", "g1", "gateway", "POST /order http send", start_ms + 99, 0, kind="INTERNAL"),
        span(t, "o1", "g1", "order-service", "POST /orders", start_ms + 10, 80 + payment_ms,
             status="OK" if ok else "ERROR"),
        span(t, "o2", "o1", "order-service", "INSERT orders", start_ms + 12, 15, kind="CLIENT"),
        span(t, "p1", "o1", "payment-service", "POST /charge", start_ms + 30, payment_ms,
             status="OK" if ok else "ERROR"),
        span(t, "p2", "p1", "payment-service", "chargeCard", start_ms + 30, payment_ms, kind="CLIENT",
             status="OK" if ok else "ERROR"),
        span(t, "p3", "p1", "payment-service", "POST /charge http send", start_ms + 30 + payment_ms, 0,
             kind="INTERNAL", status="OK" if ok else "ERROR"),
    ]


def insert(conn, spans):
    for s in spans:
        row = s.to_row()
        conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def cur(tmp_path):
    return connect(str(tmp_path / "test.duckdb")).cursor()


def edges_by_pair(result):
    return {(e["source"], e["target"]): e for e in result["edges"]}


def nodes_by_id(result):
    return {n["id"]: n for n in result["nodes"]}


def test_edges_follow_cross_service_parent_links(cur):
    insert(cur, order_trace("a" * 32))
    assert set(edges_by_pair(service_map(cur, **WINDOW))) == {
        ("gateway", "order-service"),
        ("order-service", "payment-service"),
    }  # same-service children (INSERT orders, chargeCard, http send) are not edges


def test_edge_stats(cur):
    insert(cur, order_trace("a" * 32, payment_ms=20)
           + order_trace("b" * 32, start_ms=2000, payment_ms=40)
           + order_trace("c" * 32, start_ms=3000, payment_ok=False, payment_ms=1900))
    pay = edges_by_pair(service_map(cur, **WINDOW))[("order-service", "payment-service")]
    assert pay["call_count"] == 3
    assert pay["error_count"] == 1
    assert pay["error_rate"] == pytest.approx(1 / 3)
    assert pay["avg_duration_ms"] == pytest.approx((20 + 40 + 1900) / 3)
    assert 40 < pay["p95_duration_ms"] <= 1900


def test_nodes_count_entry_spans_only(cur):
    insert(cur, order_trace("a" * 32) + order_trace("b" * 32, start_ms=2000, payment_ok=False))
    payment = nodes_by_id(service_map(cur, **WINDOW))["payment-service"]
    # 2 requests handled (one POST /charge entry span each), not 6 spans
    assert payment["request_count"] == 2
    assert payment["error_count"] == 1
    assert payment["error_rate"] == 0.5


def test_node_shape(cur):
    insert(cur, order_trace("a" * 32, payment_ms=20))
    assert nodes_by_id(service_map(cur, **WINDOW))["payment-service"] == {
        "id": "payment-service",
        "request_count": 1,
        "error_count": 0,
        "error_rate": 0.0,
        "avg_duration_ms": 20.0,
        "p95_duration_ms": 20.0,
    }


def test_root_span_counts_as_entry_even_if_not_server(cur):
    insert(cur, [span("a" * 32, "j1", None, "cron-job", "nightly", 1000, 50, kind="INTERNAL")])
    assert nodes_by_id(service_map(cur, **WINDOW))["cron-job"]["request_count"] == 1


def test_isolated_service_is_a_node_without_edges(cur):
    insert(cur, order_trace("a" * 32) + [span("z" * 32, "i1", None, "inventory-service", "reserve", 1500, 5)])
    result = service_map(cur, **WINDOW)
    assert "inventory-service" in nodes_by_id(result)
    assert all("inventory-service" not in pair for pair in edges_by_pair(result))


def test_orphan_span_with_missing_parent_makes_no_edge(cur):
    insert(cur, [span("a" * 32, "p1", "never-arrived", "payment-service", "POST /charge", 1000, 20)])
    result = service_map(cur, **WINDOW)
    assert result["edges"] == []
    assert nodes_by_id(result)["payment-service"]["request_count"] == 1


def test_parent_started_before_window_still_counts(cur):
    insert(cur, [
        span("a" * 32, "g1", None, "gateway", "POST /order", 500, 1000),          # before window
        span("a" * 32, "o1", "g1", "order-service", "POST /orders", 1200, 100),   # inside window
    ])
    edges = edges_by_pair(service_map(cur, from_ms=1000, to_ms=2000))
    assert edges[("gateway", "order-service")]["call_count"] == 1


def test_window_uses_span_start_time(cur):
    insert(cur, order_trace("a" * 32, start_ms=1000) + order_trace("b" * 32, start_ms=5000))
    pay = edges_by_pair(service_map(cur, from_ms=4000, to_ms=6000))[("order-service", "payment-service")]
    assert pay["call_count"] == 1


def test_duplicate_spans_counted_once(cur):
    trace = order_trace("a" * 32)
    insert(cur, trace + trace)  # SDK retry resent everything
    result = service_map(cur, **WINDOW)
    assert edges_by_pair(result)[("gateway", "order-service")]["call_count"] == 1
    assert nodes_by_id(result)["gateway"]["request_count"] == 1


def test_empty_range_returns_empty_lists(cur):
    assert service_map(cur, **WINDOW) == {"nodes": [], "edges": []}
    insert(cur, order_trace("a" * 32))
    assert service_map(cur) == {"nodes": [], "edges": []}  # default last hour; data is from 1970
