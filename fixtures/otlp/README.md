# Heimdall OTLP Wire Fixture Library

This directory contains authentic binary OpenTelemetry Protocol (OTLP) protobuf payloads captured directly from real OpenTelemetry SDK wire transmissions (`/v1/traces` and `/v1/logs`).

These fixtures decouple backend decoder and ingestion tests from live network servers or SDK runtimes, providing fast, deterministic, offline test data.

## Fixture Catalog

| Fixture File | Type | Compression | Source SDK | Description |
|---|---|---|---|---|
| `trace_single_root_raw.pb` | Traces | None (raw) | Python OTel SDK | Single root SERVER span (`GET /health`), HTTP 200, status OK |
| `trace_single_root_gzip.pb` | Traces | gzip | Python OTel SDK | Same single root span exported with standard `gzip` compression |
| `trace_error_span.pb` | Traces | None (raw) | Python OTel SDK | Failed span (`chargeCard`), `StatusCode.ERROR`, HTTP 504, timeout message |
| `trace_no_http_attrs.pb` | Traces | None (raw) | Python OTel SDK | Internal compute span (`calculate_checksum`) without any `http.*` attributes |
| `trace_four_span_chain.pb` | Traces | None (raw) | Python OTel SDK | Distributed trace chain: `gateway` &rarr; `createOrder` &rarr; `INSERT orders` &rarr; `chargeCard` |
| `trace_four_span_chain_gzip.pb` | Traces | gzip | Python OTel SDK | Gzipped version of the 4-span distributed trace chain |
| `logs_with_trace_context.pb` | Logs | None (raw) | Python OTel SDK | Log records emitted inside active spans carrying 32-char `trace_id` and 16-char `span_id` |
| `logs_with_trace_context_gzip.pb` | Logs | gzip | Python OTel SDK | Gzipped version of correlated log records |
| `logs_without_trace_context.pb` | Logs | None (raw) | Python OTel SDK | System/heartbeat log records emitted outside request context (`trace_id` is null) |
| `logs_without_trace_context_gzip.pb` | Logs | gzip | Python OTel SDK | Gzipped version of logs without trace context |

## How to Capture New Fixtures

1. Run the OTLP catcher server:
   ```bash
   uv run fixtures/catcher.py --port 4319
   ```

2. Point any OTel SDK (Python, Node.js, Go, Java) at the catcher:
   ```bash
   OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4319
   OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
   ```

3. Optionally pass the header `X-Fixture-Name: my_fixture.pb` to specify the output filename.

4. To regenerate all standard fixtures automatically:
   ```bash
   uv run fixtures/generate_fixtures.py
   ```

## Verifying Decoder Compatibility

Run the fixture test suite from the repository root:
```bash
uv run pytest fixtures/test_fixtures.py
```
