import asyncio
import time

import pytest
from conftest import FakeProviders, FakeStorage, HANG, seed_run, settings_for, sql

from app.db import open_pool
from app.events import EventBus
from app.executor import Executor, LeaseLost
from app.providers import Usage
from app.routing import Target

HAIKU = Target("claude-haiku-5-5", "low")


def run_async(coroutine):
    return asyncio.run(coroutine)


async def executors(db, count=2, **options):
    pool = await open_pool(db)
    settings = settings_for(db)
    bus = EventBus()
    providers = FakeProviders()
    made = [Executor(pool, settings, providers, FakeStorage(), bus, **options) for _ in range(count)]
    return pool, providers, made


def expire(db, run_id):
    sql(db, "UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = %s", (run_id,))


def test_lease_expired_before_call_prevents_new_attempt(db):
    ids = seed_run(db, status="queued", events=[])

    async def scenario():
        pool, _, (first, second) = await executors(db)
        try:
            ctx_first = await first.claim()
            expire(db, ids["run"])
            await second.recover_expired()
            ctx_second = await second.claim()
            assert ctx_second is not None and ctx_second.lease != ctx_first.lease

            async def invoke(target):
                raise AssertionError("provider must not be called")

            loop = asyncio.get_running_loop()
            with pytest.raises(LeaseLost):
                await first._attempt(ctx_first, "main", HAIKU, 10, 0, False, invoke, loop.time() + 5)
        finally:
            await pool.close()

    run_async(scenario())
    assert sql(db, "SELECT count(*) FROM attempts") == [(0,)]
    assert sql(db, "SELECT count(*) FROM budget_reservations WHERE purpose = 'main'") == [(0,)]


def test_late_result_of_previous_executor_is_discarded(db):
    ids = seed_run(db, status="queued", events=[])
    calls = []

    async def scenario():
        pool, _, (first, second) = await executors(db)
        release = asyncio.Event()
        try:
            ctx = await first.claim()

            async def invoke(target):
                calls.append(target.model)
                await release.wait()
                return "late answer", Usage(1, 1)

            async def record(conn, run, attempt_id, target, result):
                await conn.execute("UPDATE messages SET text = %s WHERE run_id = %s", (result, ids["run"]))

            task = asyncio.create_task(
                first._attempts(ctx, "main", [HAIKU, Target("gpt-6-luna", "low")], 30, invoke, record, tokens=10)
            )
            while not calls:
                await asyncio.sleep(0.01)
            expire(db, ids["run"])
            await second.recover_expired()
            assert await second.claim() is None
            release.set()
            with pytest.raises(LeaseLost):
                await task
        finally:
            await pool.close()

    run_async(scenario())
    assert calls == ["claude-haiku-5-5"]
    assert sql(db, "SELECT status, error->>'code' FROM runs") == [("failed", "provider_outcome_unknown")]
    assert sql(db, "SELECT status FROM attempts") == [("unknown",)]
    assert sql(db, "SELECT text FROM messages WHERE role = 'user'") == [("Привет",)]


def test_shutdown_applies_recovery_to_interrupted_runs(db):
    ids = seed_run(db, status="queued", events=[])

    async def scenario():
        pool, providers, (executor,) = await executors(db, count=1, grace_seconds=0.1)
        providers.fail("text", HANG)
        try:
            await executor.start()
            executor.wake()
            while not providers.count("text"):
                await asyncio.sleep(0.01)
            await executor.stop()
        finally:
            await pool.close()

    run_async(scenario())
    assert sql(db, "SELECT status, error->>'code' FROM runs WHERE id = %s", (ids["run"],)) == [
        ("failed", "provider_outcome_unknown")
    ]


class StubbornProviders(FakeProviders):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def stream_text(self, target, system, messages, on_delta):
        self.calls.append(("text", target.model))
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await self.release.wait()
            raise


def test_shutdown_is_bounded_and_next_instance_recovers_without_new_call(db):
    ids = seed_run(db, status="queued", events=[])
    stubborn = StubbornProviders()
    fresh = FakeProviders()

    async def scenario():
        pool = await open_pool(db)
        executor = Executor(
            pool, settings_for(db), stubborn, FakeStorage(), EventBus(), grace_seconds=0.1, cancel_seconds=0.2
        )
        await executor.start()
        executor.wake()
        while not stubborn.count("text"):
            await asyncio.sleep(0.01)
        started = time.monotonic()
        await executor.stop()
        elapsed = time.monotonic() - started
        stuck = [task for task, _ in executor.tasks.values()]
        await pool.close()
        stubborn.release.set()
        await asyncio.gather(*stuck, return_exceptions=True)

        assert elapsed < 1
        assert stuck
        assert sql(db, "SELECT status FROM runs WHERE id = %s", (ids["run"],)) == [("running",)]
        expire(db, ids["run"])

        pool = await open_pool(db)
        try:
            successor = Executor(pool, settings_for(db), fresh, FakeStorage(), EventBus())
            await successor.start()
            await successor.stop()
        finally:
            await pool.close()

    run_async(scenario())
    assert sql(db, "SELECT status, error->>'code' FROM runs WHERE id = %s", (ids["run"],)) == [
        ("failed", "provider_outcome_unknown")
    ]
    assert stubborn.count("text") == 1
    assert fresh.calls == []
