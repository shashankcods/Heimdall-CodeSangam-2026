"""Tests verifying OpenTelemetry Collector pipeline configuration and wire compatibility with Heimdall."""

import gzip
import os
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from heimdall.api.main import app

CONFIG_PATH = REPO_ROOT / "demo" / "otel-collector-config.yaml"


def test_collector_config_structure():
    """Verify the OTel Collector YAML configuration conforms to standard schema and specifies batch processor."""
    assert CONFIG_PATH.exists(), "Collector configuration file missing"
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 1. Receivers
    assert "otlp" in cfg.get("receivers", {})
    protocols = cfg["receivers"]["otlp"].get("protocols", {})
    assert "http" in protocols
    assert "grpc" in protocols

    # 2. Processors (must include batch processor per issue [B8])
    assert "batch" in cfg.get("processors", {})
    batch_cfg = cfg["processors"]["batch"]
    assert "timeout" in batch_cfg
    assert "send_batch_size" in batch_cfg
    assert batch_cfg["send_batch_size"] >= 100

    # 3. Exporters (must use otlphttp targeting Heimdall with gzip)
    assert "otlphttp" in cfg.get("exporters", {})
    exporter_cfg = cfg["exporters"]["otlphttp"]
    assert exporter_cfg.get("compression") == "gzip"

    # 4. Pipelines
    pipelines = cfg.get("service", {}).get("pipelines", {})
    assert "traces" in pipelines
    assert "logs" in pipelines
    assert "batch" in pipelines["traces"]["processors"]
    assert "batch" in pipelines["logs"]["processors"]
    assert "otlphttp" in pipelines["traces"]["exporters"]
    assert "otlphttp" in pipelines["logs"]["exporters"]


@pytest.fixture
def test_client(tmp_path, monkeypatch):
    """Clean test client with ephemeral DuckDB."""
    db_path = str(tmp_path / "collector_test.duckdb")
    monkeypatch.setenv("HEIMDALL_DB_PATH", db_path)
    monkeypatch.setenv("HEIMDALL_BATCH_SIZE", "1")
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")

    with TestClient(app) as client:
        yield client


