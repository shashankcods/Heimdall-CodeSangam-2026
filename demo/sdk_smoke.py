# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "httpx",
#     "opentelemetry-sdk",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-instrumentation-fastapi",
# ]
# ///
"""Smoke test: send traces and logs from the real OpenTelemetry Python SDK to Heimdall.

Start Heimdall first (cd backend && uv run uvicorn heimdall.api.main:app --port 4318),
then:  uv run demo/sdk_smoke.py

Runs a tiny instrumented FastAPI service, sends it a few requests (every 3rd one
fails), flushes the SDK's gzip/protobuf exporters, and checks the API.
Logs are written inside spans, so the SDK stamps them with trace/span IDs.
Throwaway proof for steps 1g and 3a; the real demo services come in step 5.
"""

import logging
import os
import sys
import threading
import time

from typing import Any

import httpx

# Optional OTel / FastAPI server dependencies for running the demo service.
# Kept in a conditional import block so unit tests can import polling & verification
# utilities without needing the full OTel SDK installed in the test environment.
try:
    from fastapi import FastAPI, HTTPException
    import uvicorn
    from opentelemetry import trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    HAS_OTEL_SERVER = True
except ImportError:
    HAS_OTEL_SERVER = False


# Default configuration
HEIMDALL = os.environ.get("HEIMDALL_URL", "http://localhost:4318")
SERVICE = os.environ.get("HEIMDALL_SERVICE_NAME", "sdk-smoke-service")
PORT = int(os.environ.get("HEIMDALL_DEMO_PORT", "8001"))
REQUESTS = int(os.environ.get("HEIMDALL_SMOKE_REQUESTS", "6"))


# ---------------------------------------------------------------------------
# Custom exceptions for clear failure diagnostics
# ---------------------------------------------------------------------------

class SmokeTestError(Exception):
    """Base exception for smoke test failures."""


class BackendUnreachableError(SmokeTestError):
    """Raised when the Heimdall backend cannot be reached or is unhealthy."""


class DemoServiceStartupError(SmokeTestError):
    """Raised when the internal demo service fails to bind or start."""


class IngestionTimeoutError(SmokeTestError):
    """Raised when telemetry does not become queryable in Heimdall within timeout."""


# ---------------------------------------------------------------------------
# Bounded polling and verification utilities
# ---------------------------------------------------------------------------

def wait_for_backend_health(
    client: httpx.Client,
    heimdall_url: str,
    timeout: float = 5.0,
    poll_interval: float = 0.25,
) -> None:
    """Poll Heimdall /api/health until it responds with status: ok, or raise BackendUnreachableError."""
    deadline = time.monotonic() + timeout
    health_url = f"{heimdall_url.rstrip('/')}/api/health"
    last_err: Exception | str | None = None

    while time.monotonic() < deadline:
        try:
            r = client.get(health_url, timeout=2.0)
            if r.status_code == 200:
                try:
                    data = r.json()
                    if data.get("status") == "ok":
                        return
                    last_err = f"Unexpected JSON response: {data}"
                except Exception as json_err:
                    last_err = f"Failed to parse JSON: {json_err}"
            else:
                last_err = f"HTTP {r.status_code} ({r.text})"
        except Exception as conn_err:
            last_err = conn_err
        time.sleep(poll_interval)

    raise BackendUnreachableError(
        f"Backend is unreachable at {health_url} after {timeout:.1f}s. "
        f"Ensure Heimdall is running (e.g. 'cd backend && uv run uvicorn heimdall.api.main:app --port 4318'). "
        f"Last error: {last_err}"
    )


