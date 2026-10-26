"""OTLP/HTTP protobuf -> normalized Span and LogRecord objects.

OTLP nests data as resource -> scope -> record, and service.name lives on the
resource, not on each record. This module flattens that into one object per
row, copying the service name down. Other front decoders (gRPC, OTLP JSON) can
be added later as long as they produce the same objects.
"""

import gzip
import json
import time
from typing import Any, Iterable

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as OtlpLogRecord
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

from heimdall.ingest.models import LogRecord, Span

# OTLP enum ints -> the readable strings we store (0 = UNSPECIFIED -> None).
SPAN_KINDS = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_CODES = {0: "UNSET", 1: "OK", 2: "ERROR"}

# OTLP severity numbers come in blocks of four: 1-4 TRACE, 5-8 DEBUG, 9-12 INFO,
# 13-16 WARN, 17-20 ERROR, 21-24 FATAL.
SEVERITY_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")
# Used only when severity_number is unset; maps common text spellings to our levels.
SEVERITY_TEXT_ALIASES = {"WARNING": "WARN", "CRITICAL": "FATAL", "ERR": "ERROR"}

# OTel's own fallback when a service doesn't set service.name.
UNKNOWN_SERVICE = "unknown_service"

# Older SDKs use http.status_code; current semantic conventions use
# http.response.status_code. Accept both, newest first.
HTTP_STATUS_KEYS = ("http.response.status_code", "http.status_code")


class InvalidPayload(ValueError):
    """The request body could not be decompressed or decoded. Maps to HTTP 400."""


def decompress(body: bytes, content_encoding: str | None) -> bytes:
    if content_encoding and content_encoding.strip().lower() == "gzip":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError) as e:
            raise InvalidPayload("body is not valid gzip") from e
    return body


def any_value_to_python(value: AnyValue) -> Any:
    kind = value.WhichOneof("value")
    if kind is None:
        return None
    if kind == "array_value":
        return [any_value_to_python(v) for v in value.array_value.values]
    if kind == "kvlist_value":
        return attributes_to_dict(value.kvlist_value.values)
    if kind == "bytes_value":
        return value.bytes_value.hex()
    return getattr(value, kind)  # string_value / bool_value / int_value / double_value


def attributes_to_dict(attributes: Iterable[KeyValue]) -> dict[str, Any]:
    return {kv.key: any_value_to_python(kv.value) for kv in attributes}


def _parse(message, body: bytes, content_encoding: str | None):
    try:
        message.ParseFromString(decompress(body, content_encoding))
    except DecodeError as e:
        raise InvalidPayload(f"body is not a valid {type(message).__name__}") from e
    return message


def _service_name(resource: Resource) -> str:
    resource_attrs = attributes_to_dict(resource.attributes)
    return str(resource_attrs.get("service.name") or UNKNOWN_SERVICE)


def decode_traces(body: bytes, content_encoding: str | None = None) -> list[Span]:
    request = _parse(ExportTraceServiceRequest(), body, content_encoding)
    spans = []
    for resource_spans in request.resource_spans:
        service = _service_name(resource_spans.resource)
        for scope_spans in resource_spans.scope_spans:
            for otlp_span in scope_spans.spans:
                spans.append(_to_span(otlp_span, service))
    return spans


def _to_span(s: OtlpSpan, service: str) -> Span:
    attrs = attributes_to_dict(s.attributes)
    return Span(
        trace_id=s.trace_id.hex(),
        span_id=s.span_id.hex(),
        parent_span_id=s.parent_span_id.hex() or None,  # b"" -> "" -> None for roots
        service=service,
        name=s.name,
        kind=SPAN_KINDS.get(s.kind),
        start_time=s.start_time_unix_nano,
        end_time=s.end_time_unix_nano,
        duration_ns=s.end_time_unix_nano - s.start_time_unix_nano,
        status_code=STATUS_CODES.get(s.status.code, "UNSET"),
        status_msg=s.status.message or None,
        http_status=_first_int(attrs, HTTP_STATUS_KEYS),
        route=attrs.get("http.route"),
        attributes=attrs,
    )


def decode_logs(body: bytes, content_encoding: str | None = None) -> list[LogRecord]:
    request = _parse(ExportLogsServiceRequest(), body, content_encoding)
    logs = []
    for resource_logs in request.resource_logs:
        service = _service_name(resource_logs.resource)
        for scope_logs in resource_logs.scope_logs:
            for otlp_log in scope_logs.log_records:
                logs.append(_to_log_record(otlp_log, service))
    return logs


def _to_log_record(r: OtlpLogRecord, service: str) -> LogRecord:
    return LogRecord(
        # Event time if the SDK set it, else when the SDK observed the log,
        # else now (the column is NOT NULL).
        timestamp=r.time_unix_nano or r.observed_time_unix_nano or time.time_ns(),
        service=service,
        severity=_severity(r.severity_number, r.severity_text),
        body=_body_text(r.body),
        trace_id=r.trace_id.hex() or None,  # empty when logged outside a request
        span_id=r.span_id.hex() or None,
        attributes=attributes_to_dict(r.attributes),
    )


def _severity(number: int, text: str) -> str | None:
    if 1 <= number <= 24:
        return SEVERITY_LEVELS[(number - 1) // 4]
    text = text.strip().upper()
    if not text:
        return None
    return SEVERITY_TEXT_ALIASES.get(text, text if text in SEVERITY_LEVELS else None)


def _body_text(body: AnyValue) -> str | None:
    """Log bodies are usually strings but may be structured; store those as JSON text."""
    value = any_value_to_python(body)
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


def _first_int(attrs: dict[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        if key in attrs:
            try:
                return int(attrs[key])
            except (TypeError, ValueError):
                return None
    return None
