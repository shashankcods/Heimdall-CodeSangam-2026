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
"""Order processing service for Heimdall demo (service.name: order, port: 8002)."""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from opentelemetry import trace

# Support running directly or as a module
try:
    from demo.services.common import (
        FaultState,
        flush_telemetry,
        init_telemetry,
        inject_trace_context,
        instrument_app,
        register_fault_routes,
    )
except ImportError:
    from common import (
        FaultState,
        flush_telemetry,
        init_telemetry,
        inject_trace_context,
        instrument_app,
        register_fault_routes,
    )

SERVICE_NAME = "order"
DEFAULT_PORT = int(os.environ.get("ORDER_SERVICE_PORT", "8002"))
PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://127.0.0.1:8003")

tracer = trace.get_tracer("demo.order")
fault_state = FaultState()


class OrderItem(BaseModel):
    name: str = Field(..., description="Item name")
    quantity: int = Field(1, ge=1, description="Quantity")
    price: float = Field(..., ge=0.0, description="Price per unit")


class OrderRequest(BaseModel):
    order_id: str = Field(..., description="Order identifier")
    items: list[OrderItem] = Field(default_factory=list, description="Order line items")
    customer_id: str = Field("cust_default", description="Customer identifier")
    amount: float = Field(..., gt=0.0, description="Total order amount")


def create_app(
    service_name: str = SERVICE_NAME,
    heimdall_url: str | None = None,
    payment_url: str = PAYMENT_SERVICE_URL,
    http_client: httpx.Client | None = None,
) -> tuple[FastAPI, Any, Any]:
    """Create and configure the order service FastAPI application."""
    tracer_provider, logger_provider, logger = init_telemetry(
        service_name=service_name,
        heimdall_url=heimdall_url or os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318"),
    )

    client = http_client or httpx.Client(timeout=10.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Order service started on port %s (target payment: %s)", DEFAULT_PORT, payment_url)
        yield
        logger.info("Order service shutting down; flushing telemetry...")
        if http_client is None:
            client.close()
        flush_telemetry(tracer_provider, logger_provider)

    app = FastAPI(title="Order Service", lifespan=lifespan)
    register_fault_routes(app, fault_state, service_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.post("/order")
    def process_order(payload: OrderRequest) -> dict[str, Any]:
        # 1. Apply configured order service latency
        fault_state.apply_latency()

        # 2. Check for configured order service failures
        should_fail, error_type = fault_state.should_fail()
        current_span = trace.get_current_span()

        if should_fail and error_type == "500":
            logger.error("Order processing internal crash for order %s", payload.order_id)
            current_span.set_status(trace.Status(trace.StatusCode.ERROR, "Order service internal crash"))
            current_span.set_attribute("error.type", "OrderProcessingCrash")
            raise HTTPException(status_code=500, detail="Order service internal crash")

        # 3. Internal validation span
        with tracer.start_as_current_span("validate order", kind=trace.SpanKind.INTERNAL) as val_span:
            val_span.set_attribute("order.id", payload.order_id)
            val_span.set_attribute("order.item_count", len(payload.items))
            val_span.set_attribute("order.amount", payload.amount)
            val_span.set_attribute("order.customer_id", payload.customer_id)
            logger.info("Order %s validated with %d items ($%.2f)", payload.order_id, len(payload.items), payload.amount)

        # 4. Outgoing call to payment service with W3C context propagation
        with tracer.start_as_current_span("call payment-service", kind=trace.SpanKind.CLIENT) as client_span:
            client_span.set_attribute("http.method", "POST")
            client_span.set_attribute("http.url", f"{payment_url.rstrip('/')}/pay")
            client_span.set_attribute("peer.service", "payment")

            headers: dict[str, str] = {}
            inject_trace_context(headers)

            try:
                resp = client.post(
                    f"{payment_url.rstrip('/')}/pay",
                    json={
                        "order_id": payload.order_id,
                        "amount": payload.amount,
                        "customer_id": payload.customer_id,
                    },
                    headers=headers,
                )
                client_span.set_attribute("http.status_code", resp.status_code)
            except Exception as conn_err:
                logger.error("Failed to connect to payment service: %s", conn_err)
                client_span.set_status(trace.Status(trace.StatusCode.ERROR, str(conn_err)))
                client_span.record_exception(conn_err)
                return JSONResponse(
                    status_code=502,
                    content={
                        "status": "downstream_error",
                        "order_id": payload.order_id,
                        "detail": f"Payment service unreachable: {conn_err}",
                    },
                )

            # 5. Handle payment response
            if resp.status_code == 200:
                payment_data = resp.json()
                logger.info("Order %s payment approved: tx=%s", payload.order_id, payment_data.get("transaction_id"))
                return {
                    "status": "created",
                    "order_id": payload.order_id,
                    "payment": payment_data,
                }
            elif resp.status_code == 402:
                # Business outcome: payment declined
                logger.warning("Order %s payment declined by payment service", payload.order_id)
                current_span.set_attribute("order.status", "payment_declined")
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": "payment_declined",
                        "order_id": payload.order_id,
                        "detail": resp.json(),
                    },
                )
            else:
                # Infrastructure error from payment service
                logger.error("Payment service failed with HTTP %d: %s", resp.status_code, resp.text)
                client_span.set_status(trace.Status(trace.StatusCode.ERROR, f"Payment error {resp.status_code}"))
                current_span.set_status(trace.Status(trace.StatusCode.ERROR, f"Downstream payment error {resp.status_code}"))
                return JSONResponse(
                    status_code=502,
                    content={
                        "status": "downstream_error",
                        "order_id": payload.order_id,
                        "detail": f"Downstream payment error HTTP {resp.status_code}",
                    },
                )

    instrument_app(app)
    return app, tracer_provider, logger_provider


app, _tp, _lp = create_app()

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("DEMO_SERVICE_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=DEFAULT_PORT, log_level="info")
