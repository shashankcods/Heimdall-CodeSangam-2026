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
"""Shared telemetry initialization, context propagation, and fault injection for demo services."""

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# OpenTelemetry API is always available (standard dependency)
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# Check for OpenTelemetry SDK & exporters (available when run via uv script or with OTel SDK installed)
try:
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

    HAS_OTEL_SDK = True
except ImportError:
    HAS_OTEL_SDK = False

DEFAULT_HEIMDALL_URL = os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318")
propagator = TraceContextTextMapPropagator()


# ---------------------------------------------------------------------------
# Telemetry Initialization & Context Propagation
# ---------------------------------------------------------------------------

def init_telemetry(
    service_name: str,
    heimdall_url: str = DEFAULT_HEIMDALL_URL,
) -> tuple[Any, Any, logging.Logger]:
    """Initialize OpenTelemetry tracer and logger providers for a demo service."""
    logger = logging.getLogger(f"demo.{service_name}")
    logger.setLevel(logging.INFO)

    if not HAS_OTEL_SDK:
        # Fall back to standard logger when SDK is not present (e.g. lightweight unit tests)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[%(levelname)s] [%(name)s] %(message)s"))
            logger.addHandler(handler)
        return None, None, logger

    resource = Resource.create({"service.name": service_name})

    # 1. Traces
    tracer_provider = TracerProvider(resource=resource)
    trace_endpoint = f"{heimdall_url.rstrip('/')}/v1/traces"
    span_exporter = OTLPSpanExporter(endpoint=trace_endpoint, compression=Compression.Gzip)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # 2. Logs
    logger_provider = LoggerProvider(resource=resource)
    log_endpoint = f"{heimdall_url.rstrip('/')}/v1/logs"
    log_exporter = OTLPLogExporter(endpoint=log_endpoint, compression=Compression.Gzip)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)

    # Clear existing handlers to prevent duplicate outputs
    logger.handlers.clear()
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))

    return tracer_provider, logger_provider, logger


def instrument_app(app: FastAPI) -> None:
    """Instrument a FastAPI application with OpenTelemetry if SDK is available."""
    if HAS_OTEL_SDK:
        FastAPIInstrumentor.instrument_app(app)


def flush_telemetry(tracer_provider: Any, logger_provider: Any) -> None:
    """Flush pending spans and logs to Heimdall."""
    if tracer_provider is not None and hasattr(tracer_provider, "force_flush"):
        try:
            tracer_provider.force_flush()
        except Exception:
            pass

    if logger_provider is not None and hasattr(logger_provider, "force_flush"):
        try:
            logger_provider.force_flush()
        except Exception:
            pass


def inject_trace_context(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Inject current W3C trace context into HTTP headers dictionary."""
    if headers is None:
        headers = {}
    propagator.inject(headers)
    return headers


def extract_trace_context(headers: dict[str, str]) -> Any:
    """Extract W3C trace context from HTTP headers dictionary."""
    return propagator.extract(headers)


# ---------------------------------------------------------------------------
# Fault Injection State & Controls
# ---------------------------------------------------------------------------

class LatencyFaultRequest(BaseModel):
    delay_ms: float = Field(..., ge=0.0, le=30000.0, description="Latency delay in ms (0-30000)")


class ErrorFaultRequest(BaseModel):
    error_type: str = Field(..., description="Failure mode: 'none', 'decline', or '500'")
    rate: float = Field(1.0, ge=0.0, le=1.0, description="Error probability (0.0 - 1.0)")


@dataclass
class FaultState:
    """Thread-safe runtime fault injection configuration."""
    latency_ms: float = 0.0
    error_type: str = "none"  # "none", "decline", "500"
    error_rate: float = 1.0

    def __post_init__(self):
        self._lock = threading.Lock()

    def set_latency(self, delay_ms: float) -> None:
        if delay_ms < 0.0 or delay_ms > 30000.0:
            raise ValueError(f"Latency delay must be between 0 and 30000 ms, got {delay_ms}")
        with self._lock:
            self.latency_ms = float(delay_ms)

    def set_error(self, error_type: str, rate: float = 1.0) -> None:
        if error_type not in ("none", "decline", "500"):
            raise ValueError(f"Invalid error_type: '{error_type}'. Allowed: 'none', 'decline', '500'")
        if rate < 0.0 or rate > 1.0:
            raise ValueError(f"Error rate must be between 0.0 and 1.0, got {rate}")
        with self._lock:
            self.error_type = error_type
            self.error_rate = float(rate)

    def reset(self) -> None:
        with self._lock:
            self.latency_ms = 0.0
            self.error_type = "none"
            self.error_rate = 1.0

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "latency_ms": self.latency_ms,
                "error_type": self.error_type,
                "error_rate": self.error_rate,
            }

    def apply_latency(self) -> None:
        with self._lock:
            delay = self.latency_ms
        if delay > 0.0:
            time.sleep(min(delay, 30000.0) / 1000.0)

    def should_fail(self) -> tuple[bool, str]:
        with self._lock:
            if self.error_type == "none" or self.error_rate <= 0.0:
                return False, "none"
            if self.error_rate >= 1.0 or random.random() < self.error_rate:
                return True, self.error_type
            return False, "none"


def register_fault_routes(app: FastAPI, fault_state: FaultState, service_name: str) -> None:
    """Mount standard runtime fault injection endpoints onto a FastAPI app."""

    @app.post("/fault/latency")
    def set_latency(body: LatencyFaultRequest) -> dict[str, Any]:
        try:
            fault_state.set_latency(body.delay_ms)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err))
        return {
            "status": "ok",
            "service": service_name,
            "fault": fault_state.get_status(),
        }

    @app.post("/fault/error")
    def set_error(body: ErrorFaultRequest) -> dict[str, Any]:
        try:
            fault_state.set_error(body.error_type, body.rate)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err))
        return {
            "status": "ok",
            "service": service_name,
            "fault": fault_state.get_status(),
        }

    @app.post("/fault/reset")
    def reset_faults() -> dict[str, Any]:
        fault_state.reset()
        return {
            "status": "ok",
            "service": service_name,
            "message": "All faults reset to default",
            "fault": fault_state.get_status(),
        }

    @app.get("/fault/status")
    def get_fault_status() -> dict[str, Any]:
        return {
            "service": service_name,
            "fault": fault_state.get_status(),
        }
