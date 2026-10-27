# Heimdall

**Heimdall: an OpenTelemetry-native observability engine.**

A self-hosted mini Jaeger + log explorer + service map, backed by DuckDB. Services send standard OTLP traces and logs; Heimdall stores them in a columnar store and lets you explore traces as waterfalls, search logs, and see the service topology.

> Work in progress. See [DESIGN.md](DESIGN.md) for the architecture and build plan.

## Layout

```
backend/    FastAPI ingest + DuckDB store + query API (Python, uv)
frontend/   React + TypeScript UI (Vite)
demo/       Fake instrumented services + load generator
```

## Running (backend)

```bash
cd backend
uv sync
uv run pytest
uv run uvicorn heimdall.api.main:app --port 4318
```

Point any OTel SDK at it with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` and `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. Traces go to `/v1/traces` and logs to `/v1/logs`; browse traces at `http://localhost:4318/api/traces`.

To send real SDK traces and logs from a tiny instrumented service: `uv run demo/sdk_smoke.py` (from the repo root, with the backend running).

Settings (environment variables): `HEIMDALL_DB_PATH` (default `heimdall.duckdb`), `HEIMDALL_MAX_PENDING` (100000), `HEIMDALL_BATCH_SIZE` (500), `HEIMDALL_FLUSH_INTERVAL` (1.0 s).

## Multi-Service Demo (`gateway -> order -> payment`)

A distributed 3-service demonstration instrumented with OpenTelemetry SDK and W3C trace context propagation.

### Architecture

```
Client / Ingress
      │  POST /order
      ▼
┌──────────────┐      POST /order (W3C traceparent)      ┌──────────────┐      POST /pay (W3C traceparent)      ┌──────────────┐
│   gateway    │ ───────────────────────────────────────> │    order     │ ─────────────────────────────────────> │   payment    │
│  (port 8001) │                                          │ (port 8002)  │                                        │ (port 8003)  │
└──────────────┘                                          └──────────────┘                                        └──────────────┘
```

- **`gateway` (port 8001)**: Public ingress endpoint (`POST /order`) creating root server and downstream client spans.
- **`order` (port 8002)**: Order validation and management; propagates distributed trace context to payment.
- **`payment` (port 8003)**: Simulates payment authorization, card declines (HTTP 402), or downstream 500 crashes.

### Quick Start (One-Shot Automated Run)

With Heimdall running on port 4318:

```bash
uv run demo/run_demo.py --once
```

This starts all three services, sends sample traffic with an injected decline test, flushes telemetry, verifies traces, logs, and service dependencies in Heimdall, and cleanly stops the child services.

### Interactive Demo Modes

1. **Start all 3 services in the background:**
   ```bash
   uv run demo/run_demo.py --serve
   ```
2. **Send bounded traffic:**
   ```bash
   uv run demo/run_demo.py --traffic --count 10
   ```
3. **Verify telemetry:**
   ```bash
   uv run demo/run_demo.py --verify
   ```

### Running in Separate Terminals

If you prefer running services independently:

```bash
# Terminal 1: Payment Service
uv run demo/services/payment.py

# Terminal 2: Order Service
uv run demo/services/order.py

# Terminal 3: Gateway Service
uv run demo/services/gateway.py
```

### Runtime Fault Injection

Each service exposes runtime fault-control endpoints:

- **Inject Latency (0 - 30,000 ms):**
  ```bash
  # Add 250ms delay to payment service
  curl -X POST http://127.0.0.1:8003/fault/latency -H "Content-Type: application/json" -d "{\"delay_ms\": 250}"

  # Or via CLI runner
  uv run demo/run_demo.py --fault-service payment --fault-latency 250
  ```

- **Simulate Card Decline (Expected Business Failure, HTTP 402):**
  ```bash
  curl -X POST http://127.0.0.1:8003/fault/error -H "Content-Type: application/json" -d "{\"error_type\": \"decline\", \"rate\": 1.0}"
  ```

- **Simulate Service Crash / Timeout (Unexpected Infrastructure Failure, HTTP 500):**
  ```bash
  curl -X POST http://127.0.0.1:8003/fault/error -H "Content-Type: application/json" -d "{\"error_type\": \"500\", \"rate\": 1.0}"
  ```

- **Reset Faults:**
  ```bash
  # Reset via gateway reset-all endpoint
  curl -X POST http://127.0.0.1:8001/fault/reset-all

  # Or via CLI runner
  uv run demo/run_demo.py --reset-faults
  ```

### Verifying Telemetry in Heimdall

Once traffic is sent:

- **Traces:** Browse recent traces at `http://127.0.0.1:4318/api/traces`
- **Trace Waterfall:** View full 7-span distributed trace at `http://127.0.0.1:4318/api/traces/{trace_id}`
- **Logs:** Query structured logs at `http://127.0.0.1:4318/api/logs?service=order`
- **Service Map:** Query service topology at `http://127.0.0.1:4318/api/service-map`

### Troubleshooting

- **Port in use (8001, 8002, 8003):** Override ports via environment variables: `GATEWAY_SERVICE_PORT`, `ORDER_SERVICE_PORT`, `PAYMENT_SERVICE_PORT` or pass `--gateway-port`, `--order-port`, `--payment-port` to `demo/run_demo.py`.
- **Heimdall unreachable:** Ensure backend is started with `uv run uvicorn heimdall.api.main:app --port 4318`.
- **Missing spans/logs:** Telemetry uses batch processors (`flush_interval=1.0s`). Wait 1-2 seconds after sending traffic before querying APIs, or call `flush_telemetry()` on service exit.
