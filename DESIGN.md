# Heimdall: Design Document

An OpenTelemetry-native observability engine. Self-hosted mini Jaeger + log explorer + service map, backed by DuckDB.

This file is the hand-off from the planning conversation. Read it fully before writing code.

---

## 1. Working agreement (read first)

- **Outline before changing.** Before creating or editing any file, briefly outline what will change and why, so the owner can follow the flow. Then make the change.
- **Owner is learning as they build.** Prefer short explanations with a concrete example or dry run over abstract theory. When introducing a new concept (protobuf, batching, backpressure, columnar storage), show a tiny worked example first.
- **Guided over dumped.** For design decisions, offer the tradeoff and let the owner pick. Do not silently choose.
- **Concise, informal tone.**
- **Small steps.** One piece at a time, each runnable and testable before moving on.

## 2. Context

- **Event:** a development event with a ~1 week dev period, team of 1-3.
- **Problem statement:** #08 "The Eye of Agamotto" (Hard tier), distributed telemetry and observability engine.
- **Goal beyond the event:** a strong portfolio project for placement/internship season. Depth and proof (benchmarks, defensible decisions) matter more than feature count.
- **Name:** Heimdall. Repo name `heimdall-observability`, Python package/CLI `heimdall`. Tagline: "Heimdall: an OpenTelemetry-native observability engine".
- **Owner background:** knows FastAPI + React/TS (shipped a FastAPI/React transformer project), Node/MongoDB/LangGraph (a travel itinerary app), strong DSA. New to OpenTelemetry, protobuf, DuckDB.

### Original problem statement requirements

**Pass tier**
1. **OTel ingestion gateway:** ingest OpenTelemetry HTTP/gRPC traces and structured logs with high-throughput batching.
2. **Distributed trace flamegraph:** interactive waterfall timeline of spans, parent-child relationships, durations, HTTP status codes.
3. **Live log explorer:** searchable, filterable log query UI with full-text regex and dynamic tag filtering.
4. **Service topology map:** auto-generated service-to-service dependency graph showing latency and error rates.

**Victory tier** (must be enabled by the architecture without a rewrite)
1. **Columnar storage engine:** DuckDB or ClickHouse for sub-second analytical queries across millions of spans.
2. **Automated root-cause correlation:** anomaly detection highlighting spans whose p99 latency spiked during error clusters.
3. **MCP server:** expose telemetry to AI assistants via Model Context Protocol so an LLM can inspect spans and suggest fixes.

**Focus now: pass tier. Victory tier later, but every decision below is made so victory tier plugs in.**

## 3. Concepts (shared vocabulary)

| Term | Meaning |
|---|---|
| **Service** | A running program (e.g. `payment-service`). |
| **Span** | One unit of work inside a service for one request (a handler, a DB query, an outgoing call). One service produces many spans per request. |
| **Trace** | All spans of one request, linked by a shared `trace_id`. Spans link to each other via `parent_span_id`. |
| **Log** | A text event from a service, ideally carrying `trace_id`/`span_id` so it can be matched to a request. |
| **OTel** | OpenTelemetry, the standard SDK services use to emit telemetry. We do not build the sending side. |
| **OTLP** | OpenTelemetry Protocol, the wire format. HTTP on port 4318, gRPC on 4317. Payload is protobuf. |
| **Protobuf** | Compact binary format with a shared schema (`.proto`). The `opentelemetry-proto` package provides generated classes. |
| **Collector** | A separate pipeline program (receive, process, export). Not needed to build Heimdall, but Heimdall must accept its OTLP output unchanged. |
| **Batching** | Grouping many records into one bulk write to avoid per-insert overhead. |
| **Backpressure** | Telling senders to slow down when we cannot keep up (bounded queue, `429`). |

Important OTLP detail: the payload is **nested**, and `service.name` lives on the **resource**, not on each span:

```
ExportTraceServiceRequest
 └─ resource_spans[]
     ├─ resource.attributes: service.name = "payment-service"
     └─ scope_spans[]
         └─ spans[]: trace_id(16 bytes), span_id(8), parent_span_id(8, empty for root),
                     name, start/end_time_unix_nano, attributes[], status{code,message}
```

The decoder must carry `service.name` down onto every flat span.

