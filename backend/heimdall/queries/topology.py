"""Service topology read queries. All SQL for topology lives in queries/, never in the API layer.

Derives a directed service-to-service dependency graph from parent-child span relationships across traces.
"""

import time
from typing import Any

import duckdb

DEFAULT_WINDOW_MS = 60 * 60 * 1000  # last 1 hour


def _time_range_ns(from_ms: float | None, to_ms: float | None) -> tuple[int, int]:
    """Resolve time range bounds in nanoseconds, defaulting to the last hour."""
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise ValueError("from_ms cannot be greater than to_ms")

    now_ms = time.time() * 1000
    resolved_to_ms = now_ms if to_ms is None else to_ms
    resolved_from_ms = (resolved_to_ms - DEFAULT_WINDOW_MS) if from_ms is None else from_ms

    if resolved_from_ms > resolved_to_ms:
        raise ValueError("from_ms cannot be greater than to_ms")

    return int(resolved_from_ms * 1_000_000), int(resolved_to_ms * 1_000_000)


def _rows_as_dicts(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


SERVICE_EDGES_SQL = """
WITH deduped AS (
    SELECT *
    FROM spans
    QUALIFY row_number() OVER (PARTITION BY trace_id, span_id ORDER BY end_time DESC) = 1
),
cross_calls AS (
    SELECT
        parent.service AS source,
        child.service  AS target,
        child.duration_ns / 1e6 AS duration_ms,
        CASE
            WHEN child.status_code = 'ERROR' OR (child.http_status IS NOT NULL AND child.http_status >= 500)
            THEN 1
            ELSE 0
        END AS is_error
    FROM deduped child
    JOIN deduped parent
      ON child.trace_id = parent.trace_id
     AND child.parent_span_id = parent.span_id
    WHERE child.parent_span_id IS NOT NULL
      AND child.service != parent.service
      AND child.start_time >= $from_ns
      AND child.start_time <= $to_ns
)
SELECT
    source,
    target,
    count(*)::INT AS call_count,
    round(avg(duration_ms), 2)::DOUBLE AS avg_latency_ms,
    sum(is_error)::INT AS error_count,
    round(sum(is_error)::DOUBLE / count(*), 4)::DOUBLE AS error_rate
FROM cross_calls
GROUP BY source, target
ORDER BY source, target
"""

SERVICE_NODES_SQL = """
SELECT DISTINCT service
FROM spans
WHERE start_time >= $from_ns AND start_time <= $to_ns
ORDER BY service
"""


def service_edges(
    cur: duckdb.DuckDBPyConnection,
    *,
    from_ms: float | None = None,
    to_ms: float | None = None,
) -> list[dict[str, Any]]:
    """Directed cross-service dependency edges in the time window, sorted by source then target."""
    from_ns, to_ns = _time_range_ns(from_ms, to_ms)
    cur.execute(SERVICE_EDGES_SQL, {"from_ns": from_ns, "to_ns": to_ns})
    return _rows_as_dicts(cur)


def service_map(
    cur: duckdb.DuckDBPyConnection,
    *,
    from_ms: float | None = None,
    to_ms: float | None = None,
) -> dict[str, Any]:
    """Complete service map with nodes and edges in the time window."""
    from_ns, to_ns = _time_range_ns(from_ms, to_ms)

    # 1. Compute edges
    cur.execute(SERVICE_EDGES_SQL, {"from_ns": from_ns, "to_ns": to_ns})
    edges = _rows_as_dicts(cur)

    # 2. Compute distinct services active in the window
    cur.execute(SERVICE_NODES_SQL, {"from_ns": from_ns, "to_ns": to_ns})
    services = {row[0] for row in cur.fetchall()}

    # 3. Ensure edge endpoints are also included in nodes (e.g. parent span started slightly before window)
    for edge in edges:
        services.add(edge["source"])
        services.add(edge["target"])

    nodes = [{"id": s, "service": s} for s in sorted(services)]

    return {
        "nodes": nodes,
        "edges": edges,
    }
