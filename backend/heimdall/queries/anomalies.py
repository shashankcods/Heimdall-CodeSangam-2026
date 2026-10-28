"""Latency anomaly detection: operations whose p99 jumped versus their own baseline.

For each operation (service + span name) the recent window (default: last 5 min)
is compared with a baseline window just before it (default: the 60 min before).
An operation is flagged when its recent p99 latency is at least `min_ratio` times
its baseline p99 AND at least `min_increase_ms` slower, with at least
`min_samples` spans in each window (too little data means no verdict).

Each result says whether the operation's error rate spiked in the same window
(`error_spike`), which is the "p99 spiked during an error cluster" case; those
are listed first, then by how large the latency jump is. `example_trace_id` is
the slowest recent trace for that operation, to jump straight to an example.

Everything is one SQL query, so it reads one consistent snapshot.
"""

import time
from typing import Any

import duckdb

from heimdall.queries.common import rows_as_dicts

MS = 1_000_000  # nanoseconds per millisecond

ANOMALIES_SQL = """
WITH window_spans AS (
    SELECT *, start_time >= $recent_start AS recent
    FROM spans
    WHERE start_time >= $baseline_start AND start_time <= $now
    QUALIFY row_number() OVER (PARTITION BY trace_id, span_id ORDER BY end_time DESC) = 1
),
ops AS (
    SELECT
        service,
        name,
        count(*) FILTER (WHERE recent)                                  AS recent_count,
        count(*) FILTER (WHERE NOT recent)                              AS baseline_count,
        quantile_cont(duration_ns, 0.99) FILTER (WHERE recent)          AS recent_p99,
        quantile_cont(duration_ns, 0.99) FILTER (WHERE NOT recent)      AS baseline_p99,
        count(*) FILTER (WHERE recent AND status_code = 'ERROR')        AS recent_errors,
        count(*) FILTER (WHERE NOT recent AND status_code = 'ERROR')    AS baseline_errors,
        arg_max(trace_id, duration_ns) FILTER (WHERE recent)            AS example_trace_id
    FROM window_spans
    GROUP BY service, name
),
scored AS (
    SELECT *,
        recent_p99 / greatest(baseline_p99, $one_ms)    AS p99_ratio,
        recent_errors / recent_count                    AS recent_error_rate,
        baseline_errors / baseline_count                AS baseline_error_rate
    FROM ops
    WHERE recent_count >= $min_samples AND baseline_count >= $min_samples
)
SELECT
    service,
    name,
    recent_p99 / 1e6                AS recent_p99_ms,
    baseline_p99 / 1e6              AS baseline_p99_ms,
    p99_ratio,
    recent_count,
    baseline_count,
    recent_error_rate,
    baseline_error_rate,
    recent_error_rate >= greatest($min_error_rate, 2 * baseline_error_rate) AS error_spike,
    example_trace_id
FROM scored
WHERE p99_ratio >= $min_ratio
  AND recent_p99 - baseline_p99 >= $min_increase_ns
ORDER BY error_spike DESC, p99_ratio DESC, service, name
LIMIT $limit
"""


def find_anomalies(
    cur: duckdb.DuckDBPyConnection,
    *,
    recent_minutes: float = 5,
    baseline_minutes: float = 60,
    min_ratio: float = 2.0,
    min_increase_ms: float = 20,
    min_samples: int = 10,
    min_error_rate: float = 0.05,
    limit: int = 20,
    now_ms: float | None = None,
) -> dict[str, Any]:
    """Operations whose recent p99 latency spiked versus their baseline, worst first."""
    now_ns = int((time.time() * 1000 if now_ms is None else now_ms) * MS)
    recent_start = now_ns - int(recent_minutes * 60_000 * MS)
    baseline_start = recent_start - int(baseline_minutes * 60_000 * MS)
    cur.execute(ANOMALIES_SQL, {
        "now": now_ns,
        "recent_start": recent_start,
        "baseline_start": baseline_start,
        "one_ms": MS,
        "min_ratio": min_ratio,
        "min_increase_ns": int(min_increase_ms * MS),
        "min_samples": min_samples,
        "min_error_rate": min_error_rate,
        "limit": limit,
    })
    return {
        "recent_window": {"from_ms": recent_start / MS, "to_ms": now_ns / MS},
        "baseline_window": {"from_ms": baseline_start / MS, "to_ms": recent_start / MS},
        "anomalies": rows_as_dicts(cur),
    }
