import asyncio
import time

import pytest

from heimdall.ingest.models import Span
from heimdall.ingest.pipeline import BatchWriter, QueueFull
from heimdall.store.schema import connect


def make_spans(n, start=0, **overrides):
    base = dict(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        service="gateway",
        name="POST /order",
        start_time=1_000,
        end_time=3_500,
        duration_ns=2_500,
    )
    return [Span(**{**base, "span_id": f"{start + i:016x}", **overrides}) for i in range(n)]


def count(conn):
    return conn.execute("SELECT count(*) FROM spans").fetchone()[0]


async def wait_for_rows(conn, expected, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if count(conn) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {expected} rows, got {count(conn)}")


@pytest.fixture
def conn(tmp_path):
    return connect(str(tmp_path / "test.duckdb"))


def test_flushes_when_batch_size_reached(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", batch_size=3, flush_interval=60)
        writer.start()
        writer.submit(make_spans(3))
        await wait_for_rows(conn, 3)  # long before the 60 s interval
        assert writer.stats.flushes == 1
        await writer.close()

    asyncio.run(scenario())


def test_flushes_partial_batch_after_interval(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", batch_size=500, flush_interval=0.1)
        writer.start()
        writer.submit(make_spans(2))
        await asyncio.sleep(0.05)
        assert count(conn) == 0  # not yet: under batch size, interval not elapsed
        await wait_for_rows(conn, 2)
        await writer.close()

    asyncio.run(scenario())


def test_rejects_when_pending_limit_exceeded(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", max_pending=5)  # not started: nothing drains
        writer.submit(make_spans(3))
        writer.submit(make_spans(2, start=3))  # exactly at the limit: still accepted
        with pytest.raises(QueueFull):
            writer.submit(make_spans(1, start=5))
        assert (writer.pending, writer.stats.received, writer.stats.rejected) == (5, 5, 1)

    asyncio.run(scenario())


def test_limit_counts_records_not_requests(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", max_pending=10)
        writer.submit(make_spans(8))  # one request, 8 records
        with pytest.raises(QueueFull):
            writer.submit(make_spans(3, start=8))  # 2nd request would make 11 > 10

    asyncio.run(scenario())


def test_oversized_request_accepted_into_empty_queue(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", max_pending=5)
        writer.submit(make_spans(8))  # bigger than the limit, but nothing else queued
        assert writer.pending == 8

    asyncio.run(scenario())


def test_pending_drops_as_writer_drains(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", max_pending=5, batch_size=5, flush_interval=60)
        writer.start()
        writer.submit(make_spans(5))
        await wait_for_rows(conn, 5)
        assert writer.pending == 0
        writer.submit(make_spans(5, start=5))  # room again after draining
        await writer.close()
        assert count(conn) == 10

    asyncio.run(scenario())


def test_close_flushes_remaining_records(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", batch_size=500, flush_interval=60)
        writer.start()
        writer.submit(make_spans(7))
        await writer.close()  # well before batch size or interval
        assert count(conn) == 7
        assert writer.stats.written == 7

    asyncio.run(scenario())


def test_failed_batch_is_all_or_nothing_and_writer_keeps_going(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans", batch_size=3, flush_interval=60)
        writer.start()
        good = make_spans(2)
        bad = make_spans(1, start=2, name=None)  # violates NOT NULL on spans.name
        writer.submit(good + bad)
        await asyncio.sleep(0.2)
        assert count(conn) == 0  # transaction rolled back: no partial batch
        assert writer.stats.failed == 3

        writer.submit(make_spans(3, start=10))
        await wait_for_rows(conn, 3)  # writer survived the failure
        await writer.close()

    asyncio.run(scenario())


def test_submit_empty_list_is_noop(conn):
    async def scenario():
        writer = BatchWriter(conn, "spans")
        writer.submit([])
        assert writer.pending == 0 and writer.queue.empty()

    asyncio.run(scenario())


def test_settings_from_env(conn, monkeypatch):
    monkeypatch.setenv("HEIMDALL_MAX_PENDING", "42")
    monkeypatch.setenv("HEIMDALL_BATCH_SIZE", "7")
    monkeypatch.setenv("HEIMDALL_FLUSH_INTERVAL", "0.25")

    async def scenario():
        writer = BatchWriter(conn, "spans")
        assert (writer.max_pending, writer.batch_size, writer.flush_interval) == (42, 7, 0.25)

    asyncio.run(scenario())
