"""Log read queries. All SQL for logs lives in queries/, never in the API layer.

Times leave the database as milliseconds (float), consistent with spans and traces.
"""

import json
import re
from typing import Any

import duckdb

MAX_LOGS_LIMIT = 1000


def _rows_as_dicts(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def search_logs(
    cur: duckdb.DuckDBPyConnection,
    *,
    query: str | None = None,
    regex: str | None = None,
    service: str | None = None,
    severity: str | None = None,
    trace_id: str | None = None,
    from_ms: float | None = None,
    to_ms: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search stored logs with optional filters, newest first."""
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise ValueError("from_ms cannot be greater than to_ms")

    if limit < 1:
        raise ValueError("limit must be greater than or equal to 1")

    if regex is not None and regex != "":
        try:
            re.compile(regex)
        except re.error as e:
            raise ValueError(f"Invalid regular expression: {e}")

    effective_limit = min(limit, MAX_LOGS_LIMIT)

    conditions: list[str] = []
    params: list[Any] = []

    if query is not None and query != "":
        conditions.append("contains(lower(body), lower(?))")
        params.append(query)

    if regex is not None and regex != "":
        conditions.append("regexp_matches(body, ?)")
        params.append(regex)

    if service is not None and service != "":
        conditions.append("service = ?")
        params.append(service)

    if severity is not None and severity != "":
        conditions.append("upper(severity) = upper(?)")
        params.append(severity)

    if trace_id is not None and trace_id != "":
        conditions.append("lower(trace_id) = lower(?)")
        params.append(trace_id.lower())

    if from_ms is not None:
        from_ns = int(from_ms * 1_000_000)
        conditions.append("timestamp >= ?")
        params.append(from_ns)

    if to_ms is not None:
        to_ns = int(to_ms * 1_000_000)
        conditions.append("timestamp <= ?")
        params.append(to_ns)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query_sql = f"""
        SELECT
            timestamp / 1e6 AS timestamp_ms,
            timestamp / 1e6 AS timestamp,
            service,
            severity,
            body,
            trace_id,
            span_id,
            attributes
        FROM logs
        {where_clause}
        ORDER BY timestamp DESC, rowid DESC
        LIMIT ?
    """
    params.append(effective_limit)

    cur.execute(query_sql, params)
    logs = _rows_as_dicts(cur)

    for item in logs:
        attrs = item.get("attributes")
        if attrs:
            try:
                item["attributes"] = json.loads(attrs) if isinstance(attrs, str) else attrs
            except Exception:
                item["attributes"] = {}
        else:
            item["attributes"] = {}

    return logs
