# Heimdall

> **An OpenTelemetry-native observability engine.**  
> Self-hosted distributed tracing, live log exploration, and auto-generated service topology, backed by DuckDB.

---

## Architecture Overview

```
                      ┌──────────────────────────────────────────────┐
                      │    Instrumented Applications & Services      │
                      │   (Python FastAPI, Node.js Express, etc.)    │
                      └──────────────────────┬───────────────────────┘
                                             │ OTLP / Protobuf (HTTP / Gzip)
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │    Optional OpenTelemetry Collector (otelcol)│
                      │       (batch processor, send_batch: 256)     │
                      └──────────────────────┬───────────────────────┘
                                             │ POST /v1/traces, POST /v1/logs
                                             ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   HEIMDALL BACKEND                                     │
│                                                                                        │
│   1. INGESTION GATEWAY                                                                 │
│      Decompress Gzip ──► Decode Protobuf ──► Flatten Span & Log Models                 │
│                                │                                                       │
│                                ▼                                                       │
│   2. BOUNDED QUEUE & BACKPRESSURE                                                      │
│      asyncio.Queue, limit 100k pending spans ──► HTTP 429 when full (Retry-After)      │
│                                │                                                       │
│                                ▼                                                       │
│   3. BATCH WRITER                                                                      │
│      Flush trigger: 500 records OR 1.0s ──► PyArrow Table Bulk Insert                  │
│                                │                                                       │
│                                ▼                                                       │
│   4. COLUMNAR STORAGE ENGINE (DuckDB)                                                  │
│      duckdb.connect(db_path) ──► spans + logs tables (flat, one row per record)        │
│                                │                                                       │
│                                ▼                                                       │
│   5. ANALYTICAL QUERY & EVENT LAYER                                                    │
│      list_traces, get_trace, search_logs, service_map (self-join), live SSE broadcast  │
│                                │                                                       │
│               ┌────────────────┴────────────────────────┐                              │
│               ▼                                         ▼                              │
│   6. REST & SSE API (/api/*)              7. MODEL CONTEXT PROTOCOL (MCP)              │
│      FastAPI endpoints + SSE stream          MCP server (/mcp) for AI Assistants       │
└───────────────┬─────────────────────────────────────────┬──────────────────────────────┘
                │                                         │
                ▼                                         ▼
┌────────────────────────────────┐      ┌────────────────────────────────────────────────┐
│      HEIMDALL FRONTEND         │      │             AI CODING AGENTS                   │
│   React + TypeScript + Vite    │      │        (Claude Code, Cursor, AGY)              │
│  - Trace Waterfall Timeline    │      │  - Inspect spans, root cause error traces      │
│  - Live Log Explorer (SSE)     │      │  - Query service topology and error rates      │
│  - Interactive Service Map     │      └────────────────────────────────────────────────┘
└────────────────────────────────┘
```

---

## Benchmarks

Observability queries scan and aggregate many spans (error rates per service, p95/p99 latency, the service-map self-join), which is why Heimdall uses **DuckDB** from day one. Full method and reproduction steps: [bench/README.md](bench/README.md).

### DuckDB vs indexed SQLite, 1,000,000 spans

Same data in both; SQLite gets the indexes a real deployment would have; every query returns the same answer on both engines.

| Query | DuckDB | SQLite | |
|---|---|---|---|
| Error rate per service | 4.8 ms | 512 ms | 108x faster |
| Exact p95/p99 latency per service | 19.1 ms | 2,193 ms | 115x faster |
| Service-map self-join | 85.1 ms | 1,747 ms | 20.5x faster |
| Errors in the last 5 minutes | 2.4 ms | 100 ms | 41x faster |
| Single trace lookup by id | 3.1 ms | 0.05 ms | 66x slower |
| Storage per span | 42 B | 293 B | 6.9x smaller |

Columnar storage reads only the columns a query touches and compresses repetitive values (service names, routes, status codes), so it wins every scan and aggregation. A row store's index still wins single-row lookups.

### Ingest throughput

250,000 spans sent as 500-span gzip requests by 20 concurrent senders:

| Batch size | Written to DuckDB | Requests answered 429 | Spans lost |
|---|---|---|---|
| 500 (default) | 27,696 spans/s | 36% | 0 |
| 5,000 | 66,489 spans/s | 0% | 0 |