## 4. Architecture

```
Demo services (OTel SDK)
        │  OTLP protobuf over HTTP (possibly gzip)
        ▼
1. INGEST   FastAPI POST /v1/traces, POST /v1/logs
        │   decode → normalized Span / LogRecord objects
        ▼
2. QUEUE    bounded in-memory asyncio.Queue
        │   endpoint replies 200 immediately; 429/503 if full (backpressure)
        ▼
3. WRITER   background task; flush at 500 records OR 1 second, whichever first
        │   bulk insert via Arrow table
        ▼
4. STORE    DuckDB (spans, logs)
        ▼
5. QUERIES  get_trace, list_traces, search_logs, service_edges, latency_percentiles
        ▼
6. API      REST under /api + SSE for live tail
        ▼
7. UI       React + TS: Traces/Waterfall, Logs, Service Map
```

### Stage details

**1. Ingest**
- Endpoints `POST /v1/traces` and `POST /v1/logs` (standard OTLP paths; do not move them under `/api`).
- Handle `Content-Encoding: gzip` (OTel SDKs often send gzipped bodies): decompress before decoding.
- Decode protobuf into flat `Span` / `LogRecord` dataclasses. Hex-encode `trace_id`/`span_id`. Compute `duration_ns = end - start`. Promote hot attributes (`http.status_code`, `http.route`) to real columns; keep the full attribute set as JSON.
- Respond with a valid empty `ExportTraceServiceResponse` / `ExportLogsServiceResponse`, HTTP 200.
- Design so a gRPC or JSON decoder can be added later as another front decoder producing the same normalized objects.

