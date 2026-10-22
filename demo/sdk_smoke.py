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
"""Smoke test: send traces from the real OpenTelemetry Python SDK to Heimdall.

Start Heimdall first (cd backend && uv run uvicorn heimdall.api.main:app --port 4318),
then:  uv run demo/sdk_smoke.py

Runs a tiny instrumented FastAPI service, sends it a few requests (every 3rd one
fails), flushes the SDK's gzip/protobuf exporter, and checks /api/spans.
Throwaway proof for step 1g; the real demo services come in step 5.
"""

import os
import sys
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

HEIMDALL = os.environ.get("HEIMDALL_URL", "http://localhost:4318")
SERVICE = "sdk-smoke-service"
PORT = 8001
REQUESTS = 6

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE}))
provider.add_span_processor(BatchSpanProcessor(
    OTLPSpanExporter(endpoint=f"{HEIMDALL}/v1/traces", compression=Compression.Gzip)
))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sdk-smoke")

app = FastAPI()


@app.get("/order/{n}")
def order(n: int):
    with tracer.start_as_current_span("INSERT orders", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("db.system", "postgresql")
        time.sleep(0.01)
    with tracer.start_as_current_span("chargeCard") as span:
        time.sleep(0.02)
        if n % 3 == 0:
            span.set_status(trace.Status(trace.StatusCode.ERROR, "card declined"))
            raise HTTPException(status_code=502, detail="payment failed")
    return {"ok": True}


FastAPIInstrumentor.instrument_app(app)


def main() -> int:
    server = uvicorn.Server(uvicorn.Config(app, port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    for n in range(1, REQUESTS + 1):
        httpx.get(f"http://127.0.0.1:{PORT}/order/{n}")
    provider.force_flush()  # export now instead of waiting for the SDK's 5 s timer
    server.should_exit = True

    time.sleep(1.5)  # let Heimdall's writer flush (1 s interval)
    spans = httpx.get(f"{HEIMDALL}/api/spans", params={"limit": 1000}).json()["spans"]
    ours = [s for s in spans if s["service"] == SERVICE]

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
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
