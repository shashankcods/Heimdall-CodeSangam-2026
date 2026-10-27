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
"""Gateway service: public entry point for demo client requests."""

import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from pydantic import BaseModel

from demo.services.common import inject_trace_context, setup_service_telemetry

SERVICE_NAME = "gateway"
PORT = int(os.environ.get("GATEWAY_PORT", "8001"))
ORDER_URL = os.environ.get("ORDER_URL", "http://localhost:8002")

tracer, log, tracer_provider, log_provider = setup_service_telemetry(SERVICE_NAME)

app = FastAPI(title="Gateway Service")


class Item(BaseModel):
    name: str = "widget"
    quantity: int = 1
    price: float = 49.99


class OrderPayload(BaseModel):
    customer_id: str = "cust_123"
    items: List[Item] = [Item()]
    total: float = 49.99


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/order")
def create_order(payload: Optional[OrderPayload] = None):
    order_id = f"ord_{uuid.uuid4().hex[:8]}"
    total = payload.total if payload else 49.99
    customer_id = payload.customer_id if payload else "cust_123"

    log.info("received order request %s for customer %s", order_id, customer_id)

    headers = inject_trace_context({"Content-Type": "application/json"})
    order_req = {
        "order_id": order_id,
        "customer_id": customer_id,
        "items": [item.model_dump() for item in payload.items] if payload else [{"name": "widget", "quantity": 1, "price": 49.99}],
        "total": total,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{ORDER_URL}/orders", json=order_req, headers=headers)
    except Exception as exc:
        log.error("order service connection failed for %s: %s", order_id, str(exc))
        current_span = trace.get_current_span()
        current_span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        tracer_provider.force_flush()
        log_provider.force_flush()
        raise HTTPException(status_code=502, detail="order service unreachable")

    if resp.status_code != 200:
        log.error("order processing failed for %s: status %d", order_id, resp.status_code)
        current_span = trace.get_current_span()
        current_span.set_status(trace.Status(trace.StatusCode.ERROR, f"order failed: {resp.text}"))
        current_span.set_attribute("http.status_code", resp.status_code)
        tracer_provider.force_flush()
        log_provider.force_flush()
        raise HTTPException(status_code=resp.status_code, detail="order failed")

    log.info("order %s completed successfully", order_id)
    return resp.json()


@app.get("/order/{n}")
def create_order_get(n: int):
    """GET convenience route for quick manual or browser testing."""
    return create_order(OrderPayload(customer_id=f"cust_{n}", total=49.99 + n))


FastAPIInstrumentor.instrument_app(app)


def start_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    start_server()
