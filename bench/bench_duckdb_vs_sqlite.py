# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "duckdb>=2.0.0",
#     "pyarrow>=26.0.0",
# ]
# ///
"""Columnar (DuckDB) vs row store (SQLite) on identical Heimdall span data.

Answers "was columnar storage the right choice?" with a comparison that is fair
to SQLite, so the numbers hold up to scrutiny:

- Both engines get the same generated spans (generate_dataset.generate_trace_batch).
- SQLite gets the indexes a real deployment would add: (trace_id, span_id) for
  joins and lookups, start_time for time windows. DuckDB gets none.
- Every query computes the same answer on both engines (exact p95/p99 via window
  functions in SQLite) and the results are cross-checked.
- Times are the median of 7 warm runs. Storage is measured after a DuckDB
  CHECKPOINT and a SQLite VACUUM, so nothing is hiding in write-ahead logs.
- It includes a single-trace lookup, where SQLite's index is expected to win.

Usage:
    uv run bench/bench_duckdb_vs_sqlite.py [--spans 1000000]
"""

import argparse
import os
import platform
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import duckdb
import pyarrow as pa

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bench.generate_dataset import generate_trace_batch  # noqa: E402

DUCKDB_FILE = "compare_bench.duckdb"
SQLITE_FILE = "compare_bench.sqlite"
COLUMNS = [
    "trace_id", "span_id", "parent_span_id", "service", "name", "kind", "start_time", "end_time",
    "duration_ns", "status_code", "status_msg", "http_status", "route", "attributes",
]
SPANS_DDL = """
CREATE TABLE spans (
    trace_id VARCHAR NOT NULL, span_id VARCHAR NOT NULL, parent_span_id VARCHAR,
    service VARCHAR NOT NULL, name VARCHAR NOT NULL, kind VARCHAR,
    start_time BIGINT NOT NULL, end_time BIGINT NOT NULL, duration_ns BIGINT NOT NULL,
    status_code VARCHAR, status_msg VARCHAR, http_status INTEGER, route VARCHAR, attributes TEXT
)
"""
RUNS = 7


def remove_files() -> None:
    for f in (DUCKDB_FILE, f"{DUCKDB_FILE}.wal", SQLITE_FILE):
        if os.path.exists(f):
            os.remove(f)