def wait_for_server_started(
    server: Any,
    port: int,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> None:
    """Poll until uvicorn server.started is True, or raise DemoServiceStartupError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return
        time.sleep(poll_interval)

    raise DemoServiceStartupError(
        f"Demo service on port {port} failed to start within {timeout:.1f}s. "
        f"Check if port {port} is already in use by another process."
    )


def poll_for_spans(
    client: httpx.Client,
    heimdall_url: str,
    service: str,
    min_spans: int = 1,
    min_traces: int = 1,
    run_start_ms: float | None = None,
    timeout: float = 10.0,
    poll_interval: float = 0.25,
) -> list[dict[str, Any]]:
    """Poll /api/spans until expected spans and traces for service are queryable.

    When run_start_ms is provided, only spans belonging to that run (start_ms >= run_start_ms)
    are accepted. Old/stale spans from earlier runs are strictly excluded and never returned
    as a fallback.
    """
    deadline = time.monotonic() + timeout
    spans_url = f"{heimdall_url.rstrip('/')}/api/spans"
    current_run_spans: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        try:
            r = client.get(spans_url, params={"limit": 1000}, timeout=2.0)
            if r.status_code == 200:
                raw_spans = r.json().get("spans", [])
                if run_start_ms is not None:
                    current_run_spans = [
                        s for s in raw_spans
                        if s.get("service") == service and s.get("start_ms", 0) >= run_start_ms
                    ]
                else:
                    current_run_spans = [
                        s for s in raw_spans
                        if s.get("service") == service
                    ]

                unique_traces = {
                    s.get("trace_id")
                    for s in current_run_spans
                    if s.get("trace_id")
                }

                if len(current_run_spans) >= min_spans and len(unique_traces) >= min_traces:
                    return current_run_spans
        except Exception:
            pass
        time.sleep(poll_interval)

    found_traces = len({s.get("trace_id") for s in current_run_spans if s.get("trace_id")})
    time_filter_note = f" (after run_start_ms={run_start_ms:.1f})" if run_start_ms is not None else ""
    raise IngestionTimeoutError(
        f"Telemetry did not become queryable at {spans_url} within {timeout:.1f}s. "
        f"Found {len(current_run_spans)} spans across {found_traces} traces for service '{service}'{time_filter_note} "
        f"(expected at least {min_spans} spans and {min_traces} traces). "
        f"Ingestion may have stalled or failed."
    )


def verify_smoke_checks(ours: list[dict[str, Any]], expected_requests: int) -> dict[str, bool]:
    """Evaluate standard smoke test assertions on retrieved spans."""
    return {
        "spans arrived": len(ours) > 0,
        "one trace per request": len({s["trace_id"] for s in ours}) == expected_requests,
        "roots have no parent": any(s["parent_span_id"] is None for s in ours),
        "children link to parents": any(bool(s["parent_span_id"]) for s in ours),
        "http_status promoted": any(s.get("http_status") == 200 for s in ours),
        "errors recorded": any(s.get("status_code") == "ERROR" for s in ours),
    }


def check_logs(
    client: httpx.Client,
    heimdall_url: str,
    service: str,
    expected_requests: int,
    timeout: float = 5.0,
    poll_interval: float = 0.25,
) -> tuple[str, dict[str, bool]]:
    """Check logs if /api/logs is implemented; otherwise return limitation message."""
    logs_url = f"{heimdall_url.rstrip('/')}/api/logs"
    try:
        r = client.get(logs_url, params={"service": service, "limit": 1000}, timeout=2.0)
    except Exception as e:
        return f"skipped (connection error to {logs_url}: {e})", {}

    if r.status_code == 404:
        return "skipped: GET /api/logs not implemented yet (Issue #5)", {}

    if r.status_code != 200:
        return f"failed (status {r.status_code}: {r.text})", {"logs query successful": False}

    deadline = time.monotonic() + timeout
    expected_total = expected_requests + expected_requests // 3 + 1
    logs: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        try:
            r = client.get(logs_url, params={"service": service, "limit": 1000}, timeout=2.0)
            if r.status_code == 200:
                logs = r.json().get("logs", [])
                if len(logs) >= expected_total:
                    break
        except Exception:
            pass
        time.sleep(poll_interval)

    log_checks = {
        "logs arrived": len(logs) == expected_total,
        "logs carry trace IDs": sum(l.get("trace_id") is not None for l in logs) == max(0, len(logs) - 1),
        "error logs recorded": sum(l.get("severity") == "ERROR" for l in logs) == expected_requests // 3,
    }
    return "completed", log_checks


# ---------------------------------------------------------------------------
# Demo service factory & orchestration
# ---------------------------------------------------------------------------

def create_demo_service(
    heimdall_url: str = HEIMDALL,
    service_name: str = SERVICE,
) -> tuple[Any, Any, Any, logging.Logger]:
    """Create and instrument the tiny demo service."""
    if not HAS_OTEL_SERVER:
        raise DemoServiceStartupError(
            "Demo service dependencies (fastapi, uvicorn, opentelemetry-sdk, etc.) are missing. "
            "Run the smoke test with 'uv run demo/sdk_smoke.py' so PEP 723 inline script dependencies are installed."
        )

    resource = Resource.create({"service.name": service_name})

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{heimdall_url.rstrip('/')}/v1/traces", compression=Compression.Gzip)
        )
    )
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("sdk-smoke")

    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{heimdall_url.rstrip('/')}/v1/logs", compression=Compression.Gzip)
        )
    )
    set_logger_provider(log_provider)
    demo_log = logging.getLogger(f"sdk-smoke-{service_name}")
    demo_log.setLevel(logging.INFO)
    demo_log.addHandler(LoggingHandler(logger_provider=log_provider))

    app = FastAPI()

    @app.get("/order/{n}")
    def order(n: int):
        demo_log.info("creating order %d", n)
        with tracer.start_as_current_span("INSERT orders", kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute("db.system", "postgresql")
            time.sleep(0.01)
        with tracer.start_as_current_span("chargeCard") as span:
            time.sleep(0.02)
            if n % 3 == 0:
                demo_log.error("card declined for order %d", n)
                span.set_status(trace.Status(trace.StatusCode.ERROR, "card declined"))
                raise HTTPException(status_code=502, detail="payment failed")
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(app)
    return app, provider, log_provider, demo_log


def run_smoke_test(
    heimdall_url: str = HEIMDALL,
    port: int = PORT,
    service_name: str = SERVICE,
    requests_count: int = REQUESTS,
    health_timeout: float = 5.0,
    startup_timeout: float = 5.0,
    telemetry_timeout: float = 10.0,
) -> int:
    """Execute the end-to-end smoke test workflow with bounded polling and error reporting."""
    print("--- Heimdall SDK Smoke Test ---")
    print(f"Target Heimdall URL : {heimdall_url}")
    print(f"Demo Service Name   : {service_name}")
    print(f"Demo Service Port   : {port}")
    print(f"Sample Requests     : {requests_count}\n")

    # 1. Pre-flight health check
    print(f"1. Checking Heimdall health at {heimdall_url}/api/health...")
    with httpx.Client() as client:
        wait_for_backend_health(client, heimdall_url, timeout=health_timeout)
    print("   [OK] Heimdall backend is healthy and accepting queries.\n")

    # 2. Start demo service
    print(f"2. Starting demo service on port {port}...")
    app, provider, log_provider, demo_log = create_demo_service(heimdall_url, service_name)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    wait_for_server_started(server, port, timeout=startup_timeout)
    print(f"   [OK] Demo service listening at http://127.0.0.1:{port}.\n")

    run_start_ms = (time.time() - 1.0) * 1000

    # 3. Generate traffic
    print(f"3. Sending {requests_count} sample requests (every 3rd request fails with 502)...")
    demo_log.info("smoke service started")
    with httpx.Client() as client:
        for n in range(1, requests_count + 1):
            try:
                client.get(f"http://127.0.0.1:{port}/order/{n}")
            except Exception as req_err:
                print(f"   (Warning: request to /order/{n} failed: {req_err})")
    print("   [OK] Requests sent.\n")

    # 4. Flush OTel exporters
    print("4. Flushing OpenTelemetry trace and log exporters...")
    provider.force_flush()
    log_provider.force_flush()
    server.should_exit = True
    print("   [OK] Telemetry flushed to Heimdall ingest endpoints.\n")

    # 5. Bounded polling for ingested telemetry
    print("5. Polling Heimdall for ingested spans...")
    with httpx.Client() as client:
        ours = poll_for_spans(
            client,
            heimdall_url,
            service_name,
            min_spans=requests_count,
            min_traces=requests_count,
            run_start_ms=run_start_ms,
            timeout=telemetry_timeout,
        )
    print(f"   [OK] {len(ours)} spans queryable in Heimdall.\n")

    # 6. Display sample spans
    print(f"Sample spans from {service_name}:")
    for s in sorted(ours, key=lambda s: s.get("start_ms", 0))[:8]:
        parent = (s.get("parent_span_id") or "-")[:8]
        print(f"  {s.get('name', ''):<28} parent={parent:<8} status={s.get('status_code', ''):<5} "
              f"http={s.get('http_status')}  {s.get('duration_ms', 0):.1f} ms")

    # 7. Evaluate span assertions
    checks = verify_smoke_checks(ours, requests_count)

    # 8. Check log query support
    with httpx.Client() as client:
        log_status, log_checks = check_logs(client, heimdall_url, service_name, requests_count)
    if log_checks:
        checks.update(log_checks)
    else:
        print(f"\nLog status: {log_status}")

    # 9. Report results
    print("\n--- Smoke Test Assertion Results ---")
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    all_passed = all(checks.values())
    print(f"\nFinal Result: {'SUCCESS' if all_passed else 'FAILURE'}")
    return 0 if all_passed else 1


def main() -> int:
    try:
        return run_smoke_test()
    except SmokeTestError as e:
        print(f"\n[FAIL] Smoke test aborted: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
