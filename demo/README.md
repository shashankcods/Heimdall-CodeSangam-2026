# Heimdall Demo Microservices Cluster

A distributed multi-language e-commerce microservices cluster instrumented with OpenTelemetry to demonstrate Heimdall's distributed tracing, service map topology, and correlated log analysis.

## Service Architecture

```
                    ┌─────────────────────────┐
                    │   Client / Load Gen     │
                    └────────────┬────────────┘
                                 │ HTTP POST /order
                                 ▼
                    ┌─────────────────────────┐
                    │      gateway (8001)     │  (FastAPI + Python OTel SDK)
                    └────────────┬────────────┘
                                 │ HTTP POST /orders
                                 ▼
                    ┌─────────────────────────┐
                    │   order-service (8002)  │  (FastAPI + Python OTel SDK + DB simulation)
                    └─────┬─────────────┬─────┘
          POST /reserve   │             │  POST /charge
                          ▼             ▼
   ┌──────────────────────────┐    ┌──────────────────────────┐
   │ inventory-service (8004) │    │  payment-service (8003)  │
   │ (Node.js Express + OTel) │    │ (FastAPI + Stripe span)  │
   └──────────────────────────┘    └──────────────────────────┘
```

## Running the Cluster

### Option 1: Docker Compose (One-Command Startup)

Start all 4 services and the continuous load generator with Docker Compose:

```bash
# Using the root convenience file
docker compose -f docker-compose.demo.yml up --build

# Or from the demo directory
docker compose -f demo/docker-compose.yml up --build
```

To point the demo services to a specific Heimdall instance:
```bash
HEIMDALL_URL=http://host.docker.internal:4318 docker compose -f docker-compose.demo.yml up
```

### Option 2: Local Python + Node.js Runner

Ensure Heimdall backend is running (`cd backend && uv run uvicorn heimdall.api.main:app --port 4318`), then run:

```bash
uv run demo/run_demo.py
```

For headless execution in CI or background scripts:
```bash
uv run demo/run_demo.py --non-interactive
```

## Interactive Fault Injection

When running `demo/run_demo.py` interactively, type any of the following commands:
- `f` or `fault`: Inject 1.9s latency and HTTP 504 on `payment-service`.
- `i` or `inv`: Inject 1.5s delay and 500 reservation error on `inventory-service` (Node.js).
- `r` or `reset`: Clear all injected faults.
- `s` or `status`: Display load generator throughput, success rate, and active faults.
- `q` or `quit`: Gracefully terminate all services.

Or trigger fault endpoints directly via HTTP:
```bash
# Inject payment failure
curl -X POST http://127.0.0.1:8003/fault -H "Content-Type: application/json" \
  -d '{"latency_ms": 1900.0, "error_rate": 1.0, "error_status": 504}'

# Reset payment to healthy
curl -X POST http://127.0.0.1:8003/fault/reset
```

## Endpoints

| Service | Port | Endpoint | Description |
|---|---|---|---|
| **Gateway** | `8001` | `POST /order` | Public entrypoint for orders |
| **Order Service** | `8002` | `POST /orders` | Order creation and database write |
| **Inventory Service** | `8004` | `POST /reserve` | Stock reservation (Node.js Express) |
| **Payment Service** | `8003` | `POST /charge` | Payment gateway and fault injection |
