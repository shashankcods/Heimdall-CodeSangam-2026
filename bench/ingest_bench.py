# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.28.0",
#     "opentelemetry-proto>=1.45.0",
#     "protobuf>=7.0.0",
# ]
# ///
"""Heimdall Ingestion Throughput & Backpressure Benchmark.

Measures:
1. Ingestion throughput (spans/second and requests/second).
2. HTTP response latency percentiles (p50, p95, p99).
3. Backpressure resilience: verifies HTTP 429 (Too Many Requests) when queue saturates.

Usage:
    uv run bench/ingest_bench.py [--url http://localhost:4318/v1/traces] [--batches 200] [--concurrency 10]
"""

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span as OtlpSpan, Status

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"


def build_otlp_payload(spans_count: int = 100) -> bytes:
    """Construct an authentic serialized ExportTraceServiceRequest protobuf payload."""
    request = ExportTraceServiceRequest()
    resource = Resource(
        attributes=[KeyValue(key="service.name", value=AnyValue(string_value="bench-service"))]
    )
    scope_spans = ScopeSpans()

    trace_id_bytes = uuid.uuid4().bytes
    for _ in range(spans_count):
        span_id_bytes = uuid.uuid4().bytes[:8]
        span = OtlpSpan(
            trace_id=trace_id_bytes,
            span_id=span_id_bytes,
            name="POST /benchmark/operation",
            kind=OtlpSpan.SPAN_KIND_SERVER,
            start_time_unix_nano=time.time_ns(),
            end_time_unix_nano=time.time_ns() + 25_000_000,
            status=Status(code=Status.STATUS_CODE_OK),
            attributes=[
                KeyValue(key="http.route", value=AnyValue(string_value="/benchmark/operation")),
                KeyValue(key="http.status_code", value=AnyValue(int_value=200)),
            ],
        )
        scope_spans.spans.append(span)

    request.resource_spans.append(
        ResourceSpans(resource=resource, scope_spans=[scope_spans])
    )
    return request.SerializeToString()


