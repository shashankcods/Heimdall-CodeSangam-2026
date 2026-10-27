# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "duckdb>=2.0.0",
#     "pyarrow>=26.0.0",
# ]
# ///
"""Synthetic telemetry data generator for DuckDB benchmarks at 1M and 10M span scales.

Imports schema and models from backend (read-only) as mandated by TEAM.md.

Usage:
    uv run bench/generate_dataset.py --spans 1000000 --db-path bench.duckdb
"""

import argparse
import json
import math
import os
import random
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import duckdb
import pyarrow as pa

# Ensure repo root is on sys.path to import backend modules without modifying them
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from heimdall.ingest.models import LogRecord, Span
from heimdall.store.schema import init_schema


def generate_trace_batch(
    num_traces: int,
    base_time_ns: int,
    time_window_ns: int,
    error_rate: float = 0.02,
) -> tuple[list[dict], list[dict]]:
    """Generate realistic 4-span trace trees and correlated logs.

    Services: gateway -> order-service -> db & payment-service.
    Latency: Log-normal distribution (median ~35ms, p99 long-tail up to 2.5s).
    """
    span_rows = []
    log_rows = []

    for _ in range(num_traces):
        trace_id = uuid.uuid4().hex  # 32 hex chars
        root_span_id = uuid.uuid4().hex[:16]  # 16 hex chars
        order_span_id = uuid.uuid4().hex[:16]
        db_span_id = uuid.uuid4().hex[:16]
        payment_span_id = uuid.uuid4().hex[:16]

        is_error = random.random() < error_rate

        # Trace start time within time window
        trace_start_ns = base_time_ns + int(random.random() * time_window_ns)

        # Durations (log-normal distribution with long-tail)
        # Normal request: ~20-60ms; Error request: 1.5s - 2.5s timeout
        if is_error:
            payment_dur_ms = random.uniform(1500.0, 2200.0)
            db_dur_ms = random.uniform(10.0, 30.0)
            order_dur_ms = payment_dur_ms + db_dur_ms + random.uniform(15.0, 30.0)
            root_dur_ms = order_dur_ms + random.uniform(5.0, 15.0)
            root_http = 500
            payment_http = 504
            status_code = "ERROR"
            status_msg = "Stripe gateway timeout after 1.9s, retry 0"
        else:
            db_dur_ms = random.lognormvariate(2.2, 0.4)  # ~9ms
            payment_dur_ms = random.lognormvariate(3.2, 0.5)  # ~25ms
            order_dur_ms = db_dur_ms + payment_dur_ms + random.uniform(4.0, 10.0)
            root_dur_ms = order_dur_ms + random.uniform(2.0, 6.0)
            root_http = 200
            payment_http = 200
            status_code = "OK"
            status_msg = None

        root_start = trace_start_ns
        root_end = root_start + int(root_dur_ms * 1_000_000)

        order_start = root_start + int(random.uniform(1.0, 3.0) * 1_000_000)
        order_end = order_start + int(order_dur_ms * 1_000_000)

        db_start = order_start + int(random.uniform(1.0, 2.0) * 1_000_000)
        db_end = db_start + int(db_dur_ms * 1_000_000)

        payment_start = db_end + int(random.uniform(1.0, 3.0) * 1_000_000)
        payment_end = payment_start + int(payment_dur_ms * 1_000_000)

        # 1. Gateway Span (Root)
        span_rows.append({
            "trace_id": trace_id,
            "span_id": root_span_id,
            "parent_span_id": None,
            "service": "gateway",
            "name": "POST /order",
            "kind": "SERVER",
            "start_time": root_start,
            "end_time": root_end,
            "duration_ns": root_end - root_start,
            "status_code": status_code,
            "status_msg": status_msg,
            "http_status": root_http,
            "route": "/order",
            "attributes": json.dumps({"http.method": "POST", "http.route": "/order", "http.status_code": root_http}),
        })

        # 2. Order Service Span
        span_rows.append({
            "trace_id": trace_id,
            "span_id": order_span_id,
            "parent_span_id": root_span_id,
            "service": "order-service",
            "name": "createOrder",
            "kind": "SERVER",
            "start_time": order_start,
            "end_time": order_end,
            "duration_ns": order_end - order_start,
            "status_code": status_code,
            "status_msg": status_msg,
            "http_status": root_http,
            "route": "/orders",
            "attributes": json.dumps({"http.method": "POST", "order.customer_id": f"cust_{random.randint(100, 999)}"}),
        })

        # 3. Database Client Span
        span_rows.append({
            "trace_id": trace_id,
            "span_id": db_span_id,
            "parent_span_id": order_span_id,
            "service": "order-service",
            "name": "INSERT orders",
            "kind": "CLIENT",
            "start_time": db_start,
            "end_time": db_end,
            "duration_ns": db_end - db_start,
            "status_code": "OK",
            "status_msg": None,
            "http_status": None,
            "route": None,
            "attributes": json.dumps({"db.system": "postgresql", "db.statement": "INSERT INTO orders VALUES ($1, $2)"}),
        })

        # 4. Payment Service Span
        span_rows.append({
            "trace_id": trace_id,
            "span_id": payment_span_id,
            "parent_span_id": order_span_id,
            "service": "payment-service",
            "name": "chargeCard",
            "kind": "SERVER",
            "start_time": payment_start,
            "end_time": payment_end,
            "duration_ns": payment_end - payment_start,
            "status_code": status_code,
            "status_msg": status_msg,
            "http_status": payment_http,
            "route": "/charge",
            "attributes": json.dumps({"http.method": "POST", "payment.provider": "stripe", "http.status_code": payment_http}),
        })

        # Correlated Log record
        log_rows.append({
            "timestamp": payment_start,
            "service": "payment-service",
            "severity": "ERROR" if is_error else "INFO",
            "body": status_msg or "calling Stripe, amount=49.99",
            "trace_id": trace_id,
            "span_id": payment_span_id,
            "attributes": json.dumps({"payment.provider": "stripe"}),
        })

    return span_rows, log_rows


