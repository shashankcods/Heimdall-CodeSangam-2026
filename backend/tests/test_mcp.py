import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError

from heimdall.api.main import app
from heimdall.ingest.models import LogRecord, Span
from heimdall.mcp_server import create_mcp_server
from heimdall.store.schema import connect

MS = 1_000_000
T_FAIL, T_OK, T_OLD = "a" * 32, "b" * 32, "c" * 32


def span(trace_id, span_id, parent, service, name, offset_ms, dur_ms, base_ns, kind="SERVER",
         status="OK", http_status=None):
    start = base_ns + offset_ms * MS
    return Span(trace_id=trace_id, span_id=span_id, parent_span_id=parent, service=service, name=name,
                kind=kind, start_time=start, end_time=start + dur_ms * MS, duration_ns=dur_ms * MS,
                status_code=status, http_status=http_status)


def order(trace_id, base_ns, fail):
    st = "ERROR" if fail else "OK"
    return [
        span(trace_id, "g1", None, "gateway", "POST /order", 0, 2300 if fail else 400, base_ns, status=st,
             http_status=500 if fail else 200),
        span(trace_id, "o1", "g1", "order-service", "POST /orders", 10, 2200 if fail else 300, base_ns, status=st),
        span(trace_id, "p1", "o1", "payment-service", "POST /charge", 300, 1900 if fail else 20, base_ns,
             status=st, http_status=504 if fail else 200),
        span(trace_id, "p2", "p1", "payment-service", "chargeCard", 300, 1900 if fail else 20, base_ns,
             kind="CLIENT", status=st),
        span(trace_id, "p3", "p1", "payment-service", "POST /charge http send", 2200, 0, base_ns,
             kind="INTERNAL", status=st),
    ]


def insert(conn, records, table):
    for r in records:
        row = r.to_row()
        conn.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "mcp.duckdb"))
    now = time.time_ns()
    insert(c, order(T_FAIL, now - 5_000 * MS, fail=True)
           + order(T_OK, now - 10_000 * MS, fail=False)
           + order(T_OLD, now - 2 * 3600 * 1000 * MS, fail=True), "spans")  # 2 hours ago
    insert(c, [
        LogRecord(timestamp=now - 4_000 * MS, service="payment-service", severity="INFO",
                  body="calling Stripe, amount=49.99", trace_id=T_FAIL, span_id="p2"),
        LogRecord(timestamp=now - 3_000 * MS, service="payment-service", severity="ERROR",
                  body="Stripe gateway timeout after 1.9s", trace_id=T_FAIL, span_id="p2"),
    ], "logs")
    return c


@pytest.fixture
def call(conn):
    server = create_mcp_server(lambda: conn)

    def _call(name, **arguments):
        return asyncio.run(server.call_tool(name, arguments)).structured_content

    return _call


def test_list_services(call):
    assert call("list_services") == {"services": ["gateway", "order-service", "payment-service"]}


def test_search_traces_finds_failures_with_readable_times(call):
    traces = call("search_traces", errors_only=True)["traces"]
    assert [t["trace_id"] for t in traces] == [T_FAIL]  # the 2-hour-old failure is outside 60 min
    assert traces[0]["http_status"] == 500
    assert traces[0]["start_time"].endswith("+00:00")


def test_search_traces_lookback_and_limit(call):
    assert len(call("search_traces", minutes=180)["traces"]) == 3
    assert len(call("search_traces", minutes=180, limit=1)["traces"]) == 1
    assert len(call("search_traces", minutes=180, limit=10_000)["traces"]) == 3  # capped, not an error


def test_get_trace_hides_internal_spans_by_default(call):
    trace = call("get_trace", trace_id=T_FAIL)
    assert [s["name"] for s in trace["spans"]] == ["POST /order", "POST /orders", "POST /charge", "chargeCard"]
    assert trace["hidden_internal_spans"] == 1
    charge = next(s for s in trace["spans"] if s["name"] == "chargeCard")
    assert (charge["status_code"], charge["duration_ms"], charge["start_offset_ms"]) == ("ERROR", 1900.0, 300.0)


def test_get_trace_can_include_internal_spans(call):
    trace = call("get_trace", trace_id=T_FAIL.upper(), include_internal_spans=True)
    assert trace["span_count"] == 5 and trace["hidden_internal_spans"] == 0


def test_get_trace_unknown_id_gives_readable_error(call):
    with pytest.raises(ToolError, match="not found"):
        call("get_trace", trace_id="f" * 32)


def test_search_logs_for_a_trace(call):
    logs = call("search_logs", trace_id=T_FAIL, severity="ERROR")["logs"]
    assert [l["body"] for l in logs] == ["Stripe gateway timeout after 1.9s"]
    assert logs[0]["time"].endswith("+00:00")


def test_search_logs_bad_regex_gives_readable_error(call):
    with pytest.raises(ToolError, match="invalid regex"):
        call("search_logs", query="missing (", regex=True)


def test_get_service_map_shows_the_failing_edge(call):
    result = call("get_service_map", minutes=5)
    pay = next(e for e in result["edges"] if e["target"] == "payment-service")
    assert pay["source"] == "order-service"
    assert pay["call_count"] == 2 and pay["error_rate"] == 0.5


def test_find_anomalies_tool(conn, call):
    now = time.time_ns()
    baseline = [span("d" * 32, f"b{i:015x}", None, "payment-service", "chargeCard", 0, 25,
                     now - (30 * 60_000 + i * 1000) * MS) for i in range(12)]
    recent = [span("e" * 32, f"r{i:015x}", None, "payment-service", "chargeCard", 0, 1900,
                   now - (60_000 + i * 1000) * MS, status="ERROR") for i in range(12)]
    insert(conn, baseline + recent, "spans")
    result = call("find_anomalies")
    pay = next(a for a in result["anomalies"] if a["name"] == "chargeCard")
    assert pay["error_spike"] is True and pay["p99_ratio"] > 50
    assert result["recent_window"]["from"].endswith("+00:00")


# --- A real MCP client talking to a live server over HTTP ---------------------------

@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "live.duckdb"))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    insert(app.state.conn, order(T_FAIL, time.time_ns() - 5_000 * MS, fail=True), "spans")
    yield f"http://localhost:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=10)


def test_real_mcp_client_session(live_server):
    async def session_run():
        async with streamable_http_client(live_server) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                service_map = await session.call_tool("get_service_map", {"minutes": 5})
                missing = await session.call_tool("get_trace", {"trace_id": "f" * 32})
                return init, tools, service_map, missing

    init, tools, service_map, missing = asyncio.run(session_run())
    assert init.server_info.name == "Heimdall"
    assert tools == {"list_services", "search_traces", "get_trace", "search_logs", "get_service_map",
                     "find_anomalies"}
    assert not service_map.is_error
    assert {(e["source"], e["target"]) for e in service_map.structured_content["edges"]} == {
        ("gateway", "order-service"), ("order-service", "payment-service"),
    }
    assert missing.is_error and "not found" in missing.content[0].text
