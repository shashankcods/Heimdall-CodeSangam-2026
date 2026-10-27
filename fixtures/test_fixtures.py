"""Unit tests verifying that Heimdall's decoders correctly process the captured wire fixtures."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Import decoders from backend (import only, as agreed in TEAM.md)
from heimdall.ingest.decoder import decode_logs, decode_traces

FIXTURES_DIR = Path(__file__).resolve().parent / "otlp"


def test_decode_single_root_gzip():
    payload = (FIXTURES_DIR / "trace_single_root_gzip.pb").read_bytes()
    spans = decode_traces(payload, content_encoding="gzip")
    assert len(spans) >= 1
    root = spans[0]
    assert root.service == "gateway"
    assert root.name == "GET /health"
    assert root.http_status == 200
    assert root.route == "/health"
    assert root.parent_span_id is None


def test_decode_single_root_raw():
    payload = (FIXTURES_DIR / "trace_single_root_raw.pb").read_bytes()
    spans = decode_traces(payload)
    assert len(spans) >= 1
    root = spans[0]
    assert root.service == "gateway"
    assert root.name == "GET /health"
    assert root.http_status == 200


def test_decode_error_span():
    payload = (FIXTURES_DIR / "trace_error_span.pb").read_bytes()
    spans = decode_traces(payload)
    assert len(spans) >= 1
    err = spans[0]
    assert err.service == "payment-service"
    assert err.name == "chargeCard"
    assert err.status_code == "ERROR"
    assert "timeout" in err.status_msg.lower()
    assert err.http_status == 504


def test_decode_no_http_attrs():
    payload = (FIXTURES_DIR / "trace_no_http_attrs.pb").read_bytes()
    spans = decode_traces(payload)
    assert len(spans) >= 1
    span = spans[0]
    assert span.service == "worker-service"
    assert span.name == "calculate_checksum"
    assert span.http_status is None
    assert span.route is None
    assert "hash.algorithm" in span.attributes


def test_decode_four_span_chain():
    payload = (FIXTURES_DIR / "trace_four_span_chain.pb").read_bytes()
    spans = decode_traces(payload)
    assert len(spans) >= 1
    span_names = {s.name for s in spans}
    assert any("order" in name.lower() for name in span_names)


def test_decode_four_span_chain_gzip():
    payload = (FIXTURES_DIR / "trace_four_span_chain_gzip.pb").read_bytes()
    spans = decode_traces(payload, content_encoding="gzip")
    assert len(spans) >= 1


def test_decode_logs_with_trace_context():
    payload = (FIXTURES_DIR / "logs_with_trace_context.pb").read_bytes()
    logs = decode_logs(payload)
    assert len(logs) >= 1
    log = logs[0]
    assert log.service == "order-service"
    assert log.trace_id is not None and len(log.trace_id) == 32
    assert log.span_id is not None and len(log.span_id) == 16


def test_decode_logs_with_trace_context_gzip():
    payload = (FIXTURES_DIR / "logs_with_trace_context_gzip.pb").read_bytes()
    logs = decode_logs(payload, content_encoding="gzip")
    assert len(logs) >= 1
    log = logs[0]
    assert log.service == "order-service"
    assert log.trace_id is not None


def test_decode_logs_without_trace_context():
    payload = (FIXTURES_DIR / "logs_without_trace_context.pb").read_bytes()
    logs = decode_logs(payload)
    assert len(logs) >= 1
    log = logs[0]
    assert log.service == "order-service"
    assert log.trace_id is None
    assert log.span_id is None


def test_decode_logs_without_trace_context_gzip():
    payload = (FIXTURES_DIR / "logs_without_trace_context_gzip.pb").read_bytes()
    logs = decode_logs(payload, content_encoding="gzip")
    assert len(logs) >= 1
    log = logs[0]
    assert log.service == "order-service"
    assert log.trace_id is None
