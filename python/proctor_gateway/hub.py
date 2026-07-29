"""Fan-out of violations to connected proctor consoles."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

QUEUE_LIMIT = 256


class ProctorHub:
    """Broadcasts violation records to subscribed proctor consoles.

    Each subscriber gets a bounded queue. A console that cannot keep up
    loses its oldest messages rather than applying backpressure to the
    telemetry ingest path — a slow dashboard must never be able to stall
    an exam in progress.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped_messages(self) -> int:
        return self._dropped

    async def publish(self, message: dict[str, Any]) -> None:
        async with self._lock:
            targets = tuple(self._subscribers)
        for queue in targets:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._dropped += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(message)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)
