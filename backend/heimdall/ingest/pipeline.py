"""Bounded queue + background batch writer: the single choke point into DuckDB.

    endpoint --submit()--> [ queue ] --run()--> [ buffer ] --flush--> DuckDB
                           bounded,              private list,
                           full -> QueueFull     flush at batch_size or flush_interval

- submit() never touches the database, so ingest replies fast.
- One queue item = the whole list of records from one request (1 queue op per
  request, all-or-nothing accept). The LIMIT, though, counts records, not items:
  requests range from 1 span (manual) to 512 (SDK) to 8192 (Collector), so an
  item count would make the memory bound vary ~8000x. When pending records would
  exceed max_pending, submit() raises QueueFull and the endpoint answers 429.
- Each flush is one INSERT inside a transaction, so a failed batch leaves no
  partial rows. Durability tradeoff: records still in the queue/buffer (about one
  flush interval's worth) are lost if the process crashes.
- One BatchWriter per table: spans now, logs in step 3.
"""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import duckdb
import pyarrow as pa

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


class Record(Protocol):
    def to_row(self) -> dict: ...


class QueueFull(Exception):
    """The ingest queue is full. The endpoint should reply 429."""


@dataclass
class WriterStats:
    received: int = 0       # records accepted by submit()
    rejected: int = 0       # records refused because the queue was full
    written: int = 0        # records committed to DuckDB
    failed: int = 0         # records in batches whose INSERT failed
    flushes: int = 0
    last_flush_ms: float = 0.0


class BatchWriter:
    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        table: str,
        *,
        max_pending: int | None = None,
        batch_size: int | None = None,
        flush_interval: float | None = None,
        on_flush: Callable[[list[Record]], None] | None = None,
    ):
        self.conn = conn
        self.table = table
        # Called with each batch right after it's committed (e.g. live-tail broadcast).
        self.on_flush = on_flush
        # ~100k spans at ~0.6-1.5 KB each = roughly 60-150 MB of queued data.
        self.max_pending = max_pending or _env_int("HEIMDALL_MAX_PENDING", 100_000)
        self.batch_size = batch_size or _env_int("HEIMDALL_BATCH_SIZE", 500)
        self.flush_interval = flush_interval or _env_float("HEIMDALL_FLUSH_INTERVAL", 1.0)
        # Unbounded on purpose: the bound is enforced in submit() by record count.
        self.queue: asyncio.Queue[list[Record] | None] = asyncio.Queue()
        self.pending = 0  # records submitted but not yet taken off the queue
        self.stats = WriterStats()
        self._task: asyncio.Task | None = None

    # --- endpoint side -----------------------------------------------------

    def submit(self, records: list[Record]) -> None:
        """Hand one request's records to the writer. Never blocks."""
        if not records:
            return
        # An oversized request is still accepted into an empty queue, otherwise
        # it could never get in and the sender would retry forever.
        if self.pending and self.pending + len(records) > self.max_pending:
            self.stats.rejected += len(records)
            raise QueueFull(f"{self.table} ingest queue is full")
        self.queue.put_nowait(records)
        self.pending += len(records)
        self.stats.received += len(records)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name=f"writer-{self.table}")

    async def close(self) -> None:
        """Flush everything still queued or buffered, then stop the loop."""
        if self._task is None:
            return
        await self.queue.put(None)  # sentinel: "no more records"
        await self._task
        self._task = None

    # --- writer side -------------------------------------------------------

    async def run(self) -> None:
        buffer: list[Record] = []
        last_flush = time.monotonic()
        while True:
            timeout = max(0.0, self.flush_interval - (time.monotonic() - last_flush))
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                item = []  # interval elapsed with nothing new

            if item is None:  # close() was called
                await self._flush(buffer)
                return
            self.pending -= len(item)
            buffer.extend(item)

            interval_elapsed = time.monotonic() - last_flush >= self.flush_interval
            if len(buffer) >= self.batch_size or (interval_elapsed and buffer):
                await self._flush(buffer)
                buffer = []
            if interval_elapsed or not buffer:
                last_flush = time.monotonic()

    async def _flush(self, records: list[Record]) -> None:
        if not records:
            return
        started = time.perf_counter()
        try:
            # DuckDB calls block, so run them in a worker thread.
            await asyncio.to_thread(self._write, [r.to_row() for r in records])
            self.stats.written += len(records)
        except Exception:
            self.stats.failed += len(records)
            log.exception("failed to write %d records to %s", len(records), self.table)
        else:
            if self.on_flush is not None:
                try:
                    self.on_flush(records)
                except Exception:  # a broken listener must never stop the writer
                    log.exception("on_flush callback failed for %s", self.table)
        self.stats.flushes += 1
        self.stats.last_flush_ms = (time.perf_counter() - started) * 1000

    def _write(self, rows: list[dict]) -> None:
        batch = pa.Table.from_pylist(rows)
        cur = self.conn.cursor()  # one cursor per thread; the connection is shared
        try:
            cur.register("batch", batch)
            cur.execute("BEGIN TRANSACTION")
            try:
                cur.execute(f"INSERT INTO {self.table} SELECT * FROM batch")
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        finally:
            cur.close()