Under overload the bounded queue holds at its 100,000-span limit and answers 429 instead of growing memory, and every accepted span is still written.

---

## Demo Microservices Cluster

Heimdall includes an end-to-end, multi-language e-commerce demo cluster to simulate real-world microservice traffic and failure propagation:

```
                    ┌─────────────────────────┐
                    │   Client / Load Gen     │  (2 req/sec continuous load)
                    └────────────┬────────────┘
                                 │ HTTP POST /order
                                 ▼
                    ┌─────────────────────────┐
                    │      gateway (8001)     │  (FastAPI + Python OTel SDK)
                    └────────────┬────────────┘
                                 │ HTTP POST /orders
                                 ▼
                    ┌─────────────────────────┐
                    │   order-service (8002)  │  (FastAPI + PostgreSQL DB span)
                    └─────┬─────────────┬─────┘
          POST /reserve   │             │  POST /charge
                          ▼             ▼
   ┌──────────────────────────┐    ┌──────────────────────────┐
   │ inventory-service (8004) │    │  payment-service (8003)  │
   │ (Node.js Express + OTel) │    │ (FastAPI + Stripe span)  │
   └──────────────────────────┘    └──────────────────────────┘
```

- **Cross-Language Distributed Tracing**: Distributed W3C `traceparent` headers propagate seamlessly between Python FastAPI and Node.js Express.
- **Dynamic Fault Injection**: Toggle faults in real time (`POST /fault`) on `payment-service` (1.9s latency + 504 Gateway Timeout) or `inventory-service` (stock reservation failure) and observe the waterfall turning red on Heimdall's UI.

---

## Key Features

### 1. Distributed Trace Flamegraph & Waterfall
- Visualizes end-to-end request latency, hierarchical parent-child span timelines, duration bars, and HTTP status codes.
- Highlights root cause spans in red when errors occur (e.g. 504 timeout on downstream calls).

### 2. Live Log Explorer & Tail
- Ingests structured logs at `/v1/logs` carrying `trace_id` and `span_id`.
- Full-text search and regex filtering.
- Real-time live log tail over Server-Sent Events (SSE).
- One-click jump from a log entry directly into its trace waterfall.

### 3. Service Topology Map
- Auto-generated dependency graph computed from inter-service parent-child links.
- Displays request volumes, average latency, and error rates per service edge.

### 4. AI Assistant Model Context Protocol (MCP) Server
- Exposes telemetry tools over standard MCP HTTP transport (`/mcp`):
  - `list_services`: List all active services and their span volumes.
  - `search_traces`: Find slow or failing traces by service, duration, or error flag.
  - `get_trace`: Retrieve the complete span tree for root-cause analysis.
  - `search_logs`: Search correlated structured logs.
  - `get_service_map`: Inspect service dependency graph and edge error rates.

Connect with Claude Code:
```bash
claude mcp add --transport http heimdall http://localhost:4318/mcp
```

---

## Quickstart

### 1. Run Heimdall Locally

**Backend:**
```bash
cd backend
uv sync
uv run pytest
uv run uvicorn heimdall.api.main:app --port 4318
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:5173` to explore traces, logs, and the service map.

### 2. Run the Demo Cluster

**Option A: Single-Command Docker Compose**
```bash
docker compose -f docker-compose.demo.yml up --build
```

**Option B: Python + Node Runner**
```bash
uv run demo/run_demo.py
```
Type `f` to inject payment fault, `i` to inject inventory fault, `r` to reset, and `q` to quit.

### 3. Run with OpenTelemetry Collector
```bash
docker compose -f demo/docker-compose.collector.yml up --build
```

---

## Environment Configuration

| Variable | Default | Description |
|---|---|---|
| `HEIMDALL_DB_PATH` | `heimdall.duckdb` | Path to persistent DuckDB database file |
| `HEIMDALL_MAX_PENDING` | `100000` | Bounded ingestion queue capacity before returning 429 |
| `HEIMDALL_BATCH_SIZE` | `500` | Maximum span/log batch size before writing to DuckDB |
| `HEIMDALL_FLUSH_INTERVAL` | `1.0` | Maximum seconds before flushing buffer to disk |
| `HEIMDALL_MAX_LIVE_VIEWERS` | `50` | Maximum concurrent SSE live tail viewers |
| `HEIMDALL_URL` | `http://127.0.0.1:4318` | Base URL for OTLP exporters |

