"""Tiny in-process pub/sub for pushing state changes to SSE clients.

One EventHub is shared by the gateway and the thumb backfill. Each SSE
connection subscribes its own queue, so a slow or disconnected client can
only ever drop its own events — never block the producer or another client.
This replaces the frontend's two polling loops (gateway-status every 2s and
a per-thumbnail HEAD every 1.5s) with events pushed only when state actually
changes: idle means zero traffic.

`emit()` is synchronous and non-blocking by design — it's called from the
gateway worker and the backfill loop, both of which run on the event loop
and must never await on a consumer."""

from __future__ import annotations

import asyncio


class EventHub:
    CLOSE = object()

    def __init__(self, *, max_queue: int = 512):
        self._subs: set[asyncio.Queue] = set()
        self._max = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def emit(self, event: dict) -> None:
        # Fan out without blocking. A full queue means a stuck consumer — drop
        # the event for that subscriber rather than stall the whole gateway.
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def close(self) -> None:
        """End every SSE subscription without waiting for client disconnects."""
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(self.CLOSE)
            except asyncio.QueueFull:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
