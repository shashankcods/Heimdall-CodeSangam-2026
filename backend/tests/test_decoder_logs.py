import gzip
import json
import time

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue, KeyValueList
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord as OtlpLogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from heimdall.ingest.decoder import InvalidPayload, decode_logs
from heimdall.store.schema import connect
from tests.test_decoder import ROOT_ID, TRACE_ID, kv


def otlp_log(body="Stripe timeout", severity=17, **kwargs):
    return OtlpLogRecord(
        time_unix_nano=kwargs.get("time", 5_000),
        observed_time_unix_nano=kwargs.get("observed", 6_000),
        severity_number=severity,
        severity_text=kwargs.get("severity_text", ""),
        body=kwargs.get("body_value", AnyValue(string_value=body)),
        trace_id=kwargs.get("trace_id", TRACE_ID),
        span_id=kwargs.get("span_id", ROOT_ID),
        attributes=list(kwargs.get("attrs", ())),
    )


def logs_request_bytes(logs_by_service):
    """{service_name or None: [otlp log records]} -> serialized ExportLogsServiceRequest."""
    request = ExportLogsServiceRequest()
    for service, records in logs_by_service.items():
        resource = Resource(attributes=[kv("service.name", service)] if service else [])
        request.resource_logs.append(
            ResourceLogs(resource=resource, scope_logs=[ScopeLogs(log_records=records)])
        )
    return request.SerializeToString()


def test_decodes_log_fields():
    [log] = decode_logs(logs_request_bytes({"payment-service": [otlp_log(attrs=[kv("retry", 0)])]}))
    assert log.timestamp == 5_000
    assert log.service == "payment-service"
    assert log.severity == "ERROR"
    assert log.body == "Stripe timeout"
    assert log.trace_id == TRACE_ID.hex()
    assert log.span_id == ROOT_ID.hex()
    assert log.attributes == {"retry": 0}


@pytest.mark.parametrize("number, expected", [
    (1, "TRACE"), (4, "TRACE"), (5, "DEBUG"), (9, "INFO"), (12, "INFO"),
    (13, "WARN"), (17, "ERROR"), (20, "ERROR"), (21, "FATAL"), (24, "FATAL"),
])
def test_severity_number_blocks(number, expected):
    [log] = decode_logs(logs_request_bytes({"svc": [otlp_log(severity=number)]}))
    assert log.severity == expected


@pytest.mark.parametrize("text, expected", [
    ("warning", "WARN"), ("Error", "ERROR"), ("critical", "FATAL"), ("info", "INFO"),
    ("verbose", None), ("", None),
])
def test_severity_text_used_when_number_unset(text, expected):
    [log] = decode_logs(logs_request_bytes({"svc": [otlp_log(severity=0, severity_text=text)]}))
    assert log.severity == expected


def test_log_outside_a_request_has_no_trace_ids():
    [log] = decode_logs(logs_request_bytes({"gateway": [otlp_log(trace_id=b"", span_id=b"")]}))
    assert log.trace_id is None
    assert log.span_id is None


def test_timestamp_falls_back_to_observed_time_then_now():
    [observed] = decode_logs(logs_request_bytes({"svc": [otlp_log(time=0, observed=6_000)]}))
    assert observed.timestamp == 6_000

    before = time.time_ns()
    [neither] = decode_logs(logs_request_bytes({"svc": [otlp_log(time=0, observed=0)]}))
    assert neither.timestamp >= before


def test_structured_body_stored_as_json_text():
    body = AnyValue(kvlist_value=KeyValueList(values=[kv("event", "checkout"), kv("items", 3)]))
    [log] = decode_logs(logs_request_bytes({"svc": [otlp_log(body_value=body)]}))
    assert json.loads(log.body) == {"event": "checkout", "items": 3}


def test_empty_body_is_none():
    [log] = decode_logs(logs_request_bytes({"svc": [otlp_log(body_value=AnyValue())]}))
    assert log.body is None


def test_service_name_per_resource_and_fallback():
    logs = decode_logs(logs_request_bytes({
        "gateway": [otlp_log()],
        None: [otlp_log(), otlp_log()],
    }))
    assert [l.service for l in logs] == ["gateway", "unknown_service", "unknown_service"]


def test_gzip_body():
    body = gzip.compress(logs_request_bytes({"gateway": [otlp_log()]}))
    assert len(decode_logs(body, content_encoding="gzip")) == 1


def test_empty_and_invalid_payloads():
    assert decode_logs(ExportLogsServiceRequest().SerializeToString()) == []
    with pytest.raises(InvalidPayload):
        decode_logs(b"not a protobuf")


def test_decoded_logs_insert_into_duckdb(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    for log in decode_logs(logs_request_bytes({"payment-service": [otlp_log(), otlp_log(trace_id=b"")]})):
        row = log.to_row()
        conn.execute(f"INSERT INTO logs VALUES ({', '.join('?' * len(row))})", list(row.values()))
    rows = conn.execute("SELECT service, severity, trace_id FROM logs ORDER BY trace_id NULLS LAST").fetchall()
    assert rows == [("payment-service", "ERROR", TRACE_ID.hex()), ("payment-service", "ERROR", None)]
