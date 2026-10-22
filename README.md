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
```
