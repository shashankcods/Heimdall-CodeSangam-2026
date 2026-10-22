"""OTLP/HTTP protobuf -> normalized Span objects.

OTLP nests data as resource -> scope -> span, and service.name lives on the
resource, not on each span. This module flattens that into one Span per row,
copying the service name down. Other front decoders (gRPC, OTLP JSON) can be
added later as long as they produce the same Span objects.
"""

import gzip
from typing import Any, Iterable

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as OtlpSpan

from heimdall.ingest.models import Span

# OTLP enum ints -> the readable strings we store (0 = UNSPECIFIED -> None).
SPAN_KINDS = {1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS_CODES = {0: "UNSET", 1: "OK", 2: "ERROR"}

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


def decode_traces(body: bytes, content_encoding: str | None = None) -> list[Span]:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(decompress(body, content_encoding))
    except DecodeError as e:
        raise InvalidPayload("body is not a valid ExportTraceServiceRequest") from e

    spans = []
    for resource_spans in request.resource_spans:
        resource_attrs = attributes_to_dict(resource_spans.resource.attributes)
        service = str(resource_attrs.get("service.name") or UNKNOWN_SERVICE)
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


def _first_int(attrs: dict[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        if key in attrs:
            try:
                return int(attrs[key])
            except (TypeError, ValueError):
                return None
    return None
