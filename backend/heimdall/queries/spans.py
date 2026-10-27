"""Span read queries. All SQL for spans lives in queries/, never in the API layer.

Times leave the database as milliseconds (float), because JavaScript can't hold
nanosecond timestamps exactly (they exceed 2^53).
"""

from typing import Any

import duckdb

from heimdall.queries.common import rows_as_dicts


def recent_spans(cur: duckdb.DuckDBPyConnection, limit: int = 100) -> list[dict[str, Any]]:
    """Most recent spans by start time. Debug view for the skeleton slice."""
    cur.execute(
        """
        SELECT trace_id, span_id, parent_span_id, service, name,
               start_time / 1e6  AS start_ms,
               duration_ns / 1e6 AS duration_ms,
               status_code, http_status
        FROM spans
        ORDER BY start_time DESC
        LIMIT ?
        """,
        [limit],
    )
    return rows_as_dicts(cur)
