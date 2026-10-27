"""Helpers shared by the query modules."""

import time
from typing import Any

import duckdb

DEFAULT_WINDOW_MS = 60 * 60 * 1000  # list endpoints default to the last 1 hour


def time_range_ns(from_ms: float | None, to_ms: float | None) -> tuple[int, int]:
    """Default to the last hour; return the range in nanoseconds."""
    now_ms = time.time() * 1000
    to_ms = now_ms if to_ms is None else to_ms
    from_ms = to_ms - DEFAULT_WINDOW_MS if from_ms is None else from_ms
    return int(from_ms * 1_000_000), int(to_ms * 1_000_000)


def rows_as_dicts(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]
