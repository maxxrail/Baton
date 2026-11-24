"""A small in-process pub/sub hub that pushes live updates to WebSockets.

The agent runs in worker threads; WebSocket handlers run on the event loop. `publish` is safe
to call from any thread: it hands each event to the subscriber's loop with
`call_soon_threadsafe`. Each subscription has a bounded queue; a subscriber that falls behind
loses its oldest events rather than growing memory (the console re-reads state on every event,
so a dropped one only delays a refresh).

Topics: `inbox` (every conversation change, for the console) and `conversation:<id>` (one
conversation's messages and typing indicator, for the widget).

This is one process's memory: with several API workers, a WebSocket only sees events from
its own worker. Part 07 can swap this for Postgres LISTEN/NOTIFY or Redis behind the same API.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

Event = dict[str, object]
INBOX = "inbox"
QUEUE_SIZE = 200


def conversation_topic(conversation_id: str) -> str:
    return f"conversation:{conversation_id}"


@dataclass(eq=False)
class Subscription:
    topic: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Event] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))

    def _put(self, event: Event) -> None:  # runs on the subscriber's loop
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(event)

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            yield await self.queue.get()


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscription]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic: str) -> Subscription:
        """Call from inside the event loop that will consume the subscription."""
        sub = Subscription(topic, asyncio.get_running_loop())
        with self._lock:
            self._subs.setdefault(topic, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(sub.topic)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self._subs[sub.topic]

    def publish(self, topic: str, event: Event) -> None:
        with self._lock:
            subs = list(self._subs.get(topic, ()))
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub._put, event)
            except RuntimeError:  # the subscriber's loop has closed
                self.unsubscribe(sub)

    def subscribers(self, topic: str) -> int:
        with self._lock:
            return len(self._subs.get(topic, ()))
