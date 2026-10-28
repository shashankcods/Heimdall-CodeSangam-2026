# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.28.0",
#     "opentelemetry-proto>=1.45.0",
#     "protobuf>=7.0.0",
# ]
# ///
"""Heimdall Ingestion Throughput & Backpressure Benchmark.

Measures, from the client side:
1. Accepted throughput: spans/second answered with HTTP 200, i.e. QUEUED for writing.
2. HTTP response latency percentiles (p50, p95, p99).
3. Backpressure: how many requests got HTTP 429 (Too Many Requests).

A 200 means "queued", not "on disk". To measure how fast spans are actually
written (and that none are lost), run the server via bench/writer_stats.py.

Every batch carries unique trace/span IDs in 4-span trees (gateway -> order ->
db, payment), like real SDK exports; reusing one payload would just resend the
same spans, which Heimdall's read-time de-duplication then hides.

Usage:
    uv run bench/bench_ingest.py [--url http://127.0.0.1:4318/v1/traces] [--batches 200] [--concurrency 10] [--gzip]
"""

import argparse
import asyncio
import gzip
import os
import sys
import time
import uuid

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span as OtlpSpan, Status

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"


# One trace = 4 spans across 3 services: (service, name, parent index, offset ms, duration ms).
TRACE_SHAPE = [
    ("gateway", "POST /order", None, 0, 45),
    ("order-service", "POST /orders", 0, 2, 40),
    ("order-service", "INSERT orders", 1, 4, 9),
    ("payment-service", "POST /charge", 1, 15, 25),
]


def _kv(key: str, value) -> KeyValue:
    any_value = AnyValue(int_value=value) if isinstance(value, int) else AnyValue(string_value=value)
    return KeyValue(key=key, value=any_value)


def build_otlp_payload(spans_count: int = 100) -> bytes:
    """One serialized ExportTraceServiceRequest with fresh IDs, grouped into 4-span traces."""
    request = ExportTraceServiceRequest()
    by_service: dict[str, ScopeSpans] = {}
    now = time.time_ns()
    built = 0
    while built < spans_count:
        trace_id = uuid.uuid4().bytes
        span_ids = [uuid.uuid4().bytes[:8] for _ in TRACE_SHAPE]
        for i, (service, name, parent, offset_ms, dur_ms) in enumerate(TRACE_SHAPE):
            if built == spans_count:
                break
            start = now + offset_ms * 1_000_000
            span = OtlpSpan(
                trace_id=trace_id,
                span_id=span_ids[i],
                parent_span_id=span_ids[parent] if parent is not None else b"",
                name=name,
                kind=OtlpSpan.SPAN_KIND_SERVER,
                start_time_unix_nano=start,
                end_time_unix_nano=start + dur_ms * 1_000_000,
                status=Status(code=Status.STATUS_CODE_OK),
                attributes=[_kv("http.route", name.split(" ")[-1]), _kv("http.response.status_code", 200)],
            )
            by_service.setdefault(service, ScopeSpans()).spans.append(span)
            built += 1

    for service, scope_spans in by_service.items():
        resource = Resource(attributes=[_kv("service.name", service)])
        request.resource_spans.append(ResourceSpans(resource=resource, scope_spans=[scope_spans]))
    return request.SerializeToString()


async def send_worker(
    client: httpx.AsyncClient,
    url: str,
    payloads: list[bytes],
    headers: dict[str, str],
    queue: asyncio.Queue,
    results: list,
):
    while not queue.empty():
        try:
            batch_index = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        start = time.perf_counter()
        try:
            resp = await client.post(
                url,
                content=payloads[batch_index],
                headers=headers,
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
    use_gzip: bool = False,
):
    print("=" * 65)
    print("        HEIMDALL INGESTION THROUGHPUT & LATENCY BENCHMARK")
    print(f"  Target URL:        {target_url}")
    print(f"  Total Batches:     {total_batches:,} requests")
    print(f"  Spans Per Batch:   {spans_per_batch:,} spans/batch")
    print(f"  Total Spans:       {total_batches * spans_per_batch:,} spans")
    print(f"  Concurrency:       {concurrency} workers")
    print(f"  Compression:       {'gzip' if use_gzip else 'none'}")
    print("=" * 65)

    # Pre-generate every payload (unique IDs per batch) before the clock starts.
    print(f"\n[1/3] Building {total_batches:,} protobuf payloads with unique IDs...")
    payloads = [build_otlp_payload(spans_count=spans_per_batch) for _ in range(total_batches)]
    headers = {"Content-Type": PROTOBUF_CONTENT_TYPE}
    if use_gzip:
        payloads = [gzip.compress(p) for p in payloads]
        headers["Content-Encoding"] = "gzip"
    print(f"      Payload size: {len(payloads[0]):,} bytes ({len(payloads[0])/1024:.1f} KB) each")

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
            asyncio.create_task(send_worker(client, target_url, payloads, headers, work_queue, results))
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
    print(f"  Accepted Throughput:  {spans_per_sec:,.0f} spans/sec  (queued; see writer_stats.py for written)")
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
        # 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 first and
        # adds ~2 s per new connection, which would distort the latency numbers.
        default=os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318") + "/v1/traces",
        help="Target OTLP endpoint (default http://127.0.0.1:4318/v1/traces)",
    )
    parser.add_argument("--batches", type=int, default=100, help="Total batches to send (default 100)")
    parser.add_argument("--spans-per-batch", type=int, default=200, help="Spans per batch (default 200)")
    parser.add_argument("--concurrency", type=int, default=10, help="Concurrent workers (default 10)")
    parser.add_argument("--gzip", action="store_true", help="Send gzip-compressed bodies, like OTel SDKs do")
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
            use_gzip=args.gzip,
        )
    )


if __name__ == "__main__":
    main()
