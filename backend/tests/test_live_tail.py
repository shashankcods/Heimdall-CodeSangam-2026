import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from heimdall.api.main import app
from heimdall.ingest.broadcast import LogBroadcaster, Subscription, TooManyViewers
from heimdall.ingest.models import LogRecord, Span
from heimdall.ingest.pipeline import BatchWriter
from heimdall.queries.logs import InvalidSearch, LogFilter, log_to_api
from heimdall.store.schema import connect
from tests.test_decoder import TRACE_ID
from tests.test_decoder_logs import logs_request_bytes, otlp_log

T1 = "a" * 32


def record(body="Stripe timeout", severity="ERROR", service="payment-service",
           trace_id=T1, attributes=None):
    return LogRecord(timestamp=2_000_000, service=service, severity=severity, body=body,
                     trace_id=trace_id, span_id="s4", attributes=attributes or {})


# --- LogFilter: same rules as /api/logs, applied in Python -------------------------

def test_empty_filter_matches_everything():
    assert LogFilter().matches(record())


@pytest.mark.parametrize("log_filter, expected", [
    (LogFilter(q="STRIPE"), True),                      # case-insensitive substring
    (LogFilter(q="paypal"), False),
    (LogFilter(q="time.ut"), False),                    # "." is literal in plain text search
    (LogFilter(q=r"time\w+", regex=True), True),
    (LogFilter(service="payment-service"), True),
    (LogFilter(service="gateway"), False),
    (LogFilter(severity="WARN"), True),                 # minimum level: ERROR >= WARN
    (LogFilter(severity="fatal"), False),
    (LogFilter(trace_id=T1.upper()), True),
    (LogFilter(trace_id="b" * 32), False),
])
def test_filter_rules(log_filter, expected):
    assert log_filter.matches(record()) is expected


def test_tags_compare_as_text_like_the_database():
    r = record(attributes={"retry": 0, "ok": True, "provider": "stripe"})
    assert LogFilter(tags=[("retry", "0"), ("ok", "true"), ("provider", "stripe")]).matches(r)
    assert not LogFilter(tags=[("retry", "1")]).matches(r)
    assert not LogFilter(tags=[("missing", "x")]).matches(r)


def test_null_severity_never_matches_a_severity_filter():
    assert not LogFilter(severity="TRACE").matches(record(severity=None))


@pytest.mark.parametrize("kwargs, message", [
    ({"q": "missing (", "regex": True}, "invalid regex"),
    ({"severity": "LOUD"}, "unknown severity"),
])
def test_bad_filters_fail_up_front(kwargs, message):
    with pytest.raises(InvalidSearch, match=message):
        LogFilter(**kwargs)


def test_log_to_api_matches_search_shape():
    assert log_to_api(record(attributes={"retry": 1})) == {
        "timestamp_ms": 2.0, "service": "payment-service", "severity": "ERROR",
        "body": "Stripe timeout", "trace_id": T1, "span_id": "s4", "attributes": {"retry": 1},
    }


# --- LogBroadcaster ---------------------------------------------------------------

def test_publish_reaches_only_matching_subscribers():
    async def scenario():
        hub = LogBroadcaster()
        errors = hub.subscribe(LogFilter(severity="ERROR"))
        everything = hub.subscribe(LogFilter())
        hub.publish([record(severity="INFO", body="hi"), record(severity="ERROR")])
        assert errors.queue.qsize() == 1
        assert everything.queue.qsize() == 2

    asyncio.run(scenario())


def test_unsubscribed_viewers_get_nothing():
    async def scenario():
        hub = LogBroadcaster()
        sub = hub.subscribe(LogFilter())
        hub.unsubscribe(sub)
        hub.publish([record()])
        assert sub.queue.empty() and hub.subscriber_count == 0

    asyncio.run(scenario())


def test_slow_viewer_drops_extra_logs_without_affecting_others():
    async def scenario():
        hub = LogBroadcaster()
        slow = Subscription(LogFilter(), queue=asyncio.Queue(maxsize=3))
        hub._subscribers.add(slow)
        fast = hub.subscribe(LogFilter())
        hub.publish([record(body=f"log {i}") for i in range(1, 6)])
        assert slow.queue.qsize() == 3 and slow.dropped == 2  # logs 4 and 5 skipped for slow
        assert fast.queue.qsize() == 5 and fast.dropped == 0  # fast viewer got everything

    asyncio.run(scenario())


