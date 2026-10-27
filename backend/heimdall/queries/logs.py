"""Log read queries: search with text/regex and filters.

- Text search is a case-insensitive substring match on the body.
- Regex search uses DuckDB's RE2 engine; prefix with (?i) for case-insensitive.
- Severity is a MINIMUM level: WARN returns WARN, ERROR and FATAL.
- Tags filter on attributes: every (key, value) pair must match. Values compare
  as text, so a numeric attribute retry=0 matches the tag ("retry", "0").
- Times leave the database as milliseconds (float); see queries/spans.py.
"""

import json
from typing import Any

import duckdb

from heimdall.queries.common import rows_as_dicts, time_range_ns

SEVERITY_ORDER = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")


class InvalidSearch(ValueError):
    """The search can't run as given (bad regex or severity). Maps to HTTP 400."""


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
    if severity:
        level = severity.strip().upper()
        if level not in SEVERITY_ORDER:
            raise InvalidSearch(f"unknown severity {severity!r}; use one of {', '.join(SEVERITY_ORDER)}")
        where.append("list_contains($levels, severity)")
        params["levels"] = list(SEVERITY_ORDER[SEVERITY_ORDER.index(level):])
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