**2. Queue**
- Bounded `asyncio.Queue(maxsize=...)`. When full, respond `429` (or `503`); SDKs retry with backoff. This is the backpressure story.
- Durability tradeoff, stated explicitly: replying 200 before data is on disk means a crash can lose whatever is in the queue/buffer (about a second's worth). Accepted for telemetry.

**3. Writer**
- Background task loop: pull from queue, add to buffer, flush on `>= 500 records` or `>= 1s since last flush`.
- Flush = convert batch to a pyarrow table, then `INSERT INTO spans SELECT * FROM batch` inside a transaction (so a crash mid-flush cannot leave a half-written batch).
- DuckDB calls are blocking: run them in a thread (`asyncio.to_thread`).
- After each flush: record spans/sec for benchmarks, and broadcast new logs to live-tail subscribers.

**4. Store (DuckDB)**
- Columnar, embedded, single file. Victory-tier "columnar storage" is satisfied from day one.
- **DuckDB allows only one read-write process per file.** Therefore ingest, writer, and query API all live in **one backend process** with one connection owner (use `cursor()` per thread).
- Spans stored **flat**, one row per span. The tree is assembled at read time (frontend nests by `parent_span_id`). This handles out-of-order arrival (child can arrive before parent).

## 5. Schema

```
spans:  trace_id, span_id, parent_span_id, service, name, kind,
        start_time, end_time, duration_ns, status_code, status_msg,
        http_status, route, attributes (JSON)

logs:   timestamp, service, severity, body, trace_id, span_id,
        attributes (JSON)
```

Example rows:

| trace_id | span_id | parent_span_id | service | name | duration_ns | status | http_status |
|---|---|---|---|---|---|---|---|
| abc123 | s1 | NULL | gateway | POST /order | 2,400,000,000 | ERROR | 500 |
| abc123 | s2 | s1 | order-service | createOrder | 2,350,000,000 | ERROR | 500 |
| abc123 | s3 | s2 | payment-service | chargeCard | 1,900,000,000 | ERROR | 504 |

Consider indexes/ordering on `trace_id` and on time for fast lookups; decide when benchmarking.

## 6. Query layer (single source of truth)

All consumers (REST API, anomaly job, MCP server) call these functions. **The UI and API never write SQL directly.** Lives in `queries/`.

| Function | Purpose |
|---|---|
| `list_traces(filters)` | Recent traces; filter by service, errors only, min duration, time range |
| `get_trace(trace_id)` | All flat span rows for one trace |
| `search_logs(q, regex, service, level, time_range, limit)` | Full-text, regex (`regexp_matches`), tag filters |
| `service_edges(time_range)` | Topology map data (see below) |
| `latency_percentiles(service, name, window)` | p50/p95/p99 via `quantile_cont` (victory tier) |

**Topology map derivation:** self-join spans child→parent. A child span whose `service` differs from its parent span's `service` is an edge `parent_service → child_service`. Group by the pair; compute call count, avg latency, error rate.

## 7. API

- REST under `/api`: `/api/traces`, `/api/traces/{id}`, `/api/logs`, `/api/service-map`.
- Live tail: SSE endpoint (e.g. `/api/logs/stream`) via `sse-starlette`.
- OTLP endpoints stay at `/v1/traces` and `/v1/logs`.

## 8. Frontend (React + TS)

Left sidebar: **Traces | Logs | Service Map**.

1. **Traces:** filterable list (service, errors only, min duration). Clicking a row opens the **waterfall**: nest spans by `parent_span_id`; each span is a `<div>` bar (left = start offset, width = duration), red on error, shows HTTP status; expand/collapse children; detail panel on click.
2. **Logs:** search box with full-text, regex, and tag filters; live-tail toggle (SSE); clickable `trace_id` jumps to that trace's waterfall.
3. **Service Map:** React Flow (or d3). Nodes = services, edges labeled with avg latency and error rate, red above an error threshold, time-range selector.

The frontend only fetches JSON and draws it. Consider generating TS types from FastAPI's OpenAPI schema (`openapi-typescript`) instead of hand-writing them twice.

## 9. Demo environment (required, easy to forget)

Heimdall needs something to watch. Build in `demo/`:
- 3-4 tiny fake services, e.g. `gateway → order → payment → db`. Each should produce 2-4 spans per request (handler, DB call, outgoing call) so waterfalls are interesting.
- **Mixed languages:** mostly FastAPI + OTel Python SDK, plus 1-2 Express apps with the OTel Node SDK. Proves Heimdall is truly OTel-native and makes the topology map realistic.
- A **load generator** (httpx script or Locust) firing requests continuously.
- **Fault toggles** per service, e.g. `POST /fault` to make payment slow (sleep) or fail (500). Demo story: all green, flip toggle, map edge turns red, click into the failed trace, find the slow span.
- Optional stretch: an OTel Collector between services and Heimdall with a `batch` processor, to show compatibility.

## 10. Tech stack (locked)

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web | FastAPI + uvicorn |
| OTLP decoding | `opentelemetry-proto` + `protobuf` |
| Storage | `duckdb` |
| Bulk insert | `pyarrow` |
| Queue | bounded `asyncio.Queue` |
| Models | dataclasses |
| Live tail | SSE via `sse-starlette` |
| Tests | pytest + httpx |
| Packaging | `uv` (or pip) + `pyproject.toml` |
| Frontend | React + TypeScript + Vite; React Flow for the map |

Later: `grpcio` (gRPC ingest on 4317), official `mcp` Python SDK, DuckDB SQL for anomaly math.

**Why Python over Node/TS:** protobuf decoding and DuckDB (Arrow bulk insert) are the easiest and most mature in Python, which de-risks the first slice. Main downside (duplicate types) mitigated via OpenAPI-generated TS types.

## 11. Repo layout

```
heimdall-observability/
  backend/
    heimdall/
      ingest/    decoders → normalized models → queue → writer
      store/     DuckDB schema + connection (single owner of the DB)
      queries/   list_traces, get_trace, search_logs, service_edges, percentiles
      api/       REST + SSE routes (call queries/ only)
    tests/
    pyproject.toml
  frontend/      React + TS (Vite)
  demo/          fake services + load generator + fault toggles
  DESIGN.md
  README.md
```

Docker Compose and the Dockerfile come **last** (see section 13).

## 12. Build order (pass tier)

1. **Skeleton slice:** one fake service → `POST /v1/traces` → DuckDB → plain table of spans in the UI. Everything builds on this.
   - 1a. Repo scaffold: folders, `pyproject.toml`, `.gitignore`, README stub
   - 1b. `store/schema.py`: `spans` and `logs` tables
   - 1c. `ingest/models.py`: `Span`, `LogRecord` dataclasses
   - 1d. `ingest/decoder.py`: OTLP protobuf → `Span` list (incl. gzip, resource `service.name`)
   - 1e. `ingest/pipeline.py`: bounded queue + background writer (500 / 1s flush)
   - 1f. `api/main.py`: `POST /v1/traces` + debug `GET /api/spans`
2. **Trace list + waterfall**
3. **Logs ingest (`/v1/logs`) + explorer + live tail**
4. **Topology map**
5. **Demo polish:** load generator, fault toggles, mixed-language services

**Victory-tier order (later):** DuckDB (already done) → MCP server (wrap query functions as tools) → anomaly detection (compare p99 in error-spike window vs baseline, per span name/service) → benchmarks.

## 13. Decisions that keep victory tier a plug-in

| Decision | Protects |
|---|---|
| DuckDB from day one | Columnar storage item; no migration |
| One query layer; UI/API never write SQL | MCP server and anomaly job reuse the same functions |
| Flat span storage, tree built at read time | Out-of-order arrival; fast aggregations |
| Decoder → normalized model → queue → writer | gRPC/JSON decoders added without touching downstream |
| Precompute `duration_ns`, promote hot attributes to columns | Fast p99/error queries |
| Writer is the single choke point | Throughput metrics, backpressure, live-tail broadcast attach here |

### Docker-readiness habits (do now, package later)

1. API under `/api`; OTLP paths stay `/v1/*`. Lets FastAPI serve the built UI at `/` later without route clashes.
2. Config from environment variables with defaults: DuckDB file path, port, queue max size, batch size, flush interval.
3. Frontend uses a relative `/api` path with a Vite dev proxy. No hardcoded backend URL.

During development: run `uvicorn` and `npm run dev` in two terminals. Final packaging: **one multi-stage Dockerfile** (Node builds the UI, Python image serves it with FastAPI), plus separate containers for demo services. Optional second stage of work, not a design driver.

## 14. Known gotchas

- gzip request bodies must be decompressed before protobuf decoding.
- Trace/span IDs arrive as raw bytes; hex-encode them.
- `service.name` is on the resource, not the span.
- Python OTel SDK exports protobuf (not JSON), so protobuf decoding is required from the start.
- DuckDB: one read-write process per file; blocking calls need a thread; one connection owner.
- Child spans can arrive before parents; never assume order.
- Replying 200 before flush means crash-window data loss (accepted, documented).

## 15. Killer-version extras (after pass tier works)

- **Benchmarks in the README:** ingest throughput (spans/sec), query latency at 1M and 10M spans, storage per span, compared against a row store (SQLite/Postgres) to show why columnar wins.
- **Defensible engineering decisions** for the viva: batching and backpressure, out-of-order trace assembly, tail-based sampling (keep all errors/slow traces, sample the rest), DuckDB schema/index choices.
- **Standards compliance:** protobuf and gRPC, and proof that stock SDKs and the OTel Collector work with zero changes.
- **Dogfooding:** instrument the owner's LangGraph-based travel app to trace agent nodes, LLM calls, latency, and token counts.
- **Headline demo:** break something on purpose, then an AI assistant over MCP finds the root cause ("why did itinerary generation fail at 10:42?").
- **Packaging:** one-command `docker compose up`, hosted demo, architecture diagram, tests, short write-up of the hardest tradeoff.

## 16. Open items

- **Writer-crash question (owner to answer):** if the writer crashes mid-flush, which data is lost, and which stage would you change to reduce that loss? Expected reasoning: queue and unflushed buffer are memory-only and lost (worst case about 1s of data); a transaction prevents a half-written batch; reducing loss costs ingest speed or complexity (e.g. a write-ahead log or smaller flush interval).
- **Event rules:** confirm with organizers whether "clear, incremental git commits" applies to this category. That requirement is on the Open-category sheet (#01), not #08. Incremental commits per build step are good practice regardless.
- **Pre-reading (optional, about 2 hours):** OpenTelemetry basics (spans, `traceparent` context propagation), the OTLP spec at opentelemetry.io, the `trace.proto` files in `open-telemetry/opentelemetry-proto`, and a hands-on run of Jaeger all-in-one with a tiny instrumented app. Verify exact URLs, as they were not checked.
- **Next step to start:** 1a-1c (scaffold, schema, models), outlining each file before writing it.
