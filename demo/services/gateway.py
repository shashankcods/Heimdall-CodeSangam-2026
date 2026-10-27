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
"""Gateway ingress service for Heimdall demo (service.name: gateway, port: 8001)."""

import os
import sys
import uuid
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

SERVICE_NAME = "gateway"
DEFAULT_PORT = int(os.environ.get("GATEWAY_SERVICE_PORT", "8001"))
ORDER_SERVICE_URL = os.environ.get("ORDER_SERVICE_URL", "http://127.0.0.1:8002")
PAYMENT_SERVICE_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://127.0.0.1:8003")

tracer = trace.get_tracer("demo.gateway")
fault_state = FaultState()


class CreateOrderPayload(BaseModel):
    items: list[dict[str, Any]] = Field(
        default_factory=lambda: [{"name": "Observability Widget", "quantity": 1, "price": 49.99}],
        description="List of order items",
    )
    customer_id: str = Field("cust_123", description="Customer ID")
    amount: float = Field(49.99, gt=0.0, description="Total order amount")


def create_app(
    service_name: str = SERVICE_NAME,
    heimdall_url: str | None = None,
    order_url: str = ORDER_SERVICE_URL,
    payment_url: str = PAYMENT_SERVICE_URL,
    http_client: httpx.Client | None = None,
) -> tuple[FastAPI, Any, Any]:
    """Create and configure the gateway service FastAPI application."""
    tracer_provider, logger_provider, logger = init_telemetry(
        service_name=service_name,
        heimdall_url=heimdall_url or os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318"),
    )

    client = http_client or httpx.Client(timeout=10.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Gateway service started on port %s (target order: %s)", DEFAULT_PORT, order_url)
        yield
        logger.info("Gateway service shutting down; flushing telemetry...")
        if http_client is None:
            client.close()
        flush_telemetry(tracer_provider, logger_provider)

    app = FastAPI(title="Gateway Service", lifespan=lifespan)
    register_fault_routes(app, fault_state, service_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.post("/order")
    def handle_order(payload: CreateOrderPayload | None = None) -> dict[str, Any]:
        data = payload or CreateOrderPayload()
        order_id = f"ord_{uuid.uuid4().hex[:8]}"

        # 1. Apply configured gateway latency
        fault_state.apply_latency()

        # 2. Check for configured gateway failures
        should_fail, error_type = fault_state.should_fail()
        current_span = trace.get_current_span()

        if should_fail and error_type == "500":
            logger.error("Gateway ingress failure for order %s", order_id)
            current_span.set_status(trace.Status(trace.StatusCode.ERROR, "Gateway ingress error"))
            raise HTTPException(status_code=500, detail="Gateway ingress error")

        logger.info("Gateway received order request %s for customer %s ($%.2f)", order_id, data.customer_id, data.amount)

        # 3. Outgoing call to order service with W3C trace-context propagation
        with tracer.start_as_current_span("call order-service", kind=trace.SpanKind.CLIENT) as client_span:
            client_span.set_attribute("http.method", "POST")
            client_span.set_attribute("http.url", f"{order_url.rstrip('/')}/order")
            client_span.set_attribute("peer.service", "order")

            headers: dict[str, str] = {}
            inject_trace_context(headers)

            try:
                resp = client.post(
                    f"{order_url.rstrip('/')}/order",
                    json={
                        "order_id": order_id,
                        "items": data.items,
                        "customer_id": data.customer_id,
                        "amount": data.amount,
                    },
                    headers=headers,
                )
                client_span.set_attribute("http.status_code", resp.status_code)
            except Exception as conn_err:
                logger.error("Gateway failed to reach order service: %s", conn_err)
                client_span.set_status(trace.Status(trace.StatusCode.ERROR, str(conn_err)))
                client_span.record_exception(conn_err)
                return JSONResponse(
                    status_code=502,
                    content={
                        "status": "downstream_error",
                        "order_id": order_id,
                        "detail": f"Order service unreachable: {conn_err}",
                    },
                )

            # 4. Handle response from order service
            if resp.status_code == 200:
                logger.info("Gateway order %s completed successfully", order_id)
                return {
                    "status": "success",
                    "order_id": order_id,
                    "order": resp.json(),
                }
            elif resp.status_code == 402:
                # Business outcome: payment declined
                logger.warning("Gateway order %s declined by downstream payment", order_id)
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": "payment_declined",
                        "order_id": order_id,
                        "detail": resp.json(),
                    },
                )
            else:
                logger.error("Gateway order %s encountered downstream failure (HTTP %d)", order_id, resp.status_code)
                client_span.set_status(trace.Status(trace.StatusCode.ERROR, f"Downstream error {resp.status_code}"))
                current_span.set_status(trace.Status(trace.StatusCode.ERROR, f"Downstream error {resp.status_code}"))
                return JSONResponse(
                    status_code=502,
                    content={
                        "status": "downstream_error",
                        "order_id": order_id,
                        "detail": resp.json() if resp.headers.get("content-type") == "application/json" else resp.text,
                    },
                )

    @app.post("/fault/reset-all")
    def reset_all_faults() -> dict[str, Any]:
        """Reset fault settings across gateway, order, and payment services."""
        fault_state.reset()
        results: dict[str, Any] = {"gateway": "reset"}

        for name, url in [("order", order_url), ("payment", payment_url)]:
            try:
                r = client.post(f"{url.rstrip('/')}/fault/reset", timeout=2.0)
                results[name] = "reset" if r.status_code == 200 else f"HTTP {r.status_code}"
            except Exception as e:
                results[name] = f"failed ({e})"

        return {
            "status": "ok",
            "message": "Reset commands dispatched to all demo services",
            "results": results,
        }

    instrument_app(app)
    return app, tracer_provider, logger_provider


app, _tp, _lp = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=DEFAULT_PORT, log_level="info")
