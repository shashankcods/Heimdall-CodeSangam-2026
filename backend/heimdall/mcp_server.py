"""MCP server: lets AI assistants investigate Heimdall's telemetry.

Every tool is a thin, read-only wrapper over the query layer (heimdall/queries/),
so an assistant sees exactly what the UI sees and no new SQL lives here.

Tool design for an AI caller rather than a browser:
- time ranges are a `minutes` lookback instead of epoch milliseconds,
- results carry readable ISO timestamps next to the millisecond values,
- default limits are small to save the assistant's context window,
- get_trace hides the SDK's internal "http send/receive" spans unless asked.

Served over streamable HTTP at /mcp by the FastAPI app (heimdall/api/main.py),
inside the same process that owns the DuckDB connection.
"""

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # message is shown to the AI caller

from heimdall.queries.logs import InvalidSearch, search_logs as query_logs
from heimdall.queries.topology import service_map
from heimdall.queries.traces import get_trace as query_trace, list_services as query_services, list_traces

INSTRUCTIONS = """Heimdall stores OpenTelemetry traces and logs from a set of services.
To investigate a problem: get_service_map shows which service-to-service calls are failing
or slow; search_traces finds failing or slow requests; get_trace shows where inside one
request the time or the error was; search_logs (filter by trace_id) shows why."""

MAX_TRACES = 50
MAX_LOGS = 200
INTERNAL_SPAN_SUFFIXES = (" http send", " http receive")


def _iso(ms: float | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def _window(minutes: float) -> dict[str, float]:
    now_ms = time.time() * 1000
    return {"from_ms": now_ms - minutes * 60_000, "to_ms": now_ms}


def create_mcp_server(get_conn: Callable[[], duckdb.DuckDBPyConnection]) -> MCPServer:
    """Build the MCP server. `get_conn` returns the app's shared DuckDB connection."""
    server = MCPServer("Heimdall", instructions=INSTRUCTIONS, version="0.1.0")

    def with_cursor(fn: Callable[[duckdb.DuckDBPyConnection], Any]) -> Any:
        cur = get_conn().cursor()  # one cursor per call; tools run in worker threads
        try:
            return fn(cur)
        finally:
            cur.close()

    @server.tool()
    def list_services(minutes: float = 60) -> dict[str, Any]:
        """List the services that sent telemetry in the last `minutes`."""
        return {"services": with_cursor(lambda cur: query_services(cur, **_window(minutes)))}

    @server.tool()
    def search_traces(
        service: str | None = None,
        errors_only: bool = False,
        min_duration_ms: float | None = None,
        minutes: float = 60,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find recent requests (traces), newest first.

        Use errors_only=true to find failing requests and min_duration_ms to find slow ones.
        `service` matches traces that touched that service anywhere, not only at the root.
        """
        traces = with_cursor(lambda cur: list_traces(
            cur, service=service, errors_only=errors_only, min_duration_ms=min_duration_ms,
            limit=max(1, min(limit, MAX_TRACES)), **_window(minutes),
        ))
        for t in traces:
            t["start_time"] = _iso(t["start_ms"])
        return {"traces": traces}

    @server.tool()
    def get_trace(trace_id: str, include_internal_spans: bool = False) -> dict[str, Any]:
        """Show every span of one request, to see where its time went or where it failed.

        Spans are a flat list sorted by start time; parent_span_id links them into a tree.
        start_offset_ms is relative to the start of the trace. Internal SDK spans
        ("... http send/receive") are hidden unless include_internal_spans=true.
        """
        trace = with_cursor(lambda cur: query_trace(cur, trace_id.strip().lower()))
        if trace is None:
            raise ToolError(f"trace {trace_id} not found")
        spans, hidden = [], 0
        for s in trace["spans"]:
            if not include_internal_spans and s["name"].endswith(INTERNAL_SPAN_SUFFIXES):
                hidden += 1
                continue
            s["start_offset_ms"] = round(s["start_ms"] - trace["start_ms"], 3)
            spans.append(s)
        return {
            "trace_id": trace["trace_id"],
            "start_time": _iso(trace["start_ms"]),
            "duration_ms": trace["duration_ms"],
            "span_count": len(spans),
            "hidden_internal_spans": hidden,
            "spans": spans,
        }

    @server.tool()
    def search_logs(
        query: str | None = None,
        regex: bool = False,
        service: str | None = None,
        severity: str | None = None,
        trace_id: str | None = None,
        minutes: float = 60,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search recent logs, newest first.

        `query` is case-insensitive text (or a regular expression with regex=true).
        `severity` is a minimum level: TRACE, DEBUG, INFO, WARN, ERROR or FATAL.
        Pass a trace_id to read every log written while handling that request.
        """
        try:
            logs = with_cursor(lambda cur: query_logs(
                cur, q=query, regex=regex, service=service, severity=severity, trace_id=trace_id,
                limit=max(1, min(limit, MAX_LOGS)), **_window(minutes),
            ))
        except InvalidSearch as e:
            raise ToolError(str(e)) from e
        for log in logs:
            log["time"] = _iso(log["timestamp_ms"])
        return {"logs": logs}

    @server.tool()
    def get_service_map(minutes: float = 15) -> dict[str, Any]:
        """Show which services call which, with call counts, latency and error rates.

        An edge source -> target is calls from source into target; its error_rate (0-1)
        and latency describe how target behaved. Node numbers count requests each service
        handled. High error_rate edges are where to look first.
        """
        return with_cursor(lambda cur: service_map(cur, **_window(minutes)))

    return server
