import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable

from app.schemas import TaskEvent
from app.tasks import TaskStore

BATCH = 100


class EventNotifier:
    def __init__(self) -> None:
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Event]:
        waiter = (asyncio.get_running_loop(), asyncio.Event())
        with self._lock:
            self._waiters.add(waiter)
        return waiter

    def unsubscribe(self, waiter: tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
        with self._lock:
            self._waiters.discard(waiter)

    def notify(self) -> None:
        with self._lock:
            waiters = list(self._waiters)
        for loop, event in waiters:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass


def format_event(event: TaskEvent) -> str:
    return f"event: task_event\nid: {event.event_id}\ndata: {event.model_dump_json()}\n\n"


def parse_last_event_id(value: str | None) -> int | None:
    if value is None or not value.strip().isdigit():
        return None
    return int(value.strip())


async def stream_events(
    store: TaskStore,
    notifier: EventNotifier,
    *,
    task_id: str | None,
    after: int,
    ping_seconds: float,
    poll_seconds: float,
    session_active: Callable[[], bool],
) -> AsyncIterator[str]:
    waiter = notifier.subscribe()
    cursor = after
    next_ping = time.monotonic() + ping_seconds
    try:
        yield ": ping\n\n"
        while True:
            waiter[1].clear()
            events = await asyncio.to_thread(store.events_after, task_id, cursor, BATCH)
            for event in events:
                cursor = event.event_id
                yield format_event(event)
            if time.monotonic() >= next_ping:
                if not await asyncio.to_thread(session_active):
                    return
                next_ping = time.monotonic() + ping_seconds
                yield ": ping\n\n"
            if len(events) == BATCH:
                continue
            timeout = max(0.0, min(poll_seconds, next_ping - time.monotonic()))
            try:
                await asyncio.wait_for(waiter[1].wait(), timeout)
            except TimeoutError:
                pass
    finally:
        notifier.unsubscribe(waiter)