def test_viewer_cap_rejects_extra_viewers_until_one_leaves():
    async def scenario():
        hub = LogBroadcaster(max_viewers=2)
        first = hub.subscribe(LogFilter())
        hub.subscribe(LogFilter())
        with pytest.raises(TooManyViewers):
            hub.subscribe(LogFilter())
        hub.unsubscribe(first)
        hub.subscribe(LogFilter())  # a slot freed up
        assert hub.subscriber_count == 2

    asyncio.run(scenario())


def test_viewer_cap_from_env(monkeypatch):
    monkeypatch.setenv("HEIMDALL_MAX_LIVE_VIEWERS", "7")
    assert LogBroadcaster().max_viewers == 7
    monkeypatch.delenv("HEIMDALL_MAX_LIVE_VIEWERS")
    assert LogBroadcaster().max_viewers == 50


# --- BatchWriter on_flush hook ------------------------------------------------------

def test_on_flush_called_after_successful_write_only(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    flushed: list[list] = []

    async def scenario():
        writer = BatchWriter(conn, "logs", batch_size=2, flush_interval=60, on_flush=flushed.append)
        writer.start()
        bad = LogRecord(timestamp=1, service=None, severity="INFO", body="x")  # NOT NULL violation
        writer.submit([record(), bad])
        await asyncio.sleep(0.2)
        assert flushed == []  # failed batch: nothing broadcast
        writer.submit([record(body="a"), record(body="b")])
        await asyncio.sleep(0.2)
        await writer.close()

    asyncio.run(scenario())
    assert [[r.body for r in batch] for batch in flushed] == [["a", "b"]]


def test_failing_on_flush_does_not_stop_the_writer(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))

    def explode(records):
        raise RuntimeError("listener bug")

    async def scenario():
        writer = BatchWriter(conn, "spans", batch_size=1, flush_interval=60, on_flush=explode)
        writer.start()
        for i in range(2):
            writer.submit([Span(trace_id=T1, span_id=f"{i:016x}", service="svc", name="op",
                                start_time=0, end_time=1, duration_ns=1)])
        await asyncio.sleep(0.3)
        await writer.close()
        assert writer.stats.written == 2

    asyncio.run(scenario())


# --- Real SSE connection against a live server ---------------------------------------

@pytest.fixture
def live_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(tmp_path / "live.duckdb"))
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.05")
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
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def read_log_events(url, want, out):
    """Collect `event: log` payloads from an SSE stream until `want` have arrived."""
    with httpx.Client(timeout=10) as client, client.stream("GET", url) as r:
        out["status"] = r.status_code
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and event == "log":
                out["events"].append(json.loads(line.split(":", 1)[1]))
                if len(out["events"]) >= want:
                    return


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_stream_delivers_matching_logs_end_to_end(live_server):
    out = {"events": []}
    reader = threading.Thread(
        target=read_log_events, args=(f"{live_server}/api/logs/stream?severity=ERROR", 2, out), daemon=True
    )
    reader.start()
    wait_for(lambda: app.state.log_broadcaster.subscriber_count == 1)

    body = logs_request_bytes({"payment-service": [
        otlp_log("calling Stripe", severity=9),          # INFO: filtered out
        otlp_log("Stripe timeout", severity=17),         # ERROR
        otlp_log("card declined", severity=17),          # ERROR
    ]})
    r = httpx.post(f"{live_server}/v1/logs", content=body,
                   headers={"Content-Type": "application/x-protobuf"})
    assert r.status_code == 200

    reader.join(timeout=5)
    assert out["status"] == 200
    assert [e["body"] for e in out["events"]] == ["Stripe timeout", "card declined"]
    assert out["events"][0]["trace_id"] == TRACE_ID.hex()
    assert set(out["events"][0]) == {
        "timestamp_ms", "service", "severity", "body", "trace_id", "span_id", "attributes",
    }
    # Disconnecting removes the viewer.
    wait_for(lambda: app.state.log_broadcaster.subscriber_count == 0)


def test_stream_returns_503_when_viewer_limit_reached(live_server):
    hub = app.state.log_broadcaster
    hub.max_viewers = 1
    occupant = hub.subscribe(LogFilter())  # the one allowed viewer
    try:
        r = httpx.get(f"{live_server}/api/logs/stream")
        assert r.status_code == 503
        assert r.headers["retry-after"] == "5"
        assert "limited to 1 viewers" in r.json()["detail"]
    finally:
        hub.unsubscribe(occupant)


def test_stream_rejects_bad_filters_with_400(live_server):
    r = httpx.get(f"{live_server}/api/logs/stream", params={"q": "missing (", "regex": "true"})
    assert r.status_code == 400
    assert "invalid regex" in r.json()["detail"]
    assert httpx.get(f"{live_server}/api/logs/stream", params={"tag": "nocolon"}).status_code == 400
