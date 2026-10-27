import pytest

from heimdall.ingest.models import Span
from heimdall.queries.topology import service_edges, service_map
from heimdall.store.schema import connect

MS = 1_000_000  # nanoseconds per millisecond
WINDOW = dict(from_ms=0, to_ms=10_000)  # explicit range near epoch for tests


def make_span(
    trace_id: str,
    span_id: str,
    parent_span_id: str | None = None,
    service: str = "gateway",
    name: str = "op",
    start_ms: float = 1000.0,
    end_ms: float = 2000.0,
    status_code: str = "OK",
    http_status: int | None = None,
    attributes: dict | None = None,
) -> Span:
    start_ns = int(start_ms * MS)
    end_ns = int(end_ms * MS)
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        service=service,
        name=name,
        kind="SERVER",
        start_time=start_ns,
        end_time=end_ns,
        duration_ns=end_ns - start_ns,
        status_code=status_code,
        http_status=http_status,
        attributes=attributes or {},
    )


def insert_spans(conn, spans: list[Span]):
    for s in spans:
        row = s.to_row()
        conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def cur(tmp_path):
    return connect(str(tmp_path / "test_topology.duckdb")).cursor()


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------

def test_service_map_empty_database(cur):
    result = service_map(cur, **WINDOW)
    assert result == {"nodes": [], "edges": []}


def test_service_map_single_service_no_cross_calls(cur):
    insert_spans(cur, [
        make_span("t1", "s1", None, service="isolated-service", start_ms=1000, end_ms=2000),
        make_span("t1", "s2", "s1", service="isolated-service", start_ms=1100, end_ms=1500),
    ])
    result = service_map(cur, **WINDOW)
    assert result["nodes"] == [{"id": "isolated-service", "service": "isolated-service"}]
    assert result["edges"] == []


def test_service_map_one_cross_service_call(cur):
    insert_spans(cur, [
        make_span("t1", "s1", None, service="gateway", start_ms=1000, end_ms=3000),
        make_span("t1", "s2", "s1", service="order-service", start_ms=1100, end_ms=2500),
    ])
    result = service_map(cur, **WINDOW)
    assert result["nodes"] == [
        {"id": "gateway", "service": "gateway"},
        {"id": "order-service", "service": "order-service"},
    ]
    assert len(result["edges"]) == 1
    edge = result["edges"][0]
    assert edge == {
        "source": "gateway",
        "target": "order-service",
        "call_count": 1,
        "avg_latency_ms": 1400.0,
        "error_count": 0,
        "error_rate": 0.0,
    }


def test_service_map_three_service_chain(cur):
    # gateway -> order-service -> payment-service
    insert_spans(cur, [
        make_span("t1", "s1", None, service="gateway", start_ms=1000, end_ms=4000),
        make_span("t1", "s2", "s1", service="order-service", start_ms=1100, end_ms=3800),
        make_span("t1", "s3", "s2", service="payment-service", start_ms=1500, end_ms=2500),
    ])
    result = service_map(cur, **WINDOW)
    assert [n["id"] for n in result["nodes"]] == ["gateway", "order-service", "payment-service"]
    assert len(result["edges"]) == 2
    assert (result["edges"][0]["source"], result["edges"][0]["target"]) == ("gateway", "order-service")
    assert (result["edges"][1]["source"], result["edges"][1]["target"]) == ("order-service", "payment-service")


def test_service_map_multiple_calls_same_service_pair(cur):
    # 2 calls from gateway to order-service: 100ms and 300ms, one error
    insert_spans(cur, [
        make_span("t1", "g1", None, service="gateway", start_ms=1000, end_ms=2000),
        make_span("t1", "o1", "g1", service="order-service", start_ms=1100, end_ms=1200, status_code="OK"),
        make_span("t2", "g2", None, service="gateway", start_ms=3000, end_ms=4000),
        make_span("t2", "o2", "g2", service="order-service", start_ms=3100, end_ms=3400, status_code="ERROR"),
    ])
    edges = service_edges(cur, **WINDOW)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["source"] == "gateway"
    assert edge["target"] == "order-service"
    assert edge["call_count"] == 2
    assert edge["avg_latency_ms"] == 200.0  # (100 + 300) / 2
    assert edge["error_count"] == 1
    assert edge["error_rate"] == 0.5


