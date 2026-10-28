"""Common telemetry setup and utilities for demo microservices."""

import logging
import os
from typing import Tuple

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

HEIMDALL_URL = os.environ.get("HEIMDALL_URL", "http://127.0.0.1:4318")


def setup_service_telemetry(
    service_name: str, heimdall_url: str = HEIMDALL_URL
) -> Tuple[trace.Tracer, logging.Logger, TracerProvider, LoggerProvider]:
    """Configure OTel TracerProvider and LoggerProvider exporting to Heimdall via gzip/protobuf."""
    resource = Resource.create({"service.name": service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{heimdall_url}/v1/traces", compression=Compression.Gzip
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)
    tracer = trace.get_tracer(service_name)

    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(
                endpoint=f"{heimdall_url}/v1/logs", compression=Compression.Gzip
            )
        )
    )
    set_logger_provider(log_provider)

    logger = logging.getLogger(service_name)
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(logger_provider=log_provider))

    return tracer, logger, tracer_provider, log_provider


def inject_trace_context(headers: dict) -> dict:
    """Inject W3C traceparent headers from the current active span into an outgoing HTTP headers dict."""
    TraceContextTextMapPropagator().inject(headers)
    return headers