def generate_benchmark_data(total_spans: int, db_path: str, batch_spans: int = 40000, include_logs: bool = True):
    print("=" * 65)
    print(f"      HEIMDALL SYNTHETIC BENCHMARK DATA GENERATOR")
    print(f"      Target: {total_spans:,} spans into {db_path}")
    print("=" * 65)

    # Spans per trace = 4
    total_traces = total_spans // 4
    traces_per_batch = batch_spans // 4

    # Time window: last 2 hours
    now_ns = int(time.time() * 1_000_000_000)
    window_ns = 2 * 3600 * 1_000_000_000
    base_time_ns = now_ns - window_ns

    # Connect to DuckDB
    conn = duckdb.connect(db_path)
    init_schema(conn)

    start_wall = time.perf_counter()
    spans_inserted = 0
    logs_inserted = 0
    traces_generated = 0

    cur = conn.cursor()

    while traces_generated < total_traces:
        count = min(traces_per_batch, total_traces - traces_generated)
        span_rows, log_rows = generate_trace_batch(
            count, base_time_ns, window_ns, error_rate=0.02
        )

        # Bulk insert spans via PyArrow table
        span_table = pa.Table.from_pylist(span_rows)
        cur.register("span_batch", span_table)
        cur.execute("BEGIN TRANSACTION")
        cur.execute("INSERT INTO spans SELECT * FROM span_batch")
        cur.execute("COMMIT")

        if include_logs and log_rows:
            log_table = pa.Table.from_pylist(log_rows)
            cur.register("log_batch", log_table)
            cur.execute("BEGIN TRANSACTION")
            cur.execute("INSERT INTO logs SELECT * FROM log_batch")
            cur.execute("COMMIT")
            logs_inserted += len(log_rows)

        spans_inserted += len(span_rows)
        traces_generated += count

        elapsed = time.perf_counter() - start_wall
        rate = spans_inserted / max(elapsed, 0.001)
        pct = (spans_inserted / total_spans) * 100
        print(
            f"\rProgress: {spans_inserted:>10,} / {total_spans:,} spans ({pct:>5.1f}%) | "
            f"Rate: {rate:>9,.0f} spans/s | Elapsed: {elapsed:>5.1f}s",
            end="",
            flush=True,
        )

    print()
    total_time = time.perf_counter() - start_wall
    avg_rate = spans_inserted / max(total_time, 0.001)

    # Verification queries
    total_in_db = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    total_errors = conn.execute("SELECT COUNT(*) FROM spans WHERE status_code = 'ERROR'").fetchone()[0]
    services = conn.execute("SELECT DISTINCT service FROM spans ORDER BY service").fetchall()
    p50, p95, p99 = conn.execute(
        "SELECT quantile_cont(duration_ns / 1000000.0, [0.5, 0.95, 0.99]) FROM spans"
    ).fetchone()[0]

    db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
    bytes_per_span = (os.path.getsize(db_path) / max(total_in_db, 1))

    conn.close()

    print("\n" + "-" * 65)
    print("BENCHMARK SUMMARY:")
    print(f"  Total Spans in DB:    {total_in_db:,}")
    print(f"  Total Errors in DB:   {total_errors:,} ({(total_errors / max(total_in_db, 1))*100:.2f}%)")
    print(f"  Services:             {[s[0] for s in services]}")
    print(f"  Latency Percentiles:  p50={p50:.1f}ms | p95={p95:.1f}ms | p99={p99:.1f}ms")
    print(f"  Database File Size:   {db_size_mb:.2f} MB ({bytes_per_span:.1f} bytes/span)")
    print(f"  Generation Time:      {total_time:.2f} s")
    print(f"  Average Throughput:   {avg_rate:,.0f} spans/sec")
    print("-" * 65)


def main():
    parser = argparse.ArgumentParser(description="Heimdall Benchmark Data Generator")
    parser.add_argument("--spans", type=int, default=100_000, help="Total spans to generate (default 100,000)")
    parser.add_argument("--batch-size", type=int, default=40_000, help="Spans per Arrow bulk insert batch")
    parser.add_argument("--db-path", default="bench_heimdall.duckdb", help="Target DuckDB file")
    parser.add_argument("--no-logs", action="store_true", help="Skip generating logs")
    args = parser.parse_args()

    generate_benchmark_data(
        total_spans=args.spans,
        db_path=args.db_path,
        batch_spans=args.batch_size,
        include_logs=not args.no_logs,
    )


if __name__ == "__main__":
    main()