def load(spans_count: int) -> tuple[duckdb.DuckDBPyConnection, sqlite3.Connection, dict]:
    print(f"[1/3] Generating {spans_count:,} spans ({spans_count // 4:,} traces) and loading both engines...")
    now_ns = time.time_ns()
    window_ns = 3600 * 1_000_000_000
    rows, _ = generate_trace_batch(spans_count // 4, now_ns - window_ns, window_ns, error_rate=0.02)

    duck = duckdb.connect(DUCKDB_FILE)
    duck.execute(SPANS_DDL)
    t0 = time.perf_counter()
    duck.register("batch", pa.Table.from_pylist(rows))
    duck.execute("INSERT INTO spans SELECT * FROM batch")
    duck.execute("CHECKPOINT")
    duck_insert_s = time.perf_counter() - t0

    lite = sqlite3.connect(SQLITE_FILE)
    lite.execute(SPANS_DDL)
    data = [tuple(r[c] for c in COLUMNS) for r in rows]
    t0 = time.perf_counter()
    lite.execute("BEGIN")
    lite.executemany(f"INSERT INTO spans VALUES ({','.join('?' * len(COLUMNS))})", data)
    lite.execute("COMMIT")
    lite_insert_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    lite.execute("CREATE INDEX idx_trace_span ON spans(trace_id, span_id)")
    lite.execute("CREATE INDEX idx_start ON spans(start_time)")
    lite.execute("ANALYZE")
    lite_index_s = time.perf_counter() - t0
    lite.execute("VACUUM")

    info = {
        "rows": len(rows),
        "duck_insert_s": duck_insert_s,
        "lite_insert_s": lite_insert_s,
        "lite_index_s": lite_index_s,
        "now_ns": now_ns,
        "sample_trace": rows[len(rows) // 2]["trace_id"],
    }
    return duck, lite, info


def queries(now_ns: int, sample_trace: str) -> dict[str, tuple[str, str]]:
    """name -> (DuckDB SQL, SQLite SQL). Same answer on both engines."""
    cutoff = now_ns - 5 * 60 * 1_000_000_000
    same = lambda sql: (sql, sql)  # noqa: E731
    return {
        "Error rate per service": same(
            "SELECT service, count(*), avg(duration_ns), sum(CASE WHEN status_code = 'ERROR' THEN 1 ELSE 0 END) "
            "FROM spans GROUP BY service ORDER BY service"
        ),
        "Exact p95/p99 latency per service": (
            "SELECT service, quantile_disc(duration_ns, 0.95), quantile_disc(duration_ns, 0.99) "
            "FROM spans GROUP BY service ORDER BY service",
            "WITH r AS (SELECT service, duration_ns, "
            "ROW_NUMBER() OVER (PARTITION BY service ORDER BY duration_ns) AS rn, "
            "COUNT(*) OVER (PARTITION BY service) AS n FROM spans) "
            "SELECT service, MIN(CASE WHEN rn >= (n * 95 + 99) / 100 THEN duration_ns END), "
            "MIN(CASE WHEN rn >= (n * 99 + 99) / 100 THEN duration_ns END) "
            "FROM r GROUP BY service ORDER BY service",
        ),
        "Service-map self-join": same(
            "SELECT p.service, c.service, count(*), avg(c.duration_ns) FROM spans c "
            "JOIN spans p ON p.trace_id = c.trace_id AND p.span_id = c.parent_span_id "
            "WHERE p.service <> c.service GROUP BY 1, 2 ORDER BY 1, 2"
        ),
        "Errors in the last 5 minutes": same(
            f"SELECT service, count(*) FROM spans WHERE start_time >= {cutoff} AND status_code = 'ERROR' "
            "GROUP BY service ORDER BY service"
        ),
        "Single trace lookup by id": same(
            f"SELECT * FROM spans WHERE trace_id = '{sample_trace}' ORDER BY start_time, span_id"
        ),
    }


def timed(conn, sql: str) -> tuple[float, list]:
    conn.execute(sql).fetchall()  # warm-up
    times, result = [], []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        result = conn.execute(sql).fetchall()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), result


def normalized(rows: list) -> list:
    return [tuple(round(v, 3) if isinstance(v, float) else v for v in r) for r in rows]


def run(spans_count: int) -> None:
    remove_files()
    print(f"Machine: {platform.processor() or platform.machine()}, {os.cpu_count()} logical CPUs | "
          f"Python {platform.python_version()} | DuckDB {duckdb.__version__} | SQLite {sqlite3.sqlite_version}")
    duck, lite, info = load(spans_count)

    print(f"[2/3] Running queries (median of {RUNS} warm runs)...")
    results = []
    for name, (duck_sql, lite_sql) in queries(info["now_ns"], info["sample_trace"]).items():
        duck_ms, duck_rows = timed(duck, duck_sql)
        lite_ms, lite_rows = timed(lite, lite_sql)
        results.append((name, duck_ms, lite_ms, normalized(duck_rows) == normalized(lite_rows)))

    duck.close()
    lite.close()
    duck_bytes = os.path.getsize(DUCKDB_FILE)
    if os.path.exists(f"{DUCKDB_FILE}.wal"):
        duck_bytes += os.path.getsize(f"{DUCKDB_FILE}.wal")
    lite_bytes = os.path.getsize(SQLITE_FILE)
    remove_files()

    rows = info["rows"]
    print("[3/3] Results\n")
    print("=" * 84)
    print(f"  COLUMNAR (DuckDB) vs ROW STORE (SQLite, indexed) | {rows:,} spans")
    print("=" * 84)
    print(f"  {'Query':<36}{'DuckDB':>11}{'SQLite':>12}{'Result':>16}   Same answer")
    print("-" * 84)
    for name, duck_ms, lite_ms, same in results:
        verdict = f"{lite_ms / duck_ms:,.1f}x faster" if lite_ms >= duck_ms else f"{duck_ms / lite_ms:,.1f}x slower"
        print(f"  {name:<36}{duck_ms:>9.2f}ms{lite_ms:>10.2f}ms{verdict:>16}   {'yes' if same else 'NO'}")
    print("-" * 84)
    print(f"  {'Storage per span':<36}{duck_bytes / rows:>9.0f} B{lite_bytes / rows:>10.0f} B"
          f"{lite_bytes / duck_bytes:>9.1f}x smaller   (SQLite size incl. its indexes)")
    print(f"  {'Bulk insert':<36}{rows / info['duck_insert_s']:>7,.0f}/s{rows / info['lite_insert_s']:>8,.0f}/s"
          f"   (+{info['lite_index_s']:.1f}s for SQLite to build its 2 indexes)")
    print("=" * 84)
    if not all(same for *_, same in results):
        print("WARNING: some queries returned different answers on the two engines; check the SQL.")


def main() -> None:
    parser = argparse.ArgumentParser(description="DuckDB vs SQLite comparison on Heimdall span data")
    parser.add_argument("--spans", type=int, default=200_000, help="Spans to compare (default 200,000)")
    run(parser.parse_args().spans)


if __name__ == "__main__":
    main()