def test_collector_batched_multi_resource_traces_and_logs(test_client):
    """Simulate the exact output of an OTel Collector batch processor:

    A single gzipped ExportTraceServiceRequest containing multiple ResourceSpans
    from multiple distinct services, followed by correlated batched ResourceLogs.
    """
    trace_id_bytes = bytes.fromhex("11223344556677889900aabbccddeeff")
    root_span_id = bytes.fromhex("0011223344556677")
    child_span_id1 = bytes.fromhex("0022334455667788")
    child_span_id2 = bytes.fromhex("0033445566778899")

    now_ns = time.time_ns()

    # Build batched trace payload with 3 distinct services grouped by ResourceSpans
    trace_req = ExportTraceServiceRequest()

    # 1. Gateway root span
    rs_gateway = trace_req.resource_spans.add()
    rs_gateway.resource.attributes.append(
        KeyValue(key="service.name", value=AnyValue(string_value="gateway"))
    )
    ss_gateway = rs_gateway.scope_spans.add()
    span1 = ss_gateway.spans.add(
        trace_id=trace_id_bytes,
        span_id=root_span_id,
        name="POST /order",
        start_time_unix_nano=now_ns,
        end_time_unix_nano=now_ns + 100_000_000,
        kind=Span.SPAN_KIND_SERVER,
    )
    span1.attributes.append(KeyValue(key="http.response.status_code", value=AnyValue(int_value=200)))
    span1.attributes.append(KeyValue(key="http.route", value=AnyValue(string_value="/order")))

    # 2. Order-service child span
    rs_order = trace_req.resource_spans.add()
    rs_order.resource.attributes.append(
        KeyValue(key="service.name", value=AnyValue(string_value="order-service"))
    )
    ss_order = rs_order.scope_spans.add()
    span2 = ss_order.spans.add(
        trace_id=trace_id_bytes,
        span_id=child_span_id1,
        parent_span_id=root_span_id,
        name="createOrder",
        start_time_unix_nano=now_ns + 10_000_000,
        end_time_unix_nano=now_ns + 90_000_000,
        kind=Span.SPAN_KIND_SERVER,
    )
    span2.attributes.append(KeyValue(key="http.response.status_code", value=AnyValue(int_value=200)))

    # 3. Inventory-service (Node.js) child span
    rs_inv = trace_req.resource_spans.add()
    rs_inv.resource.attributes.append(
        KeyValue(key="service.name", value=AnyValue(string_value="inventory-service"))
    )
    ss_inv = rs_inv.scope_spans.add()
    span3 = ss_inv.spans.add(
        trace_id=trace_id_bytes,
        span_id=child_span_id2,
        parent_span_id=child_span_id1,
        name="checkInventoryStock",
        start_time_unix_nano=now_ns + 20_000_000,
        end_time_unix_nano=now_ns + 50_000_000,
        kind=Span.SPAN_KIND_INTERNAL,
    )

    # Post gzipped batch to Heimdall /v1/traces (as otlphttp exporter does)
    trace_bytes = trace_req.SerializeToString()
    gzipped_trace = gzip.compress(trace_bytes)

    res = test_client.post(
        "/v1/traces",
        content=gzipped_trace,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
        },
    )
    assert res.status_code == 200
    resp_proto = ExportTraceServiceResponse()
    resp_proto.ParseFromString(res.content)

    # Build batched log payload for the same trace across services
    log_req = ExportLogsServiceRequest()
    rl_gw = log_req.resource_logs.add()
    rl_gw.resource.attributes.append(
        KeyValue(key="service.name", value=AnyValue(string_value="gateway"))
    )
    sl_gw = rl_gw.scope_logs.add()
    log1 = sl_gw.log_records.add(
        time_unix_nano=now_ns + 5_000_000,
        trace_id=trace_id_bytes,
        span_id=root_span_id,
        severity_text="INFO",
    )
    log1.body.string_value = "order request accepted by gateway"

    rl_ord = log_req.resource_logs.add()
    rl_ord.resource.attributes.append(
        KeyValue(key="service.name", value=AnyValue(string_value="order-service"))
    )
    sl_ord = rl_ord.scope_logs.add()
    log2 = sl_ord.log_records.add(
        time_unix_nano=now_ns + 15_000_000,
        trace_id=trace_id_bytes,
        span_id=child_span_id1,
        severity_text="INFO",
    )
    log2.body.string_value = "order reservation confirmed"

    log_bytes = log_req.SerializeToString()
    gzipped_logs = gzip.compress(log_bytes)

    res_log = test_client.post(
        "/v1/logs",
        content=gzipped_logs,
        headers={
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
        },
    )
    assert res_log.status_code == 200
    resp_log_proto = ExportLogsServiceResponse()
    resp_log_proto.ParseFromString(res_log.content)

    # Allow background writer flush
    time.sleep(0.3)

    # Verify trace retrieval in Heimdall
    trace_hex = trace_id_bytes.hex()
    detail_res = test_client.get(f"/api/traces/{trace_hex}")
    assert detail_res.status_code == 200
    detail = detail_res.json()
    spans = detail.get("spans", [])
    assert len(spans) == 3

    services = {s["service"] for s in spans}
    assert services == {"gateway", "order-service", "inventory-service"}

    # Verify parent-child links preserved across batched resources
    span_map = {s["span_id"]: s for s in spans}
    assert span_map[root_span_id.hex()]["parent_span_id"] is None
    assert span_map[child_span_id1.hex()]["parent_span_id"] == root_span_id.hex()
    assert span_map[child_span_id2.hex()]["parent_span_id"] == child_span_id1.hex()

    # Verify correlated logs retrieval
    logs_res = test_client.get(f"/api/logs?trace_id={trace_hex}&from_ms=0")
    assert logs_res.status_code == 200
    logs = logs_res.json().get("logs", [])
    assert len(logs) == 2
    log_bodies = [l["body"] for l in logs]
    assert "order request accepted by gateway" in log_bodies
    assert "order reservation confirmed" in log_bodies
