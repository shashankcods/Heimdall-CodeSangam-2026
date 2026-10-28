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
"""Order service: handles order creation, database persistence, and payment orchestration."""

import os
import sys
import time
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

SERVICE_NAME = "order-service"
PORT = int(os.environ.get("ORDER_PORT", "8002"))
PAYMENT_URL = os.environ.get("PAYMENT_URL", "http://127.0.0.1:8003")
INVENTORY_URL = os.environ.get("INVENTORY_URL", "http://127.0.0.1:8004")

tracer, log, tracer_provider, log_provider = setup_service_telemetry(SERVICE_NAME)

http_client = httpx.Client(timeout=10.0)

app = FastAPI(title="Order Service")


class OrderItem(BaseModel):
    name: str = "item-1"
    quantity: int = 1
    price: float = 49.99


class CreateOrderRequest(BaseModel):
    order_id: str
    customer_id: str = "cust_123"
    items: List[OrderItem] = [OrderItem()]
    total: float = 49.99


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE_NAME}


@app.post("/orders")
def create_order(req: CreateOrderRequest):
    log.info("processing order %s for customer %s", req.order_id, req.customer_id)

    # 1. Simulate database write: INSERT orders
    with tracer.start_as_current_span("INSERT orders", kind=trace.SpanKind.CLIENT) as db_span:
        db_span.set_attribute("db.system", "postgresql")
        db_span.set_attribute(
            "db.statement", "INSERT INTO orders (id, customer_id, total) VALUES ($1, $2, $3)"
        )
        time.sleep(0.015)
        log.info("order %s persisted to database", req.order_id)

    # 2. Call Node.js inventory-service to reserve stock (cross-language distributed trace)
    inv_headers = inject_trace_context({"Content-Type": "application/json"})
    item_id = req.items[0].name if req.items else "item-101"
    quantity = req.items[0].quantity if req.items else 1
    inv_payload = {
        "order_id": req.order_id,
        "item_id": item_id,
        "quantity": quantity,
    }

    try:
        inv_resp = http_client.post(f"{INVENTORY_URL}/reserve", json=inv_payload, headers=inv_headers)
        if inv_resp.status_code != 200:
            error_msg = f"inventory reservation failed: upstream returned {inv_resp.status_code}"
            log.error("inventory reservation failed for order %s: %s", req.order_id, inv_resp.text)
            current_span = trace.get_current_span()
            current_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
            current_span.set_attribute("http.status_code", inv_resp.status_code)
            tracer_provider.force_flush()
            log_provider.force_flush()
            raise HTTPException(status_code=inv_resp.status_code, detail=error_msg)
        inv_data = inv_resp.json()
        log.info("inventory reserved for order %s: %s", req.order_id, inv_data.get("reservation_id"))
    except httpx.RequestError as exc:
        log.error("inventory service connection failed for order %s: %s", req.order_id, str(exc))
        current_span = trace.get_current_span()
        current_span.set_status(trace.Status(trace.StatusCode.ERROR, f"inventory unreachable: {exc}"))
        tracer_provider.force_flush()
        log_provider.force_flush()
        raise HTTPException(status_code=502, detail="inventory service unreachable")

    # 3. Call payment-service to charge the order with context propagation
    headers = inject_trace_context({"Content-Type": "application/json"})
    payment_payload = {
        "order_id": req.order_id,
        "amount": req.total,
        "customer_id": req.customer_id,
        "provider": "stripe",
    }

    try:
        resp = http_client.post(f"{PAYMENT_URL}/charge", json=payment_payload, headers=headers)
    except Exception as exc:
        log.error("payment service connection failed for order %s: %s", req.order_id, str(exc))
        current_span = trace.get_current_span()
        current_span.set_status(trace.Status(trace.StatusCode.ERROR, f"payment connection failed: {exc}"))
        tracer_provider.force_flush()
        log_provider.force_flush()
        raise HTTPException(status_code=502, detail="payment service unreachable")

    if resp.status_code != 200:
        error_msg = f"payment failed: upstream returned {resp.status_code}"
        log.error("payment failed for order %s: upstream returned %d", req.order_id, resp.status_code)
        current_span = trace.get_current_span()
        current_span.set_status(trace.Status(trace.StatusCode.ERROR, error_msg))
        current_span.set_attribute("http.status_code", 500)
        tracer_provider.force_flush()
        log_provider.force_flush()
        raise HTTPException(status_code=500, detail=error_msg)

    log.info("payment response received for order %s: confirmed", req.order_id)
    return {
        "order_id": req.order_id,
        "status": "confirmed",
        "inventory": inv_data,
        "payment": resp.json(),
    }


FastAPIInstrumentor.instrument_app(app)


def start_server():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    start_server()
