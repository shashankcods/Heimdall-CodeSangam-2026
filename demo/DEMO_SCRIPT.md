# Heimdall 3-Minute Live Demo Playbook

This script is the exact step-by-step walkthrough for project demonstrations, recorded backups, and jury vivas.

---

## 1. Pre-Flight Setup (Before the Demo)

Open 3 terminal windows:

### Terminal 1: Heimdall Backend
```bash
cd backend
uv sync
uv run uvicorn heimdall.api.main:app --port 4318
```
*Verify: http://localhost:4318/api/health returns `{"status":"ok"}`.*

### Terminal 2: Heimdall Frontend
```bash
cd frontend
npm run dev
```
*Verify: Open http://localhost:5173 in the browser (Traces | Logs | Service Map).*

### Terminal 3: Microservice Cluster & Load Generator
```bash
uv run demo/run_demo.py
```
*Starts `gateway` (:8001), `order-service` (:8002), and `payment-service` (:8003) with a 2 req/s continuous load generator.*

---

## 2. The 3-Minute Presentation Storyline

### Minute 1: The Healthy Baseline & Ingestion Architecture
- **Action**: Show browser on `http://localhost:5173`. Click **Service Map** or **Traces**.
- **Talking Point**:
  > *"Heimdall is an OpenTelemetry-native observability engine. Our demo cluster (gateway, order, payment) is actively emitting standard OTLP binary protobuf traces and logs to Heimdall. Notice how our in-memory bounded queue and DuckDB batch writer flush records every 500 spans or 1 second with sub-millisecond write times."*
- **Action**: Click a trace row on the **Traces** page to open the **Waterfall view**.
- **Talking Point**:
  > *"Every trace is assembled at read time from flat DuckDB columnar storage. You can see the full request journey: the HTTP handler in gateway, the database INSERT in order-service, and the Stripe card charge in payment-service."*

---

### Minute 2: Chaos & Fault Injection
- **Action**: Switch to Terminal 3 (where `run_demo.py` is running).
- **Command**: Type `f` and press `Enter` (or run `curl -X POST http://localhost:8003/fault -d '{"latency_ms":1900,"error_rate":1.0,"error_status":504}' -H "Content-Type: application/json"`).
- **Talking Point**:
  > *"Now let's simulate a live production outage. We just injected a 1.9-second latency and 504 Gateway Timeout fault into the payment service."*
- **Action**: Refresh or view the **Service Map**.
- **Talking Point**:
  > *"Instantly, the `order-service -> payment-service` edge turns red as error rates climb and average latency spikes from ~30ms to nearly 2 seconds."*

---

### Minute 3: Root-Cause Investigation & Log Correlation
- **Action**: Click **Traces** page, filter by **Errors Only**.
- **Action**: Click on the latest failed trace (marked with a red badge).
- **Talking Point**:
  > *"In the waterfall view, we immediately spot the culprit: `chargeCard` took 1.9s and returned HTTP 504. The parent span in order-service failed with a 500 cascade as a result."*
- **Action**: Copy the `trace_id` and open the **Logs** page (or click the log link).
- **Talking Point**:
  > *"Observability is incomplete without logs. Because Heimdall uses the OTel logging bridge, every log carries the exact `trace_id` and `span_id`. Here is the error log: `Stripe gateway timeout after 1.9s, retry 0`. In under 60 seconds, we pinpointed the exact service, span, duration, and root cause."*
- **Action**: Switch to Terminal 3, type `r` and press `Enter` to reset the fault back to healthy.

---

## 3. Anticipated Viva / Defense Questions

| Question | Defensible Answer |
|---|---|
| **Why DuckDB instead of Postgres / MongoDB?** | Observability is write-heavy, append-only, and analytical. DuckDB's columnar layout gives us 10x-50x faster aggregations and joins (`quantile_cont`, topology self-joins) and 1000x better compression than row stores without managing a heavy external database. |
| **How do you handle traffic spikes?** | We decouple ingestion from storage using an in-memory bounded queue (`asyncio.Queue`). When the queue fills, we reject incoming requests with HTTP `429 Too Many Requests` (`Retry-After: 1`), providing backpressure so the server never runs out of memory. |
| **What happens if child spans arrive before parents?** | Spans are stored strictly flat in DuckDB. The parent-child tree is assembled dynamically on read by matching `parent_span_id` &rarr; `span_id`. Out-of-order arrival never breaks storage or visualization. |
| **Why does DuckDB run in the same process?** | DuckDB is an embedded engine allowing only one read-write process per file. All ingest, writer, and query services live in one backend process sharing cursors safely across threads. |
