"""Log read queries: search with text/regex and filters.

- Text search is a case-insensitive substring match on the body.
- Regex search uses DuckDB's RE2 engine; prefix with (?i) for case-insensitive.
- Severity is a MINIMUM level: WARN returns WARN, ERROR and FATAL.
- Tags filter on attributes: every (key, value) pair must match. Values compare
  as text, so a numeric attribute retry=0 matches the tag ("retry", "0").
- Times leave the database as milliseconds (float); see queries/spans.py.

LogFilter applies the same filters in Python, for the live tail: new logs are
matched as they're written instead of being queried from the table.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import duckdb

from heimdall.ingest.models import LogRecord
from heimdall.queries.common import rows_as_dicts, time_range_ns

SEVERITY_ORDER = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")


class InvalidSearch(ValueError):
    """The search can't run as given (bad regex or severity). Maps to HTTP 400."""


def _min_levels(severity: str | None) -> tuple[str, ...] | None:
    """'WARN' -> ('WARN', 'ERROR', 'FATAL'); None -> no severity filter."""
    if not severity:
        return None
    level = severity.strip().upper()
    if level not in SEVERITY_ORDER:
        raise InvalidSearch(f"unknown severity {severity!r}; use one of {', '.join(SEVERITY_ORDER)}")
    return SEVERITY_ORDER[SEVERITY_ORDER.index(level):]


def _as_text(value: Any) -> str:
    # Matches DuckDB's json_extract_string: strings as-is, everything else as JSON (0 -> "0", True -> "true").
    return value if isinstance(value, str) else json.dumps(value)


def log_to_api(record: LogRecord) -> dict[str, Any]:
    """A just-written LogRecord in the same shape as one item of GET /api/logs."""
    return {
        "timestamp_ms": record.timestamp / 1e6,
        "service": record.service,
        "severity": record.severity,
        "body": record.body,
        "trace_id": record.trace_id,
        "span_id": record.span_id,
        "attributes": record.attributes,
    }


@dataclass
class LogFilter:
    """The /api/logs filters (minus time range and limit), checked in Python."""

    q: str | None = None
    regex: bool = False
    service: str | None = None
    severity: str | None = None
    trace_id: str | None = None
    tags: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Validate up front so a bad filter is a 400 when the stream opens, not a silent no-match.
        self._levels = _min_levels(self.severity)
        self._pattern = None
        if self.q and self.regex:
            try:
                self._pattern = re.compile(self.q)
            except re.error as e:
                raise InvalidSearch(f"invalid regex: {e}") from e
        self._trace_id = self.trace_id.lower() if self.trace_id else None

    def matches(self, record: LogRecord) -> bool:
        body = record.body or ""
        if self.q:
            if self._pattern is not None:
                if not self._pattern.search(body):
                    return False
            elif self.q.lower() not in body.lower():
                return False
        if self.service and record.service != self.service:
            return False
        if self._levels is not None and record.severity not in self._levels:
            return False
        if self._trace_id and record.trace_id != self._trace_id:
            return False
        for key, value in self.tags:
            if key not in record.attributes or _as_text(record.attributes[key]) != value:
                return False
        return True


def _json_path(key: str) -> str:
    # Quote the key so dotted attribute names like http.method aren't read as nesting.
    return '$."' + key.replace('"', '\\"') + '"'


def search_logs(
    cur: duckdb.DuckDBPyConnection,
    *,
    q: str | None = None,
    regex: bool = False,
    service: str | None = None,
    severity: str | None = None,
    trace_id: str | None = None,
    tags: list[tuple[str, str]] = (),
    from_ms: float | None = None,
    to_ms: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Matching logs, newest first."""
    from_ns, to_ns = time_range_ns(from_ms, to_ms)
    where = ["timestamp >= $from_ns", "timestamp <= $to_ns"]
    params: dict[str, Any] = {"from_ns": from_ns, "to_ns": to_ns, "limit": limit}

    if q:
        where.append("regexp_matches(body, $q)" if regex else "contains(lower(body), lower($q))")
        params["q"] = q
    if service:
        where.append("service = $service")
        params["service"] = service
    levels = _min_levels(severity)
    if levels:
        where.append("list_contains($levels, severity)")
        params["levels"] = list(levels)
    if trace_id:
        where.append("trace_id = $trace_id")
        params["trace_id"] = trace_id.lower()
    for i, (key, value) in enumerate(tags):
        where.append(f"json_extract_string(attributes, $tag_path_{i}) = $tag_value_{i}")
        params[f"tag_path_{i}"] = _json_path(key)
        params[f"tag_value_{i}"] = value

    sql = f"""
        SELECT timestamp / 1e6 AS timestamp_ms, service, severity, body, trace_id, span_id, attributes
        FROM logs
        WHERE {' AND '.join(where)}
        ORDER BY timestamp DESC
        LIMIT $limit
    """
    try:
        cur.execute(sql, params)
    except duckdb.InvalidInputException as e:
        if regex:  # the only user-supplied input that can make the SQL itself invalid
            raise InvalidSearch(f"invalid regex: {str(e).splitlines()[0].split(': ', 1)[-1]}") from e
        raise
    logs = rows_as_dicts(cur)
    for log in logs:
        log["attributes"] = json.loads(log["attributes"]) if log["attributes"] else {}
    return logs
