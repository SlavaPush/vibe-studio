import asyncio
import json
import time
import uuid
from collections import defaultdict
from datetime import timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, current_user
from app.conversations import ACTIVE_STATUSES
from app.db import fetch_all, fetch_one
from app.errors import AppError

TERMINAL_EVENTS = {"completed", "refusal", "check_unavailable", "failed"}
POLL_SECONDS = 1.0
PING_SECONDS = 15.0

router = APIRouter()


class EventBus:
    def __init__(self) -> None:
        self.waiters: dict[str, set[asyncio.Event]] = defaultdict(set)

    def notify(self, run_id: str) -> None:
        for event in self.waiters.get(run_id, ()):
            event.set()

    async def wait(self, run_id: str, timeout: float) -> None:
        event = asyncio.Event()
        self.waiters[run_id].add(event)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self.waiters[run_id].discard(event)
            if not self.waiters[run_id]:
                self.waiters.pop(run_id, None)


def format_event(row: dict) -> str:
    data = {
        "event_id": str(row["id"]),
        "run_id": str(row["run_id"]),
        "seq": row["seq"],
        "type": row["type"],
        "timestamp": row["created_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": row["payload"],
    }
    return f"id: {row['seq']}\nevent: {row['type']}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def start_seq(request: Request, after_seq: int | None) -> int:
    header = request.headers.get("last-event-id")
    if header and header.strip().isdigit():
        return int(header.strip())
    return max(after_seq or 0, 0)


@router.get("/api/runs/{run_id}/events")
async def run_events(
    run_id: uuid.UUID, request: Request, after_seq: int | None = None, user: CurrentUser = Depends(current_user)
) -> StreamingResponse:
    run_id = str(run_id)
    pool = request.app.state.pool
    bus: EventBus = request.app.state.bus
    async with pool.connection() as conn:
        run = await fetch_one(
            conn, "SELECT id FROM runs WHERE id = %s AND user_id = %s", (run_id, user.user_id)
        )
    if run is None:
        raise AppError("not_found")
    last = start_seq(request, after_seq)

    async def stream():
        nonlocal last
        idle_since = time.monotonic()
        while True:
            async with pool.connection() as conn:
                status = await fetch_one(conn, "SELECT status FROM runs WHERE id = %s", (run_id,))
                rows = await fetch_all(
                    conn,
                    "SELECT id, run_id, seq, type, payload, created_at FROM run_events WHERE run_id = %s AND seq > %s ORDER BY seq",
                    (run_id, last),
                )
            for row in rows:
                last = row["seq"]
                yield format_event(row)
                if row["type"] in TERMINAL_EVENTS:
                    return
            if not rows and status["status"] not in ACTIVE_STATUSES:
                return
            if rows:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= PING_SECONDS:
                idle_since = time.monotonic()
                yield ": ping\n\n"
            await bus.wait(run_id, POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