def test_service_map_bidirectional_calls(cur):
    # Service A calls Service B in t1; Service B calls Service A in t2
    insert_spans(cur, [
        make_span("t1", "a1", None, service="service-a", start_ms=1000, end_ms=2000),
        make_span("t1", "b1", "a1", service="service-b", start_ms=1100, end_ms=1500),
        make_span("t2", "b2", None, service="service-b", start_ms=3000, end_ms=4000),
        make_span("t2", "a2", "b2", service="service-a", start_ms=3100, end_ms=3600),
    ])
    edges = service_edges(cur, **WINDOW)
    assert len(edges) == 2
    assert (edges[0]["source"], edges[0]["target"]) == ("service-a", "service-b")
    assert (edges[1]["source"], edges[1]["target"]) == ("service-b", "service-a")


def test_service_map_excludes_same_service_nested_spans(cur):
    # order-service -> order-service internal spans must not produce an edge
    insert_spans(cur, [
        make_span("t1", "o1", None, service="order-service", start_ms=1000, end_ms=3000),
        make_span("t1", "o2", "o1", service="order-service", name="validate", start_ms=1100, end_ms=1500),
        make_span("t1", "o3", "o2", service="order-service", name="db_insert", start_ms=1200, end_ms=1400),
    ])
    result = service_map(cur, **WINDOW)
    assert result["nodes"] == [{"id": "order-service", "service": "order-service"}]
    assert result["edges"] == []


def test_service_map_does_not_join_across_traces_with_overlapping_span_ids(cur):
    # Trace 1: A (span_id 'x') -> B (span_id 'y', parent 'x')
    # Trace 2: C (span_id 'x') -> D (span_id 'y', parent 'x')
    insert_spans(cur, [
        make_span("trace_1", "span_x", None, service="svc-a", start_ms=1000, end_ms=2000),
        make_span("trace_1", "span_y", "span_x", service="svc-b", start_ms=1100, end_ms=1500),
        make_span("trace_2", "span_x", None, service="svc-c", start_ms=3000, end_ms=4000),
        make_span("trace_2", "span_y", "span_x", service="svc-d", start_ms=3100, end_ms=3500),
    ])
    edges = service_edges(cur, **WINDOW)
    # Must only produce svc-a -> svc-b and svc-c -> svc-d
    # Must NEVER produce svc-a -> svc-d or svc-c -> svc-b
    assert len(edges) == 2
    pairs = {(e["source"], e["target"]) for e in edges}
    assert pairs == {("svc-a", "svc-b"), ("svc-c", "svc-d")}


def test_service_map_excludes_orphan_spans_missing_parent(cur):
    # Child references parent 'missing_parent' which does not exist
    insert_spans(cur, [
        make_span("t1", "s2", "missing_parent", service="order-service", start_ms=1000, end_ms=2000),
    ])
    result = service_map(cur, **WINDOW)
    assert result["nodes"] == [{"id": "order-service", "service": "order-service"}]
    assert result["edges"] == []


def test_service_map_deduplicates_retry_spans(cur):
    # Retry sends child span s2 twice (end_time 2000 and 2100)
    spans = [
        make_span("t1", "s1", None, service="gateway", start_ms=1000, end_ms=3000),
        make_span("t1", "s2", "s1", service="order-service", start_ms=1100, end_ms=2000, status_code="ERROR"),
        make_span("t1", "s2", "s1", service="order-service", start_ms=1100, end_ms=2100, status_code="ERROR"),
    ]
    insert_spans(cur, spans)
    edges = service_edges(cur, **WINDOW)
    assert len(edges) == 1
    # call_count must be 1, not 2
    assert edges[0]["call_count"] == 1
    assert edges[0]["error_count"] == 1
    assert edges[0]["error_rate"] == 1.0
    assert edges[0]["avg_latency_ms"] == 1000.0  # from the latest end_time 2100 (2100 - 1100 = 1000)


