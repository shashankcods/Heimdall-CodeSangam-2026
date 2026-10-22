"""DuckDB schema for Heimdall and the single place that opens the database.

Type choices (see DESIGN.md section 5):
- IDs are lowercase hex VARCHAR (trace_id 32 chars, span_id 16 chars).
- Times are BIGINT nanoseconds since the Unix epoch, exactly as OTLP sends them.
- Span kind / status are readable strings ('SERVER', 'ERROR'), not enum ints.
- No primary key: keeps bulk inserts fast; duplicates are handled at read time.
"""

import os

import duckdb

DEFAULT_DB_PATH = "heimdall.duckdb"

SPANS_DDL = """
CREATE TABLE IF NOT EXISTS spans (
    trace_id        VARCHAR NOT NULL,
    span_id         VARCHAR NOT NULL,
    parent_span_id  VARCHAR,            -- NULL for a root span
    service         VARCHAR NOT NULL,   -- copied down from resource service.name
    name            VARCHAR NOT NULL,
    kind            VARCHAR,            -- INTERNAL / SERVER / CLIENT / PRODUCER / CONSUMER
    start_time      BIGINT  NOT NULL,   -- unix nanoseconds
    end_time        BIGINT  NOT NULL,
    duration_ns     BIGINT  NOT NULL,   -- end_time - start_time, precomputed
    status_code     VARCHAR,            -- UNSET / OK / ERROR
    status_msg      VARCHAR,
    http_status     INTEGER,            -- promoted from http.status_code
    route           VARCHAR,            -- promoted from http.route
    attributes      JSON                -- full span attribute set
)
"""

LOGS_DDL = """
CREATE TABLE IF NOT EXISTS logs (
    timestamp   BIGINT  NOT NULL,       -- unix nanoseconds
    service     VARCHAR NOT NULL,
    severity    VARCHAR,                -- TRACE / DEBUG / INFO / WARN / ERROR / FATAL
    body        VARCHAR,
    trace_id    VARCHAR,                -- NULL when the log isn't tied to a request
    span_id     VARCHAR,
    attributes  JSON
)
"""


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all tables if they don't exist yet. Safe to call repeatedly."""
    conn.execute(SPANS_DDL)
    conn.execute(LOGS_DDL)


def connect(path: str | None = None) -> duckdb.DuckDBPyConnection:
    """Open the Heimdall database and make sure the schema exists.

    Path order: explicit argument, then HEIMDALL_DB_PATH env var, then the default.
    DuckDB allows one read-write process per file, so the backend calls this once
    and hands out conn.cursor() to other threads.
    """
    path = path or os.environ.get("HEIMDALL_DB_PATH", DEFAULT_DB_PATH)
    conn = duckdb.connect(path)
    init_schema(conn)
    return conn
