import gzip

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span as OtlpSpan, Status

from heimdall.ingest.decoder import InvalidPayload, decode_traces
from heimdall.store.schema import connect

TRACE_ID = bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736")
ROOT_ID = bytes.fromhex("00f067aa0ba902b7")
CHILD_ID = bytes.fromhex("b7ad6b7169203331")


def kv(key, value):
    if isinstance(value, bool):
        any_value = AnyValue(bool_value=value)
    elif isinstance(value, int):
        any_value = AnyValue(int_value=value)
    elif isinstance(value, float):
        any_value = AnyValue(double_value=value)
    else:
        any_value = AnyValue(string_value=value)
    return KeyValue(key=key, value=any_value)


def otlp_span(span_id=ROOT_ID, parent=b"", name="POST /order", attrs=(), **kwargs):
    return OtlpSpan(
        trace_id=TRACE_ID,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        kind=kwargs.get("kind", OtlpSpan.SPAN_KIND_SERVER),
        start_time_unix_nano=kwargs.get("start", 1_000),
        end_time_unix_nano=kwargs.get("end", 3_500),
        status=kwargs.get("status", Status()),
        attributes=list(attrs),
    )


def request_bytes(spans_by_service):
    """{service_name or None: [otlp spans]} -> serialized ExportTraceServiceRequest."""
    request = ExportTraceServiceRequest()
    for service, spans in spans_by_service.items():
        resource = Resource(attributes=[kv("service.name", service)] if service else [])
        request.resource_spans.append(
            ResourceSpans(resource=resource, scope_spans=[ScopeSpans(spans=spans)])
        )
    return request.SerializeToString()


def test_decodes_root_span_fields():
    body = request_bytes({"gateway": [otlp_span(
        status=Status(code=Status.STATUS_CODE_ERROR, message="upstream 500"),
    )]})
    [span] = decode_traces(body)
    assert span.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span.span_id == "00f067aa0ba902b7"
    assert span.parent_span_id is None
    assert span.service == "gateway"
    assert span.name == "POST /order"
    assert span.kind == "SERVER"
    assert (span.start_time, span.end_time, span.duration_ns) == (1_000, 3_500, 2_500)
    assert span.status_code == "ERROR"
    assert span.status_msg == "upstream 500"


def test_child_span_gets_hex_parent_id():
    body = request_bytes({"order-service": [otlp_span(span_id=CHILD_ID, parent=ROOT_ID)]})
    [span] = decode_traces(body)
    assert span.parent_span_id == "00f067aa0ba902b7"


def test_service_name_copied_onto_every_span_per_resource():
    body = request_bytes({
        "gateway": [otlp_span()],
        "order-service": [otlp_span(span_id=CHILD_ID, parent=ROOT_ID), otlp_span(span_id=b"\x01" * 8)],
    })
    assert [s.service for s in decode_traces(body)] == ["gateway", "order-service", "order-service"]


def test_missing_service_name_falls_back_to_unknown_service():
    [span] = decode_traces(request_bytes({None: [otlp_span()]}))
    assert span.service == "unknown_service"


def test_unset_status_and_unspecified_kind():
    [span] = decode_traces(request_bytes({"svc": [otlp_span(kind=OtlpSpan.SPAN_KIND_UNSPECIFIED)]}))
    assert span.status_code == "UNSET"
    assert span.status_msg is None
    assert span.kind is None


@pytest.mark.parametrize("status_key", ["http.response.status_code", "http.status_code"])
def test_promotes_http_status_and_route(status_key):
    attrs = [kv(status_key, 504), kv("http.route", "/charge")]
    [span] = decode_traces(request_bytes({"payment": [otlp_span(attrs=attrs)]}))
    assert span.http_status == 504
    assert span.route == "/charge"
    assert span.attributes[status_key] == 504  # also kept in the full attribute set


def test_no_http_attributes_means_null_columns():
    [span] = decode_traces(request_bytes({"svc": [otlp_span(attrs=[kv("db.system", "postgresql")])]}))
    assert span.http_status is None
    assert span.route is None


def test_attribute_value_types():
    attrs = [
        kv("s", "text"),
        kv("i", 42),
        kv("f", 1.5),
        kv("b", True),
        KeyValue(key="arr", value=AnyValue(array_value=ArrayValue(values=[AnyValue(int_value=1), AnyValue(string_value="x")]))),
        KeyValue(key="map", value=AnyValue(kvlist_value=KeyValueList(values=[kv("nested", "yes")]))),
        KeyValue(key="raw", value=AnyValue(bytes_value=b"\xde\xad")),
        KeyValue(key="empty", value=AnyValue()),
    ]
    [span] = decode_traces(request_bytes({"svc": [otlp_span(attrs=attrs)]}))
    assert span.attributes == {
        "s": "text", "i": 42, "f": 1.5, "b": True,
        "arr": [1, "x"], "map": {"nested": "yes"}, "raw": "dead", "empty": None,
    }


def test_gzip_body():
    body = gzip.compress(request_bytes({"gateway": [otlp_span()]}))
    [span] = decode_traces(body, content_encoding="gzip")
    assert span.service == "gateway"


def test_empty_request_decodes_to_no_spans():
    assert decode_traces(ExportTraceServiceRequest().SerializeToString()) == []


def test_invalid_protobuf_raises():
    with pytest.raises(InvalidPayload):
        decode_traces(b"not a protobuf")


def test_invalid_gzip_raises():
    with pytest.raises(InvalidPayload):
        decode_traces(b"definitely not gzip", content_encoding="gzip")


def test_decoded_spans_insert_into_duckdb(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    body = request_bytes({
        "gateway": [otlp_span(attrs=[kv("http.response.status_code", 500)])],
        "order-service": [otlp_span(span_id=CHILD_ID, parent=ROOT_ID, name="createOrder")],
    })
    for span in decode_traces(body):
        row = span.to_row()
        conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))
    rows = conn.execute(
        "SELECT service, parent_span_id, http_status FROM spans ORDER BY service"
    ).fetchall()
    assert rows == [("gateway", None, 500), ("order-service", "00f067aa0ba902b7", None)]
