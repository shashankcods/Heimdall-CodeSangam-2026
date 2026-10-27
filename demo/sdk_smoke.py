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

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
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

HEIMDALL = os.environ.get("HEIMDALL_URL", "http://localhost:4318")
SERVICE = "sdk-smoke-service"
PORT = int(os.environ.get("PORT", "8009"))
REQUESTS = 6
resource = Resource.create({"service.name": SERVICE})

provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(
    OTLPSpanExporter(endpoint=f"{HEIMDALL}/v1/traces", compression=Compression.Gzip)
))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sdk-smoke")

# Route standard-library logging through the OTel SDK to /v1/logs.
log_provider = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(BatchLogRecordProcessor(
    OTLPLogExporter(endpoint=f"{HEIMDALL}/v1/logs", compression=Compression.Gzip)
))
set_logger_provider(log_provider)
log = logging.getLogger("sdk-smoke")
log.setLevel(logging.INFO)
log.addHandler(LoggingHandler(logger_provider=log_provider))

app = FastAPI()


@app.get("/order/{n}")
def order(n: int):
    log.info("creating order %d", n)
    with tracer.start_as_current_span("INSERT orders", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("db.system", "postgresql")
        time.sleep(0.01)
    with tracer.start_as_current_span("chargeCard") as span:
        time.sleep(0.02)
        if n % 3 == 0:
            log.error("card declined for order %d", n)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "card declined"))
            raise HTTPException(status_code=502, detail="payment failed")
    return {"ok": True}


FastAPIInstrumentor.instrument_app(app)


def check_heimdall_health() -> bool:
    try:
        r = httpx.get(f"{HEIMDALL}/api/health", timeout=2.0)
        return r.status_code == 200 and r.json().get("status") == "ok"
    except Exception:
        return False


def main() -> int:
    print(f"Connecting to Heimdall backend at {HEIMDALL}...")
    if not check_heimdall_health():
        print(f"Error: Heimdall backend is not reachable at {HEIMDALL}.")
        print("Please start Heimdall first:")
        print("  cd backend && uv run uvicorn heimdall.api.main:app --port 4318")
        return 1

    server = uvicorn.Server(uvicorn.Config(app, port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    log.info("smoke service started")  # outside any request: no trace ID
    for n in range(1, REQUESTS + 1):
        httpx.get(f"http://127.0.0.1:{PORT}/order/{n}")
    provider.force_flush()  # export now instead of waiting for the SDK's 5 s timer
    log_provider.force_flush()
    server.should_exit = True

    # Poll until Heimdall flushes spans to DuckDB (1s interval or 500 records)
    print("Waiting for Heimdall to batch-write telemetry to DuckDB...")
    deadline = time.time() + 5.0
    ours = []
    while time.time() < deadline:
        time.sleep(0.3)
        try:
            r = httpx.get(f"{HEIMDALL}/api/spans", params={"limit": 1000}, timeout=2.0)
            if r.status_code == 200:
                spans = r.json().get("spans", [])
                ours = [s for s in spans if s["service"] == SERVICE]
                # Each request creates: root handler span + INSERT orders + chargeCard = 3 spans
                if len(ours) >= REQUESTS * 2:
                    break
        except Exception:
            pass

    print(f"{len(ours)} spans from {SERVICE} in Heimdall")
    for s in sorted(ours, key=lambda s: s["start_ms"])[:8]:
        parent = (s["parent_span_id"] or "-")[:8]
        print(f"  {s['name']:<28} parent={parent:<8} status={s['status_code']:<5} "
              f"http={s['http_status']}  {s['duration_ms']:.1f} ms")

    checks = {
        "spans arrived": len(ours) > 0,
        "one trace per request": len({s["trace_id"] for s in ours}) == REQUESTS,
        "roots have no parent": any(s["parent_span_id"] is None for s in ours),
        "children link to parents": any(s["parent_span_id"] for s in ours),
        "http_status promoted": any(s["http_status"] == 200 for s in ours),
        "errors recorded": any(s["status_code"] == "ERROR" for s in ours),
    }

    # Logs: verified using GET /api/logs
    logs = []
    logs_supported = True
    deadline = time.time() + 4.0
    while time.time() < deadline:
        try:
            r = httpx.get(f"{HEIMDALL}/api/logs", params={"service": SERVICE, "limit": 1000}, timeout=2.0)
            if r.status_code == 404:
                logs_supported = False
                break
            if r.status_code == 200:
                logs = r.json().get("logs", [])
                if len(logs) >= REQUESTS + REQUESTS // 3 + 1:
                    break
        except Exception:
            pass
        time.sleep(0.3)

    if not logs_supported:
        print("  (log checks skipped: GET /api/logs not built yet)")
    else:
        checks["logs arrived"] = len(logs) == REQUESTS + REQUESTS // 3 + 1
        checks["logs carry trace IDs"] = sum(l["trace_id"] is not None for l in logs) == len(logs) - 1
        checks["error logs recorded"] = sum(l["severity"] == "ERROR" for l in logs) == REQUESTS // 3

    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
