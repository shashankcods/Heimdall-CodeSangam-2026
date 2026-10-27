"""Trace read queries: the trace list, one trace's spans, and service names.

Rules applied in every query here (decided in step 2a):
- Duplicate spans (same trace_id + span_id, e.g. from SDK retries) are removed at
  read time, since the table has no primary key.
- A trace belongs to the moment its EARLIEST span started. Time ranges match on
  that, so a trace never shows up partially in a window it started before.
- Times leave the database as milliseconds (float); see queries/spans.py.
"""

import json
from typing import Any

import duckdb

from heimdall.queries.common import rows_as_dicts, time_range_ns


# For each root field: take it from the span with no parent if the trace has
# one, otherwise from the earliest span (the root may not have arrived yet).
def _root_field(column: str) -> str:
    return f"""
        CASE WHEN count(*) FILTER (WHERE parent_span_id IS NULL) > 0
             THEN arg_min({column}, start_time) FILTER (WHERE parent_span_id IS NULL)
             ELSE arg_min({column}, start_time)
        END"""


LIST_TRACES_SQL = f"""
WITH candidates AS (
    SELECT trace_id
    FROM spans
    GROUP BY trace_id
    HAVING min(start_time) >= $from_ns AND min(start_time) <= $to_ns
),
deduped AS (
    SELECT *
    FROM spans
    WHERE trace_id IN (SELECT trace_id FROM candidates)
    QUALIFY row_number() OVER (PARTITION BY trace_id, span_id ORDER BY end_time DESC) = 1
),
traces AS (
    SELECT
        trace_id,
        {_root_field("service")}     AS root_service,
        {_root_field("name")}        AS root_name,
        {_root_field("http_status")} AS http_status,
        min(start_time)              AS start_time,
        max(end_time)                AS end_time,
        count(*)                     AS span_count,
        count(DISTINCT service)      AS service_count,
        count(*) FILTER (WHERE status_code = 'ERROR') AS error_count,
        bool_or(service = $service)  AS has_service
    FROM deduped
    GROUP BY trace_id
)
SELECT
    trace_id, root_service, root_name,
    start_time / 1e6              AS start_ms,
    (end_time - start_time) / 1e6 AS duration_ms,
    span_count, service_count, error_count,
    error_count > 0               AS has_error,
    http_status
FROM traces
WHERE ($service IS NULL OR has_service)
  AND (NOT $errors_only OR error_count > 0)
  AND ($min_duration_ns IS NULL OR end_time - start_time >= $min_duration_ns)
ORDER BY start_time DESC, trace_id
LIMIT $limit
"""


def list_traces(
    cur: duckdb.DuckDBPyConnection,
    *,
    service: str | None = None,
    errors_only: bool = False,
    min_duration_ms: float | None = None,
    from_ms: float | None = None,
    to_ms: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """One summary row per trace, newest first."""
    from_ns, to_ns = time_range_ns(from_ms, to_ms)
    cur.execute(LIST_TRACES_SQL, {
        "from_ns": from_ns,
        "to_ns": to_ns,
        "service": service,
        "errors_only": errors_only,
        "min_duration_ns": None if min_duration_ms is None else int(min_duration_ms * 1_000_000),
        "limit": limit,
    })
    return rows_as_dicts(cur)


GET_TRACE_SQL = """
SELECT
    span_id, parent_span_id, service, name, kind,
    start_time / 1e6  AS start_ms,
    end_time / 1e6    AS end_ms,
    duration_ns / 1e6 AS duration_ms,
    status_code, status_msg, http_status, route, attributes
FROM spans
WHERE trace_id = $trace_id
QUALIFY row_number() OVER (PARTITION BY span_id ORDER BY end_time DESC) = 1
ORDER BY start_time, span_id
"""


def get_trace(cur: duckdb.DuckDBPyConnection, trace_id: str) -> dict[str, Any] | None:
    """All spans of one trace as a flat list sorted by start. None if unknown.

    The tree is built by the caller from parent_span_id (flat by design: a span
    whose parent hasn't arrived yet is still returned).
    """
    cur.execute(GET_TRACE_SQL, {"trace_id": trace_id})
    spans = rows_as_dicts(cur)
    if not spans:
        return None
    for span in spans:
        span["attributes"] = json.loads(span["attributes"]) if span["attributes"] else {}
    start_ms = min(s["start_ms"] for s in spans)
    end_ms = max(s["end_ms"] for s in spans)
    return {
        "trace_id": trace_id,
        "start_ms": start_ms,
        "duration_ms": end_ms - start_ms,
        "spans": spans,
    }


def list_services(
    cur: duckdb.DuckDBPyConnection,
    *,
    from_ms: float | None = None,
    to_ms: float | None = None,
) -> list[str]:
    """Service names that produced spans in the range, alphabetical."""
    from_ns, to_ns = time_range_ns(from_ms, to_ms)
    cur.execute(
        """
        SELECT DISTINCT service FROM spans
        WHERE start_time >= $from_ns AND start_time <= $to_ns
        ORDER BY service
        """,
        {"from_ns": from_ns, "to_ns": to_ns},
    )
    return [row[0] for row in cur.fetchall()]
