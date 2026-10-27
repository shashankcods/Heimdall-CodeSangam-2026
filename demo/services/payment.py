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
"""Payment processing service for Heimdall demo (service.name: payment, port: 8003)."""

import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

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
        instrument_app,
        register_fault_routes,
    )
except ImportError:
    from common import (
        FaultState,
        flush_telemetry,
        init_telemetry,
        instrument_app,
        register_fault_routes,
    )

SERVICE_NAME = "payment"
DEFAULT_PORT = int(os.environ.get("PAYMENT_SERVICE_PORT", "8003"))
tracer = trace.get_tracer("demo.payment")
fault_state = FaultState()


class PaymentRequest(BaseModel):
    order_id: str = Field(..., description="Order identifier")
    amount: float = Field(..., gt=0.0, description="Payment amount in dollars")
    customer_id: str | None = Field(None, description="Optional customer identifier")


def create_app(
    service_name: str = SERVICE_NAME,
    heimdall_url: str | None = None,
) -> tuple[FastAPI, Any, Any]:
    """Create and configure the payment service FastAPI application."""
    tracer_provider, logger_provider, logger = init_telemetry(
        service_name=service_name,
        heimdall_url=heimdall_url or os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Payment service started on port %s", DEFAULT_PORT)
        yield
        logger.info("Payment service shutting down; flushing telemetry...")
        flush_telemetry(tracer_provider, logger_provider)

    app = FastAPI(title="Payment Service", lifespan=lifespan)
    register_fault_routes(app, fault_state, service_name)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": service_name}

    @app.post("/pay")
    def process_payment(payload: PaymentRequest) -> dict[str, Any]:
        # 1. Simulate configured latency
        fault_state.apply_latency()

        # 2. Check for configured failures
        should_fail, error_type = fault_state.should_fail()
        current_span = trace.get_current_span()

        if should_fail:
            if error_type == "500":
                # Unexpected infrastructure failure
                logger.error("Payment gateway internal failure for order %s", payload.order_id)
                current_span.set_status(trace.Status(trace.StatusCode.ERROR, "Downstream payment gateway error"))
                current_span.set_attribute("error.type", "PaymentGatewayTimeout")
                raise HTTPException(status_code=500, detail="Downstream payment gateway error")

            elif error_type == "decline":
                # Expected business failure (card declined)
                logger.warning("Card declined for order %s: insufficient funds", payload.order_id)
                current_span.set_attribute("payment.status", "declined")
                current_span.set_attribute("payment.decline_reason", "insufficient_funds")
                # Return HTTP 402 with structured decline body without flagging an unhandled span exception
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": "declined",
                        "order_id": payload.order_id,
                        "reason": "insufficient_funds",
                    },
                )

        # 3. Successful payment authorization
        with tracer.start_as_current_span("authorize payment", kind=trace.SpanKind.INTERNAL) as span:
            span.set_attribute("payment.amount", payload.amount)
            span.set_attribute("payment.currency", "USD")
            span.set_attribute("payment.provider", "mock_stripe")
            tx_id = f"tx_{uuid.uuid4().hex[:12]}"
            span.set_attribute("payment.transaction_id", tx_id)
            logger.info("Payment authorized for order %s: $%.2f (tx: %s)", payload.order_id, payload.amount, tx_id)

        return {
            "status": "authorized",
            "order_id": payload.order_id,
            "transaction_id": tx_id,
            "amount": payload.amount,
        }

    instrument_app(app)
    return app, tracer_provider, logger_provider


app, _tp, _lp = create_app()

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("DEMO_SERVICE_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=DEFAULT_PORT, log_level="info")
