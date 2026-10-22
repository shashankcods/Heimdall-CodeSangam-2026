import json
from dataclasses import fields

import pytest

from heimdall.ingest.models import LogRecord, Span
from heimdall.store.schema import connect


def table_columns(conn, table):
    return [row[0] for row in conn.execute(f"SELECT * FROM {table} LIMIT 0").description]


@pytest.mark.parametrize("model, table", [(Span, "spans"), (LogRecord, "logs")])
def test_model_fields_match_table_columns(tmp_path, model, table):
    conn = connect(str(tmp_path / "test.duckdb"))
    assert [f.name for f in fields(model)] == table_columns(conn, table)


def make_span(**overrides):
    values = dict(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
        service="gateway",
        name="POST /order",
        start_time=1_000,
        end_time=3_500,
        duration_ns=2_500,
    )
    values.update(overrides)
    return Span(**values)


def test_span_optional_fields_default_to_none():
    span = make_span()
    assert span.parent_span_id is None
    assert span.http_status is None
    assert span.attributes == {}


def test_span_to_row_serializes_attributes():
    row = make_span(attributes={"http.method": "POST"}).to_row()
    assert list(row) == [f.name for f in fields(Span)]
    assert json.loads(row["attributes"]) == {"http.method": "POST"}


def test_span_row_inserts_into_duckdb(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    row = make_span(http_status=500).to_row()
    conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))
    assert conn.execute("SELECT service, http_status FROM spans").fetchone() == ("gateway", 500)


def test_log_record_to_row():
    log = LogRecord(timestamp=1_000, service="payment", severity="ERROR", body="Stripe timeout")
    row = log.to_row()
    assert row["trace_id"] is None
    assert row["attributes"] == "{}"
