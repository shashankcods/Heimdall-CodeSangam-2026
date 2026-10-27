"""Fan-out of freshly written logs to live-tail viewers.

    log_writer flush ──publish()──► each subscriber whose filter matches
                                    └─► its own small bounded queue ──► one SSE connection

- Logs are published AFTER their batch is committed, so the live tail only ever
  shows logs that search can also find.
- Each viewer has its own bounded queue. If a viewer can't keep up, its extra
  logs are dropped (and counted) instead of slowing ingest or other viewers.
- The number of viewers is bounded too (HEIMDALL_MAX_LIVE_VIEWERS, default 50),
  so memory (~1 MB per viewer) and per-flush filter work stay predictable.
- Runs entirely on the event loop: publish() is called from the writer task,
  subscribe()/unsubscribe() from request handlers. No locks needed.
"""

import asyncio
import os
from dataclasses import dataclass, field

from heimdall.ingest.models import LogRecord
from heimdall.queries.logs import LogFilter

DEFAULT_SUBSCRIBER_QUEUE = 1000
DEFAULT_MAX_VIEWERS = 50


class TooManyViewers(Exception):
    """The live tail is at its viewer limit. The endpoint should reply 503."""


@dataclass(eq=False)  # compared by identity, so it can live in a set
class Subscription:
    filter: LogFilter
    queue: asyncio.Queue[LogRecord] = field(
        default_factory=lambda: asyncio.Queue(maxsize=DEFAULT_SUBSCRIBER_QUEUE)
    )
    dropped: int = 0


class LogBroadcaster:
    def __init__(self, max_viewers: int | None = None) -> None:
        self.max_viewers = max_viewers or int(
            os.environ.get("HEIMDALL_MAX_LIVE_VIEWERS", DEFAULT_MAX_VIEWERS)
        )
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, log_filter: LogFilter) -> Subscription:
        if len(self._subscribers) >= self.max_viewers:
            raise TooManyViewers(f"live tail is limited to {self.max_viewers} viewers")
        sub = Subscription(log_filter)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)

    def publish(self, records: list[LogRecord]) -> None:
        """Called by the log writer after each successful flush. Never blocks."""
        for sub in self._subscribers:
            for record in records:
                if not sub.filter.matches(record):
                    continue
                try:
                    sub.queue.put_nowait(record)
                except asyncio.QueueFull:
                    sub.dropped += 1  # slow viewer: skip, don't stall everyone else
