# OpenTelemetry Collector Compatibility

This document details Heimdall's wire-level compatibility with the standard **OpenTelemetry Collector** (`otelcol` / `otelcol-contrib`).

## Architecture & Pipeline

```
┌────────────────────────────────────────────────────────┐
│                   Demo Microservices                   │
│ (Gateway, Order Service, Payment Service, Inventory)   │
└───────────────────────────┬────────────────────────────┘
                            │ OTLP/HTTP or OTLP/gRPC
                            ▼
┌────────────────────────────────────────────────────────┐
│            OpenTelemetry Collector (otelcol)           │
│                                                        │
│  1. Receivers:   otlp (HTTP :4318, gRPC :4317)         │
│  2. Processors:  batch (send_batch_size: 256, timeout) │
│  3. Exporters:   otlphttp (compression: gzip)          │
└───────────────────────────┬────────────────────────────┘
                            │ Batched Protobuf over HTTP (Gzip)
                            │ POST /v1/traces, POST /v1/logs
                            ▼
┌────────────────────────────────────────────────────────┐
│                   Heimdall Observability               │
│                                                        │
│  - Ingest Gateway (gzip decompress + protobuf decode)  │
│  - Bounded Asyncio Ingestion Queue                     │
│  - DuckDB Columnar Storage Engine                      │
└────────────────────────────────────────────────────────┘
```

## Configuration

The configuration file [`demo/otel-collector-config.yaml`](./otel-collector-config.yaml) defines the pipeline:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch:
    send_batch_size: 256
    timeout: 1s
    send_batch_max_size: 1024

exporters:
  otlphttp:
    endpoint: ${HEIMDALL_URL:http://host.docker.internal:4318}
    compression: gzip
    tls:
      insecure: true

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp]
    logs:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlphttp]
```

## Running the Demo Cluster through OTel Collector

Launch the collector-backed demo cluster with Docker Compose:

```bash
docker compose -f demo/docker-compose.collector.yml up --build
```

The services emit telemetry to the OTel Collector on port `4318`, which buffers and batches records before exporting them to Heimdall.

## Compatibility Proof & Automated Verification

Compatibility is verified via automated tests in [`demo/test_collector_compat.py`](./test_collector_compat.py):
1. **Pipeline Syntax**: Confirms standard schema conformance, batch processor tuning, and gzip compression.
2. **Multi-Resource Batched Ingestion**: Simulates the exact multi-service payload produced by the collector's `batch` processor (`ExportTraceServiceRequest` containing multiple `ResourceSpans` from distinct services and `ExportLogsServiceRequest`).
3. **Trace Topology & Correlated Logs**: Verifies that Heimdall accurately reconstructs cross-service parent-child links and indexes correlated logs from a single collector batch.

Run the test suite:
```bash
uv run --directory backend pytest ../demo/test_collector_compat.py -v
```
