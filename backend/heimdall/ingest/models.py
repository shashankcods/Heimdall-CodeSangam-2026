"""Normalized telemetry records: what every decoder produces and the writer stores.

Field names and ORDER must match the columns in store/schema.py exactly, because
the writer bulk-inserts with `INSERT INTO spans SELECT * FROM batch`, which
matches columns by position. tests/test_models.py enforces this.

kw_only=True lets nullable fields have defaults while keeping column order
(normally Python forbids a defaulted field before a required one).
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True, kw_only=True)
class Span:
    trace_id: str                       # 32 hex chars
    span_id: str                        # 16 hex chars
    parent_span_id: str | None = None   # None for a root span
    service: str
    name: str
    kind: str | None = None             # INTERNAL / SERVER / CLIENT / PRODUCER / CONSUMER
    start_time: int                     # unix nanoseconds
    end_time: int
    duration_ns: int
    status_code: str | None = None      # UNSET / OK / ERROR
    status_msg: str | None = None
    http_status: int | None = None
    route: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Column-ordered dict ready for a pyarrow/DuckDB insert."""
        row = asdict(self)
        row["attributes"] = json.dumps(self.attributes)
        return row


@dataclass(slots=True, kw_only=True)
class LogRecord:
    timestamp: int                      # unix nanoseconds
    service: str
    severity: str | None = None         # TRACE / DEBUG / INFO / WARN / ERROR / FATAL
    body: str | None = None
    trace_id: str | None = None         # None when not tied to a request
    span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """Column-ordered dict ready for a pyarrow/DuckDB insert."""
        row = asdict(self)
        row["attributes"] = json.dumps(self.attributes)
        return row

    def to_dict(self) -> dict[str, Any]:
        """User-facing dictionary matching /api/logs representation."""
        return {
            "timestamp_ms": self.timestamp / 1e6,
            "timestamp": self.timestamp / 1e6,
            "service": self.service,
            "severity": self.severity,
            "body": self.body,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "attributes": self.attributes,
        }
