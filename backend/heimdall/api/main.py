"""Heimdall HTTP app: OTLP ingest under /v1, query API under /api.

Run:  uv run uvicorn heimdall.api.main:app --port 4318

One process owns the DuckDB connection (DuckDB allows one read-write process per
file); request handlers get their own cursor from it.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from heimdall.ingest.decoder import InvalidPayload, decode_logs, decode_traces
from heimdall.ingest.pipeline import BatchWriter, QueueFull
from heimdall.queries.logs import InvalidSearch, search_logs
from heimdall.queries.spans import recent_spans
from heimdall.queries.traces import get_trace, list_services, list_traces
from heimdall.store.schema import connect

PROTOBUF = "application/x-protobuf"
EMPTY_TRACE_RESPONSE = ExportTraceServiceResponse().SerializeToString()
EMPTY_LOGS_RESPONSE = ExportLogsServiceResponse().SerializeToString()
MAX_SPANS_LIMIT = 1000
MAX_TRACES_LIMIT = 500
MAX_LOGS_LIMIT = 1000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    conn = connect()
    # One writer per table: separate queues, so a flood of logs can't block traces.
    span_writer = BatchWriter(conn, "spans")
    log_writer = BatchWriter(conn, "logs")
    span_writer.start()
    log_writer.start()
    app.state.conn = conn
    app.state.span_writer = span_writer
    app.state.log_writer = log_writer
    try:
        yield
    finally:
        # Flush what's still queued before exiting.
        await span_writer.close()
        await log_writer.close()
        conn.close()


app = FastAPI(title="Heimdall", lifespan=lifespan)


def get_cursor(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    cur = request.app.state.conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


# --- OTLP ingest -----------------------------------------------------------

async def _ingest(
    request: Request,
    decode: Callable[[bytes, str | None], list],
    writer: BatchWriter,
    empty_response: bytes,
) -> Response:
    """Shared OTLP/HTTP handling: content type -> decode -> queue -> reply."""
    if "json" in request.headers.get("content-type", ""):
        return Response("OTLP/JSON is not supported yet; send application/x-protobuf",
                        status_code=415)
    body = await request.body()
    try:
        records = decode(body, request.headers.get("content-encoding"))
    except InvalidPayload as e:
        return Response(str(e), status_code=400)
    try:
        writer.submit(records)
    except QueueFull:
        return Response(status_code=429, headers={"Retry-After": "1"})
    return Response(empty_response, media_type=PROTOBUF)


# async on purpose: submit() touches an asyncio.Queue, which must be used from
# the event loop thread, not from FastAPI's sync-handler thread pool.
@app.post("/v1/traces")
async def ingest_traces(request: Request) -> Response:
    return await _ingest(request, decode_traces, request.app.state.span_writer, EMPTY_TRACE_RESPONSE)


@app.post("/v1/logs")
async def ingest_logs(request: Request) -> Response:
    return await _ingest(request, decode_logs, request.app.state.log_writer, EMPTY_LOGS_RESPONSE)


# --- Query API -------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/services")
def services(
    from_ms: float | None = None,
    to_ms: float | None = None,
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> dict:
    return {"services": list_services(cur, from_ms=from_ms, to_ms=to_ms)}


@app.get("/api/traces")
def traces(
    service: str | None = None,
    errors_only: bool = False,
    min_duration_ms: float | None = Query(None, ge=0),
    from_ms: float | None = None,
    to_ms: float | None = None,
    limit: int = Query(50, ge=1),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> dict:
    return {"traces": list_traces(
        cur,
        service=service,
        errors_only=errors_only,
        min_duration_ms=min_duration_ms,
        from_ms=from_ms,
        to_ms=to_ms,
        limit=min(limit, MAX_TRACES_LIMIT),
    )}


@app.get("/api/traces/{trace_id}")
def trace(trace_id: str, cur: duckdb.DuckDBPyConnection = Depends(get_cursor)) -> dict:
    result = get_trace(cur, trace_id.lower())
    if result is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return result


@app.get("/api/logs")
def logs(
    q: str | None = None,
    regex: bool = False,
    service: str | None = None,
    severity: str | None = None,
    trace_id: str | None = None,
    tag: list[str] = Query(default=[]),
    from_ms: float | None = None,
    to_ms: float | None = None,
    limit: int = Query(100, ge=1),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> dict:
    """Log search. Repeat ?tag=key:value to filter on attributes (all must match)."""
    tags = []
    for t in tag:
        key, sep, value = t.partition(":")  # split on the FIRST colon; values may contain more
        if not sep or not key:
            raise HTTPException(status_code=400, detail=f"tag must look like key:value, got {t!r}")
        tags.append((key, value))
    try:
        return {"logs": search_logs(
            cur,
            q=q,
            regex=regex,
            service=service,
            severity=severity,
            trace_id=trace_id,
            tags=tags,
            from_ms=from_ms,
            to_ms=to_ms,
            limit=min(limit, MAX_LOGS_LIMIT),
        )}
    except InvalidSearch as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/spans")
def debug_spans(
    limit: int = Query(100, ge=1),
    cur: duckdb.DuckDBPyConnection = Depends(get_cursor),
) -> dict:
    """Debug only (skeleton slice). Replaced by /api/traces in step 2."""
    return {"spans": recent_spans(cur, min(limit, MAX_SPANS_LIMIT))}
