"""Thread-safe event bus bridging modem worker threads to asyncio subscribers (SSE, webhooks)."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue] = set()
        self._listeners: list[Callable[[str, dict], Any]] = []
        self._lock = threading.Lock()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add_listener(self, fn: Callable[[str, dict], Any]) -> None:
        self._listeners.append(fn)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish(self, event: str, data: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._dispatch, event, data)
        except RuntimeError:
            pass

    def _dispatch(self, event: str, data: dict) -> None:
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                log.warning("event subscriber too slow, dropping event")
        for fn in self._listeners:
            try:
                res = fn(event, data)
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception:
                log.exception("event listener failed")