def test_service_map_error_classification_rules(cur):
    # Test error sources:
    # 1. status_code = 'ERROR', http_status = None -> error
    # 2. status_code = 'OK', http_status = 500 -> error
    # 3. status_code = 'ERROR', http_status = 502 -> error (counted once, not twice)
    # 4. status_code = 'OK', http_status = 200 -> ok
    insert_spans(cur, [
        make_span("t1", "g1", None, service="gw", start_ms=1000, end_ms=2000),
        make_span("t1", "c1", "g1", service="svc", start_ms=1100, end_ms=1200, status_code="ERROR", http_status=None),

        make_span("t2", "g2", None, service="gw", start_ms=2000, end_ms=3000),
        make_span("t2", "c2", "g2", service="svc", start_ms=2100, end_ms=2200, status_code="OK", http_status=500),

        make_span("t3", "g3", None, service="gw", start_ms=3000, end_ms=4000),
        make_span("t3", "c3", "g3", service="svc", start_ms=3100, end_ms=3200, status_code="ERROR", http_status=502),

        make_span("t4", "g4", None, service="gw", start_ms=4000, end_ms=5000),
        make_span("t4", "c4", "g4", service="svc", start_ms=4100, end_ms=4200, status_code="OK", http_status=200),
    ])
    edges = service_edges(cur, **WINDOW)
    assert len(edges) == 1
    edge = edges[0]
    assert edge["call_count"] == 4
    assert edge["error_count"] == 3
    assert edge["error_rate"] == 0.75


def test_service_map_time_window_filtering_inclusive(cur):
    # Spans at t=999 (before), t=1000 (bound), t=2000 (inside), t=3000 (bound), t=3001 (after)
    insert_spans(cur, [
        make_span("t0", "g0", None, service="gw", start_ms=900, end_ms=1000),
        make_span("t0", "c0", "g0", service="svc", start_ms=999, end_ms=1000),

        make_span("t1", "g1", None, service="gw", start_ms=950, end_ms=1100),
        make_span("t1", "c1", "g1", service="svc", start_ms=1000, end_ms=1050),

        make_span("t2", "g2", None, service="gw", start_ms=1900, end_ms=2100),
        make_span("t2", "c2", "g2", service="svc", start_ms=2000, end_ms=2050),

        make_span("t3", "g3", None, service="gw", start_ms=2900, end_ms=3100),
        make_span("t3", "c3", "g3", service="svc", start_ms=3000, end_ms=3050),

        make_span("t4", "g4", None, service="gw", start_ms=3000, end_ms=3200),
        make_span("t4", "c4", "g4", service="svc", start_ms=3001, end_ms=3050),
    ])
    edges = service_edges(cur, from_ms=1000.0, to_ms=3000.0)
    assert len(edges) == 1
    assert edges[0]["call_count"] == 3  # c1, c2, c3


def test_service_map_parent_outside_window_child_inside_window(cur):
    # Parent span started at 800ms (outside window [1000, 2000]), child started at 1100ms (inside window)
    insert_spans(cur, [
        make_span("t1", "p1", None, service="caller-svc", start_ms=800, end_ms=2500),
        make_span("t1", "c1", "p1", service="callee-svc", start_ms=1100, end_ms=1500),
    ])
    result = service_map(cur, from_ms=1000.0, to_ms=2000.0)
    # Both services must be in nodes
    assert [n["id"] for n in result["nodes"]] == ["callee-svc", "caller-svc"]
    # Edge must be found
    assert len(result["edges"]) == 1
    assert (result["edges"][0]["source"], result["edges"][0]["target"]) == ("caller-svc", "callee-svc")


def test_service_map_invalid_time_range_raises_value_error(cur):
    with pytest.raises(ValueError) as exc:
        service_map(cur, from_ms=5000.0, to_ms=2000.0)
    assert "from_ms cannot be greater than to_ms" in str(exc.value)


def test_service_map_stable_ordering(cur):
    # Insert multiple services and calls
    insert_spans(cur, [
        make_span("t1", "s1", None, service="zebra", start_ms=1000, end_ms=2000),
        make_span("t1", "s2", "s1", service="alpha", start_ms=1100, end_ms=1200),
        make_span("t2", "s3", None, service="alpha", start_ms=2000, end_ms=3000),
        make_span("t2", "s4", "s3", service="beta", start_ms=2100, end_ms=2200),
    ])
    result = service_map(cur, **WINDOW)
    # Nodes ordered alphabetically: alpha, beta, zebra
    assert [n["id"] for n in result["nodes"]] == ["alpha", "beta", "zebra"]
    # Edges ordered by source, target: alpha -> beta, zebra -> alpha
    assert [(e["source"], e["target"]) for e in result["edges"]] == [
        ("alpha", "beta"),
        ("zebra", "alpha"),
    ]
