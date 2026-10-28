"""Response models for the /api endpoints.

They describe exactly what each endpoint returns, so FastAPI's OpenAPI schema
(/openapi.json, the /docs page) lists real fields and types. The frontend can
generate its TypeScript types from that schema instead of keeping a hand-written
copy, and FastAPI checks every response against these shapes.

Times are milliseconds since the Unix epoch (float). Missing values are null.
"""

from typing import Any, Literal

from pydantic import BaseModel

SpanKind = Literal["INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
StatusCode = Literal["UNSET", "OK", "ERROR"]
Severity = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]


class Health(BaseModel):
    status: str


class ErrorDetail(BaseModel):
    detail: str


class ServiceList(BaseModel):
    services: list[str]


# --- traces -------------------------------------------------------------------

class TraceSummary(BaseModel):
    """One row of the trace list. Root fields come from the root span, or the earliest span."""
    trace_id: str
    root_service: str
    root_name: str
    start_ms: float
    duration_ms: float
    span_count: int
    service_count: int
    error_count: int
    has_error: bool
    http_status: int | None


class TraceList(BaseModel):
    traces: list[TraceSummary]


class SpanDetail(BaseModel):
    """One span of a trace. The tree is built from parent_span_id (null for a root)."""
    span_id: str
    parent_span_id: str | None
    service: str
    name: str
    kind: SpanKind | None
    start_ms: float
    end_ms: float
    duration_ms: float
    status_code: StatusCode | None
    status_msg: str | None
    http_status: int | None
    route: str | None
    attributes: dict[str, Any]


class TraceDetail(BaseModel):
    """Every span of one trace as a flat list sorted by start."""
    trace_id: str
    start_ms: float
    duration_ms: float
    spans: list[SpanDetail]


# --- logs -----------------------------------------------------------------------

class LogEntry(BaseModel):
    """One log line. Also the JSON payload of each `log` event on /api/logs/stream."""
    timestamp_ms: float
    service: str
    severity: Severity | None
    body: str | None
    trace_id: str | None
    span_id: str | None
    attributes: dict[str, Any]


class LogList(BaseModel):
    logs: list[LogEntry]


# --- service map ------------------------------------------------------------------

class ServiceNode(BaseModel):
    """A service. Counts are requests it handled (entry spans), not raw spans."""
    id: str
    request_count: int
    error_count: int
    error_rate: float
    avg_duration_ms: float | None
    p95_duration_ms: float | None


class ServiceEdge(BaseModel):
    """Calls from source into target; latency and errors describe how target behaved."""
    source: str
    target: str
    call_count: int
    avg_duration_ms: float
    p95_duration_ms: float
    error_count: int
    error_rate: float


class ServiceMap(BaseModel):
    nodes: list[ServiceNode]
    edges: list[ServiceEdge]


# --- anomalies --------------------------------------------------------------------

class TimeWindow(BaseModel):
    from_ms: float
    to_ms: float


class Anomaly(BaseModel):
    """An operation whose recent p99 latency jumped versus its baseline."""
    service: str
    name: str
    recent_p99_ms: float
    baseline_p99_ms: float
    p99_ratio: float
    recent_count: int
    baseline_count: int
    recent_error_rate: float
    baseline_error_rate: float
    error_spike: bool
    example_trace_id: str | None


class AnomalyReport(BaseModel):
    recent_window: TimeWindow
    baseline_window: TimeWindow
    anomalies: list[Anomaly]
