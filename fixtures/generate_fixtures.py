# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "httpx",
#     "opentelemetry-sdk",
#     "opentelemetry-exporter-otlp-proto-http",
# ]
# ///
"""Automated fixture generator: emits real OpenTelemetry wire payloads into the catcher server.

Generates:
1. trace_single_root_gzip.pb      - Single root span (gzip compressed)
2. trace_single_root_raw.pb       - Single root span (uncompressed)
3. trace_four_span_chain.pb       - 4-span chain across services (gateway -> order -> db -> payment)
4. trace_error_span.pb            - Error span with StatusCode.ERROR and message
5. trace_no_http_attrs.pb         - Internal computation span without any http.* attributes
6. logs_with_trace_context.pb     - Logs carrying trace_id and span_id
7. logs_without_trace_context.pb  - System logs without trace context
8. logs_with_trace_context_gzip.pb- Gzipped logs with trace context

Usage:
    uv run fixtures/generate_fixtures.py
"""

import logging
import threading
import time
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uvicorn
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from fixtures.catcher import FIXTURES_DIR, app

CATCHER_PORT = 4319
CATCHER_URL = f"http://127.0.0.1:{CATCHER_PORT}"


def start_catcher_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=CATCHER_PORT, log_level="error")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    return server


def generate_single_root(compressed: bool = True):
    name = "trace_single_root_gzip.pb" if compressed else "trace_single_root_raw.pb"
    comp = Compression.Gzip if compressed else Compression.NoCompression
    resource = Resource.create({"service.name": "gateway", "host.name": "gw-01"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{CATCHER_URL}/v1/traces",
        compression=comp,
        headers={"X-Fixture-Name": name},
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("fixtures-generator")

    with tracer.start_as_current_span("GET /health", kind=trace.SpanKind.SERVER) as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.route", "/health")
        span.set_attribute("http.status_code", 200)

    provider.force_flush()
    print(f"Generated: {name}")


def generate_error_span():
    name = "trace_error_span.pb"
    resource = Resource.create({"service.name": "payment-service"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{CATCHER_URL}/v1/traces",
        compression=Compression.NoCompression,
        headers={"X-Fixture-Name": name},
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("fixtures-generator")

    with tracer.start_as_current_span("chargeCard", kind=trace.SpanKind.SERVER) as span:
        span.set_attribute("http.method", "POST")
        span.set_attribute("http.route", "/charge")
        span.set_attribute("http.status_code", 504)
        span.set_attribute("payment.provider", "stripe")
        span.set_status(trace.Status(trace.StatusCode.ERROR, "Stripe gateway timeout after 1.9s"))

    provider.force_flush()
    print(f"Generated: {name}")


def generate_no_http_attrs():
    name = "trace_no_http_attrs.pb"
    resource = Resource.create({"service.name": "worker-service"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{CATCHER_URL}/v1/traces",
        compression=Compression.NoCompression,
        headers={"X-Fixture-Name": name},
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("fixtures-generator")

    with tracer.start_as_current_span("calculate_checksum", kind=trace.SpanKind.INTERNAL) as span:
        span.set_attribute("hash.algorithm", "sha256")
        span.set_attribute("bytes.processed", 1048576)
        span.set_attribute("cache.hit", False)

    provider.force_flush()
    print(f"Generated: {name}")


def generate_four_span_chain(compressed: bool = False):
    name = "trace_four_span_chain_gzip.pb" if compressed else "trace_four_span_chain.pb"
    comp = Compression.Gzip if compressed else Compression.NoCompression
    resource = Resource.create({"service.name": "gateway"})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(
        endpoint=f"{CATCHER_URL}/v1/traces",
        compression=comp,
        headers={"X-Fixture-Name": name},
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("chain-tracer")

    with tracer.start_as_current_span("POST /order", kind=trace.SpanKind.SERVER) as s1:
        s1.set_attribute("http.method", "POST")
        s1.set_attribute("http.route", "/order")
        s1.set_attribute("http.status_code", 200)

        with tracer.start_as_current_span("createOrder", kind=trace.SpanKind.INTERNAL) as s2:
            s2.set_attribute("order.id", "ord_9981")

            with tracer.start_as_current_span("INSERT orders", kind=trace.SpanKind.CLIENT) as s3:
                s3.set_attribute("db.system", "postgresql")
                s3.set_attribute("db.statement", "INSERT INTO orders (id, total) VALUES ($1, $2)")

            with tracer.start_as_current_span("chargeCard", kind=trace.SpanKind.CLIENT) as s4:
                s4.set_attribute("payment.provider", "stripe")
                s4.set_attribute("payment.amount", 49.99)

    provider.force_flush()
    print(f"Generated: {name}")


def generate_logs(with_trace: bool = True, compressed: bool = False):
    suffix = "_gzip.pb" if compressed else ".pb"
    prefix = "logs_with_trace_context" if with_trace else "logs_without_trace_context"
    name = f"{prefix}{suffix}"

    comp = Compression.Gzip if compressed else Compression.NoCompression
    resource = Resource.create({"service.name": "order-service"})

    trace_provider = TracerProvider(resource=resource)
    tracer = trace_provider.get_tracer("log-tracer")

    log_provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(
        endpoint=f"{CATCHER_URL}/v1/logs",
        compression=comp,
        headers={"X-Fixture-Name": name},
    )
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))

    logger = logging.getLogger(f"fixture-logger-{name}")
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(logger_provider=log_provider))

    if with_trace:
        with tracer.start_as_current_span("order_step") as span:
            logger.info("Order validation initiated")
            logger.warning("Retrying inventory reservation, attempt 1")
    else:
        logger.info("Service initialized without active request context")
        logger.warning("Heartbeat ping received")

    log_provider.force_flush()
    print(f"Generated: {name}")


def main():
    print("Starting catcher server...")
    server = start_catcher_server()
    print(f"Catcher server running at {CATCHER_URL}")

    print("\nGenerating OTLP wire fixtures...")
    generate_single_root(compressed=True)
    generate_single_root(compressed=False)
    generate_error_span()
    generate_no_http_attrs()
    generate_four_span_chain(compressed=False)
    generate_four_span_chain(compressed=True)
    generate_logs(with_trace=True, compressed=False)
    generate_logs(with_trace=True, compressed=True)
    generate_logs(with_trace=False, compressed=False)
    generate_logs(with_trace=False, compressed=True)

    time.sleep(0.5)
    server.should_exit = True

    print("\nGenerated files in fixtures/otlp/:")
    for f in sorted(FIXTURES_DIR.glob("*.pb")):
        print(f"  {f.name:<35} {f.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
