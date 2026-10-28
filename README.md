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

Settings (environment variables): `HEIMDALL_DB_PATH` (default `heimdall.duckdb`), `HEIMDALL_MAX_PENDING` (100000), `HEIMDALL_BATCH_SIZE` (500), `HEIMDALL_FLUSH_INTERVAL` (1.0 s), `HEIMDALL_MAX_LIVE_VIEWERS` (50).

## Ask an AI assistant (MCP)

The backend also serves an MCP server at `http://localhost:4318/mcp` (streamable HTTP), so an AI assistant can investigate your telemetry. Tools: `list_services`, `search_traces`, `get_trace`, `search_logs`, `get_service_map`, all read-only.

With Claude Code:

```bash
claude mcp add --transport http heimdall http://localhost:4318/mcp
```

Then ask something like "why are orders failing?". For security the server only accepts requests addressed to `localhost` / `127.0.0.1`.
