"""Service map: which service calls which, derived from parent/child spans.

An EDGE source -> target exists when a span in `target` has a parent span in
`source` (a different service). Each such child span is one call; edge latency
and errors describe those child spans, i.e. how the called service behaved.

A NODE's numbers count only ENTRY spans: where a request enters the service
(kind SERVER, or a root span). That makes request_count "requests handled" and
error_rate "share of requests that failed", without the SDK's internal spans
(e.g. "http send"/"http receive") inflating the counts.

Spans belong to the time range by their own start time (a live map counts calls
when they happened). Duplicate spans are removed, as in queries/traces.py.
"""

from typing import Any

import duckdb

from heimdall.queries.common import rows_as_dicts, time_range_ns

WINDOW_SPANS = """
    window_spans AS (
        SELECT *
        FROM spans
        WHERE start_time >= $from_ns AND start_time <= $to_ns
        QUALIFY row_number() OVER (PARTITION BY trace_id, span_id ORDER BY end_time DESC) = 1
    )
"""

NODES_SQL = f"""
WITH {WINDOW_SPANS},
entries AS (
    SELECT * FROM window_spans WHERE kind = 'SERVER' OR parent_span_id IS NULL
)
SELECT
    s.service AS id,
    count(e.span_id) AS request_count,
    count(e.span_id) FILTER (WHERE e.status_code = 'ERROR') AS error_count,
    avg(e.duration_ns) / 1e6 AS avg_duration_ms,
    quantile_cont(e.duration_ns, 0.95) / 1e6 AS p95_duration_ms
FROM (SELECT DISTINCT service FROM window_spans) s
LEFT JOIN entries e ON e.service = s.service
GROUP BY s.service
ORDER BY s.service
"""

EDGES_SQL = f"""
WITH {WINDOW_SPANS},
parents AS (
    -- A parent may have started before the window, so look it up across the
    -- whole trace, not just the window.
    SELECT trace_id, span_id, any_value(service) AS service
    FROM spans
    WHERE trace_id IN (SELECT DISTINCT trace_id FROM window_spans)
    GROUP BY trace_id, span_id
)
SELECT
    p.service AS source,
    c.service AS target,
    count(*) AS call_count,
    avg(c.duration_ns) / 1e6 AS avg_duration_ms,
    quantile_cont(c.duration_ns, 0.95) / 1e6 AS p95_duration_ms,
    count(*) FILTER (WHERE c.status_code = 'ERROR') AS error_count
FROM window_spans c
JOIN parents p ON p.trace_id = c.trace_id AND p.span_id = c.parent_span_id
WHERE p.service <> c.service
GROUP BY p.service, c.service
ORDER BY p.service, c.service
"""


def _rate(errors: int, total: int) -> float:
    return errors / total if total else 0.0


def service_nodes(cur: duckdb.DuckDBPyConnection, from_ns: int, to_ns: int) -> list[dict[str, Any]]:
    cur.execute(NODES_SQL, {"from_ns": from_ns, "to_ns": to_ns})
    nodes = rows_as_dicts(cur)
    for n in nodes:
        n["error_rate"] = _rate(n["error_count"], n["request_count"])
    return nodes


def service_edges(cur: duckdb.DuckDBPyConnection, from_ns: int, to_ns: int) -> list[dict[str, Any]]:
    cur.execute(EDGES_SQL, {"from_ns": from_ns, "to_ns": to_ns})
    edges = rows_as_dicts(cur)
    for e in edges:
        e["error_rate"] = _rate(e["error_count"], e["call_count"])
    return edges


def service_map(
    cur: duckdb.DuckDBPyConnection,
    *,
    from_ms: float | None = None,
    to_ms: float | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Nodes (every service seen in the range) and edges (service-to-service calls)."""
    from_ns, to_ns = time_range_ns(from_ms, to_ms)
    return {
        "nodes": service_nodes(cur, from_ns, to_ns),
        "edges": service_edges(cur, from_ns, to_ns),
    }
