# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn",
#     "httpx",
#     "pydantic",
#     "opentelemetry-sdk",
#     "opentelemetry-exporter-otlp-proto-http",
#     "opentelemetry-instrumentation-fastapi",
# ]
# ///
"""Payment service: handles charges with configurable latency and failure fault injection."""

import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import uvicorn
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel

from demo.services.common import setup_service_telemetry

SERVICE_NAME = "payment-service"
PORT = int(os.environ.get("PAYMENT_PORT", "8003"))

tracer, log, tracer_provider, log_provider = setup_service_telemetry(SERVICE_NAME)

app = FastAPI(title="Payment Service")


class FaultConfig(BaseModel):
    latency_ms: float = 0.0
    error_rate: float = 0.0
    error_status: int = 504
    error_message: str = "Stripe gateway timeout after 1.9s, retry 0"


fault_state = FaultConfig()


class ChargeRequest(BaseModel):
    order_id: str
    amount: float = 49.99
    customer_id: str = "cust_123"
    provider: str = "stripe"


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.get("/fault")
def get_fault():
    return {
        "latency_ms": fault_state.latency_ms,
        "error_rate": fault_state.error_rate,
        "error_status": fault_state.error_status,
        "error_message": fault_state.error_message,
        "fault_active": fault_state.latency_ms > 0 or fault_state.error_rate > 0,
    }


@app.post("/fault")
def set_fault(config: FaultConfig):
    fault_state.latency_ms = config.latency_ms
    fault_state.error_rate = config.error_rate
    fault_state.error_status = config.error_status
    fault_state.error_message = config.error_message
    log.warning(
        "Fault configuration updated: latency=%.1fms, error_rate=%.2f, status=%d",
        fault_state.latency_ms,
        fault_state.error_rate,
        fault_state.error_status,
    )
    return {"status": "updated", "fault": get_fault()}


@app.post("/fault/reset")
def reset_fault():
    fault_state.latency_ms = 0.0
    fault_state.error_rate = 0.0
    fault_state.error_status = 504
    fault_state.error_message = "Stripe gateway timeout after 1.9s, retry 0"
    log.info("Faults reset to normal")
    return {"status": "reset", "fault": get_fault()}


@app.post("/charge")
def charge(req: ChargeRequest):
    log.info("calling Stripe, amount=%.2f", req.amount)

    with tracer.start_as_current_span("chargeCard", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("payment.provider", req.provider)
        span.set_attribute("payment.amount", req.amount)
        span.set_attribute("payment.order_id", req.order_id)

        # Apply fault latency if set, or baseline latency (20ms)
        delay_sec = (fault_state.latency_ms / 1000.0) if fault_state.latency_ms > 0 else 0.02
        time.sleep(delay_sec)

        # Apply fault failure
        should_fail = (
            fault_state.error_rate > 0 and random.random() < fault_state.error_rate
        )
        if should_fail:
            span.set_status(trace.Status(trace.StatusCode.ERROR, fault_state.error_message))
            span.set_attribute("http.status_code", fault_state.error_status)
            log.error(fault_state.error_message)
            # Flush telemetry so error arrives immediately
            tracer_provider.force_flush()
            log_provider.force_flush()
            raise HTTPException(
                status_code=fault_state.error_status,
                detail=fault_state.error_message,
            )

        log.info("Stripe charge successful for order %s", req.order_id)
        span.set_attribute("payment.charge_id", f"ch_{req.order_id[:8]}")
        span.set_status(trace.Status(trace.StatusCode.OK))
        return {
            "status": "charged",
            "charge_id": f"ch_{req.order_id[:8]}",
            "amount": req.amount,
        }


FastAPIInstrumentor.instrument_app(app)


def start_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    start_server()