async def send_worker(
    client: httpx.AsyncClient,
    url: str,
    payload: bytes,
    queue: asyncio.Queue,
    results: list,
):
    while not queue.empty():
        try:
            _ = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        start = time.perf_counter()
        try:
            resp = await client.post(
                url,
                content=payload,
                headers={"Content-Type": PROTOBUF_CONTENT_TYPE},
                timeout=10.0,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            retry_after = resp.headers.get("retry-after")
            results.append((resp.status_code, elapsed_ms, retry_after))
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            results.append((0, elapsed_ms, str(exc)))
        finally:
            queue.task_done()


def calculate_percentile(sorted_list: list[float], pct: float) -> float:
    if not sorted_list:
        return 0.0
    idx = int(math_ceil((pct / 100.0) * len(sorted_list))) - 1
    idx = max(0, min(idx, len(sorted_list) - 1))
    return sorted_list[idx]


def math_ceil(val: float) -> int:
    return int(val) if val == int(val) else int(val) + 1


async def run_benchmark(
    target_url: str,
    total_batches: int,
    spans_per_batch: int,
    concurrency: int,
):
    print("=" * 65)
    print("        HEIMDALL INGESTION THROUGHPUT & LATENCY BENCHMARK")
    print(f"  Target URL:        {target_url}")
    print(f"  Total Batches:     {total_batches:,} requests")
    print(f"  Spans Per Batch:   {spans_per_batch:,} spans/batch")
    print(f"  Total Spans:       {total_batches * spans_per_batch:,} spans")
    print(f"  Concurrency:       {concurrency} workers")
    print("=" * 65)

    # Pre-generate payload
    print("\n[1/3] Building protobuf payload...")
    payload = build_otlp_payload(spans_count=spans_per_batch)
    print(f"      Payload size: {len(payload):,} bytes ({len(payload)/1024:.1f} KB)")

    # Prepare work queue
    work_queue = asyncio.Queue()
    for i in range(total_batches):
        work_queue.put_nowait(i)

    results = []

    print(f"\n[2/3] Firing {total_batches} batches with concurrency {concurrency}...")
    start_time = time.perf_counter()

    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        workers = [
            asyncio.create_task(send_worker(client, target_url, payload, work_queue, results))
            for _ in range(concurrency)
        ]
        await asyncio.gather(*workers)

    total_time = time.perf_counter() - start_time
    print(f"[3/3] Completed in {total_time:.2f} seconds.")

    # Analyze results
    success_200 = sum(1 for code, _, _ in results if code == 200)
    rate_limited_429 = sum(1 for code, _, _ in results if code == 429)
    client_errors_4xx = sum(1 for code, _, _ in results if 400 <= code < 500 and code != 429)
    server_errors_5xx = sum(1 for code, _, _ in results if code >= 500)
    connection_errors = sum(1 for code, _, _ in results if code == 0)

    total_spans_accepted = success_200 * spans_per_batch
    spans_per_sec = total_spans_accepted / max(total_time, 0.001)
    reqs_per_sec = len(results) / max(total_time, 0.001)

    latencies = sorted(lat for _, lat, _ in results)
    p50 = calculate_percentile(latencies, 50.0)
    p90 = calculate_percentile(latencies, 90.0)
    p95 = calculate_percentile(latencies, 95.0)
    p99 = calculate_percentile(latencies, 99.0)
    max_lat = latencies[-1] if latencies else 0.0

    print("\n" + "=" * 65)
    print("                  BENCHMARK RESULTS REPORT")
    print("=" * 65)
    print(f"  Duration:             {total_time:.2f} s")
    print(f"  Total Requests Sent:  {len(results):,}")
    print(f"  HTTP 200 (Accepted):  {success_200:,}  ({(success_200/len(results))*100:.1f}%)")
    print(f"  HTTP 429 (Backpressure):{rate_limited_429:,}  ({(rate_limited_429/len(results))*100:.1f}%)")
    if client_errors_4xx > 0:
        print(f"  HTTP 4xx (Client Err):{client_errors_4xx:,}")
    if server_errors_5xx > 0:
        print(f"  HTTP 5xx (Server Err):{server_errors_5xx:,}")
    if connection_errors > 0:
        print(f"  Connection Failures:  {connection_errors:,}")

    print("-" * 65)
    print(f"  Ingest Throughput:    {spans_per_sec:,.0f} spans/sec")
    print(f"  Request Throughput:   {reqs_per_sec:,.1f} reqs/sec")
    print("-" * 65)
    print("  Response Latency Distribution:")
    print(f"    p50 (Median):       {p50:.2f} ms")
    print(f"    p90:                {p90:.2f} ms")
    print(f"    p95:                {p95:.2f} ms")
    print(f"    p99:                {p99:.2f} ms")
    print(f"    Max:                {max_lat:.2f} ms")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Heimdall Ingestion Benchmark")
    parser.add_argument(
        "--url",
        default=os.environ.get("HEIMDALL_URL", "http://localhost:4318") + "/v1/traces",
        help="Target OTLP endpoint (default http://localhost:4318/v1/traces)",
    )
    parser.add_argument("--batches", type=int, default=100, help="Total batches to send (default 100)")
    parser.add_argument("--spans-per-batch", type=int, default=200, help="Spans per batch (default 200)")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent workers (default 10)")
    args = parser.parse_args()

    # Pre-check health
    base_url = args.url.split("/v1/")[0]
    try:
        r = httpx.get(f"{base_url}/api/health", timeout=2.0)
        if r.status_code != 200:
            print(f"Warning: {base_url}/api/health returned status {r.status_code}")
    except Exception:
        print(f"Error: Heimdall backend is not reachable at {base_url}.")
        print("Please start Heimdall first:")
        print("  cd backend && uv run uvicorn heimdall.api.main:app --port 4318\n")
        sys.exit(1)

    asyncio.run(
        run_benchmark(
            target_url=args.url,
            total_batches=args.batches,
            spans_per_batch=args.spans_per_batch,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()
