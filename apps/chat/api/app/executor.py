import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from app import budget, policy, retry
from app.conversations import MESSAGE_SELECT, load_message, message_out
from app.db import fetch_all, fetch_one
from app.errors import api_error
from app.providers import Classification, Usage
from app.routing import CLASSIFIER_MODELS, CLASSIFIER_OUTPUT_TOKENS, FALLBACK_REASON, Route, Target, route, switch_text
from app.schemas import (
    AcceptedPayload,
    AssetReadyPayload,
    AssetRef,
    ChecksPassedPayload,
    CompletedPayload,
    FailedPayload,
    ModelSelectedPayload,
    ModelSwitchedPayload,
    SystemMessagePayload,
    TextDeltaPayload,
)
from app.storage import StorageError

log = logging.getLogger("vibe_chat")

MODERATION_SECONDS = 10.0
CLASSIFIER_SECONDS = 15.0
TEXT_SECONDS = 120.0
IMAGE_SECONDS = 180.0
SAVE_SECONDS = 0.3
SAVE_CHARS = 400


class Stop(Exception):
    pass


class LeaseLost(Stop):
    pass


class CancelRequested(Stop):
    pass


class OutcomeUnknown(Exception):
    pass


class Exhausted(Exception):
    pass


class CheckFailed(Exception):
    pass


@dataclass
class RunContext:
    run_id: str
    lease: str
    user_cancelled: bool = False
    lease_lost: bool = False


def dump(model) -> dict:
    return model.model_dump(mode="json")


async def add_event(conn, run_id: str, event_type: str, payload) -> None:
    row = await fetch_one(
        conn, "SELECT coalesce(max(seq), 0) + 1 AS seq FROM run_events WHERE run_id = %s", (run_id,)
    )
    await conn.execute(
        "INSERT INTO run_events (id, run_id, seq, type, payload) VALUES (%s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), run_id, row["seq"], event_type, Jsonb(dump(payload))),
    )


async def insert_message(conn, run: dict, role: str, kind: str | None, text: str, target: Target | None = None):
    message_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO messages (id, conversation_id, user_id, role, kind, text, run_id, effective_model, effort)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            message_id,
            run["conversation_id"],
            run["user_id"],
            role,
            kind,
            text,
            run["id"],
            target.model if target else None,
            target.effort if target else None,
        ),
    )
    return await load_message(conn, message_id)


async def finish(conn, run: dict, status: str, error=None) -> None:
    await conn.execute(
        """
        UPDATE runs SET status = %s, error = %s, lease_token = NULL, lease_until = NULL, updated_at = now()
        WHERE id = %s
        """,
        (status, Jsonb(dump(error)) if error else None, run["id"]),
    )
    await conn.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", (run["conversation_id"],))


async def finish_failed(conn, run: dict, code: str) -> None:
    error = api_error(code)
    await add_event(conn, str(run["id"]), "failed", FailedPayload(error=error))
    await finish(conn, run, "cancelled" if code == "run_cancelled" else "failed", error)


async def finish_system(conn, run: dict, kind: str) -> None:
    text = policy.REFUSAL_TEXT if kind == "refusal" else policy.CHECK_UNAVAILABLE_TEXT
    message = await insert_message(conn, run, "system", kind, text)
    await add_event(conn, str(run["id"]), kind, SystemMessagePayload(message=message))
    await finish(conn, run, "refused" if kind == "refusal" else "check_unavailable")


async def close_open_attempts(conn, run_id) -> list[dict]:
    rows = await fetch_all(
        conn,
        """
        UPDATE attempts SET status = 'unknown', finished_at = coalesce(finished_at, now())
        WHERE run_id = %s AND status IN ('started', 'unknown') RETURNING id, purpose
        """,
        (run_id,),
    )
    for row in rows:
        await conn.execute(
            "UPDATE budget_reservations SET state = 'unknown' WHERE attempt_id = %s AND state = 'reserved'",
            (row["id"],),
        )
    return rows


async def recover(conn, run: dict) -> bool:
    attempts = await fetch_all(
        conn, "SELECT purpose, status FROM attempts WHERE run_id = %s ORDER BY created_at", (run["id"],)
    )
    open_attempts = await close_open_attempts(conn, run["id"])
    if run["cancel_requested"]:
        await finish_failed(conn, run, "run_cancelled")
        return False
    if open_attempts:
        if any(row["purpose"] == "main" for row in open_attempts):
            await finish_failed(conn, run, "provider_outcome_unknown")
        else:
            await finish_system(conn, run, "check_unavailable")
        return False
    main = [row for row in attempts if row["purpose"] == "main"]
    classifier = [row for row in attempts if row["purpose"] == "classifier"]
    if not any(row["status"] == "succeeded" for row in main):
        if classifier and not any(row["status"] == "succeeded" for row in classifier):
            await finish_system(conn, run, "check_unavailable")
            return False
        if main:
            await finish_failed(conn, run, "provider_unavailable")
            return False
    await conn.execute(
        "UPDATE runs SET status = 'queued', lease_token = NULL, lease_until = NULL, updated_at = now() WHERE id = %s",
        (run["id"],),
    )
    return True


def build_context(rows: list[dict]) -> list[dict]:
    selected: list[dict] = []
    total = 0
    for row in reversed(rows):
        total += len(row["text"])
        if total > policy.CONTEXT_CHARS:
            break
        selected.append({"role": row["role"], "content": row["text"]})
    selected.reverse()
    truncated = len(selected) < len(rows)
    while selected and selected[0]["role"] != "user":
        selected.pop(0)
        truncated = True
    if truncated and selected:
        selected[0] = {"role": "user", "content": f"{policy.CONTEXT_OMITTED}\n\n{selected[0]['content']}"}
    return selected


class TextWriter:
    def __init__(self, executor: "Executor", ctx: RunContext, message_id: str) -> None:
        self.executor = executor
        self.ctx = ctx
        self.message_id = message_id
        self.buffer = ""
        self.started = False
        self.saved_at = time.monotonic()

    async def on_delta(self, text: str) -> None:
        self.started = True
        self.buffer += text
        if len(self.buffer) >= SAVE_CHARS or time.monotonic() - self.saved_at >= SAVE_SECONDS:
            async with self.executor.guarded(self.ctx) as (conn, _):
                await self.write(conn, final=False)

    async def write(self, conn, final: bool) -> None:
        emit, self.buffer = policy.split_safe(self.buffer, final)
        self.saved_at = time.monotonic()
        if not emit:
            return
        await conn.execute("UPDATE messages SET text = text || %s WHERE id = %s", (emit, self.message_id))
        await add_event(conn, self.ctx.run_id, "text_delta", TextDeltaPayload(message_id=self.message_id, delta=emit))


class Executor:
    def __init__(
        self,
        pool,
        settings,
        providers,
        storage,
        bus,
        *,
        lease_seconds: float = 30,
        heartbeat_seconds: float = 10,
        poll_seconds: float = 1,
        grace_seconds: float = 20,
        max_runs: int = 8,
    ) -> None:
        self.pool = pool
        self.settings = settings
        self.providers = providers
        self.storage = storage
        self.bus = bus
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.grace_seconds = grace_seconds
        self.max_runs = max_runs
        self.tasks: dict[str, tuple[asyncio.Task, RunContext]] = {}
        self.semaphores = {"anthropic": asyncio.Semaphore(2), "openai": asyncio.Semaphore(2)}
        self.wake_event = asyncio.Event()
        self.loop_task: asyncio.Task | None = None
        self.running = False

    async def start(self) -> None:
        await self.recover_expired()
        self.running = True
        self.loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        if self.loop_task is not None:
            self.loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.loop_task
        tasks = [task for task, _ in self.tasks.values()]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.grace_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def wake(self) -> None:
        self.wake_event.set()

    def cancel_local(self, run_id: str) -> None:
        entry = self.tasks.get(run_id)
        if entry is not None:
            task, ctx = entry
            ctx.user_cancelled = True
            task.cancel()

    async def _loop(self) -> None:
        while self.running:
            self.wake_event.clear()
            try:
                await self.recover_expired()
                while len(self.tasks) < self.max_runs:
                    ctx = await self.claim()
                    if ctx is None:
                        break
                    self._spawn(ctx)
            except Exception as error:
                log.error("executor loop error: %s", type(error).__name__)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake_event.wait(), self.poll_seconds)

    async def claim(self) -> RunContext | None:
        lease = str(uuid.uuid4())
        async with self.pool.connection() as conn:
            row = await fetch_one(
                conn,
                """
                UPDATE runs SET status = 'running', lease_token = %s,
                    lease_until = now() + make_interval(secs => %s), updated_at = now()
                WHERE id = (
                    SELECT id FROM runs WHERE status = 'queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                )
                RETURNING id
                """,
                (lease, self.lease_seconds),
            )
        return RunContext(str(row["id"]), lease) if row else None

    async def recover_expired(self) -> None:
        recovered: list[str] = []
        requeued = False
        async with self.pool.connection() as conn:
            rows = await fetch_all(
                conn,
                "SELECT * FROM runs WHERE status = 'running' AND lease_until < now() FOR UPDATE SKIP LOCKED",
            )
            for row in rows:
                requeued = await recover(conn, row) or requeued
                recovered.append(str(row["id"]))
        for run_id in recovered:
            self.bus.notify(run_id)
        if requeued:
            self.wake()

    def _spawn(self, ctx: RunContext) -> None:
        task = asyncio.create_task(self._execute(ctx))
        self.tasks[ctx.run_id] = (task, ctx)

        def done(_task: asyncio.Task) -> None:
            self.tasks.pop(ctx.run_id, None)
            self.wake()

        task.add_done_callback(done)

    @asynccontextmanager
    async def guarded(self, ctx: RunContext, check_cancel: bool = True):
        async with self.pool.connection() as conn:
            run = await fetch_one(conn, "SELECT * FROM runs WHERE id = %s FOR UPDATE", (ctx.run_id,))
            if run is None or run["status"] != "running" or str(run["lease_token"]) != ctx.lease:
                raise LeaseLost()
            if check_cancel and run["cancel_requested"]:
                raise CancelRequested()
            yield conn, run
        self.bus.notify(ctx.run_id)

    async def _heartbeat(self, ctx: RunContext, task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                async with self.guarded(ctx, check_cancel=False) as (conn, run):
                    await conn.execute(
                        "UPDATE runs SET lease_until = now() + make_interval(secs => %s) WHERE id = %s",
                        (self.lease_seconds, ctx.run_id),
                    )
                    cancel = run["cancel_requested"]
            except LeaseLost:
                ctx.lease_lost = True
                task.cancel()
                return
            except Exception as error:
                log.error("lease heartbeat error: %s", type(error).__name__)
                continue
            if cancel:
                ctx.user_cancelled = True
                task.cancel()
                return

    async def _execute(self, ctx: RunContext) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(ctx, asyncio.current_task()))
        try:
            await self._pipeline(ctx)
        except LeaseLost:
            pass
        except CancelRequested:
            await self._settle_owned(ctx, cancelled=True)
        except asyncio.CancelledError:
            if not ctx.lease_lost:
                await self._settle_owned(ctx, cancelled=ctx.user_cancelled)
        except budget.QuotaExceeded:
            await self._finish_owned(ctx, lambda conn, run: finish_failed(conn, run, "quota_exceeded"))
        except Exception as error:
            log.error("run %s failed: %s", ctx.run_id, type(error).__name__)
            await self._finish_owned(ctx, self._internal_error)
        finally:
            heartbeat.cancel()

    async def _internal_error(self, conn, run) -> None:
        open_attempts = await close_open_attempts(conn, run["id"])
        code = "provider_outcome_unknown" if any(row["purpose"] == "main" for row in open_attempts) else "internal_error"
        await finish_failed(conn, run, code)

    async def _settle_owned(self, ctx: RunContext, cancelled: bool) -> None:
        async def settle(conn, run) -> None:
            if cancelled:
                await close_open_attempts(conn, run["id"])
                await finish_failed(conn, run, "run_cancelled")
            elif await recover(conn, run):
                self.wake()

        await self._finish_owned(ctx, settle)

    async def _finish_owned(self, ctx: RunContext, action) -> None:
        with suppress(LeaseLost):
            async with self.guarded(ctx, check_cancel=False) as (conn, run):
                await action(conn, run)

    async def _pipeline(self, ctx: RunContext) -> None:
        async with self.guarded(ctx) as (conn, run):
            events = await fetch_one(conn, "SELECT count(*) AS n FROM run_events WHERE run_id = %s", (ctx.run_id,))
            if events["n"] == 0:
                await add_event(
                    conn,
                    ctx.run_id,
                    "accepted",
                    AcceptedPayload(user_message_id=str(run["user_message_id"]), requested_model=run["requested_model"]),
                )
            user_message = await fetch_one(
                conn, "SELECT text, position FROM messages WHERE id = %s", (run["user_message_id"],)
            )
            previous = await fetch_all(
                conn,
                """
                SELECT text FROM messages WHERE conversation_id = %s AND role = 'user' AND position < %s
                ORDER BY position DESC LIMIT %s
                """,
                (run["conversation_id"], user_message["position"], policy.CLASSIFIER_HISTORY),
            )
            history = await fetch_all(
                conn,
                """
                SELECT role, text FROM messages
                WHERE conversation_id = %s AND role IN ('user', 'assistant') AND text <> '' AND position <= %s
                ORDER BY position
                """,
                (run["conversation_id"], user_message["position"]),
            )
            attempts = await fetch_all(
                conn,
                "SELECT purpose, status, model, effort, result FROM attempts WHERE run_id = %s ORDER BY created_at",
                (ctx.run_id,),
            )
        text = user_message["text"]

        if run["intent"] is None:
            run = await self._gateway(ctx, text, [row["text"] for row in reversed(previous)], attempts)
            if run is None:
                return

        if run["intent"] == "clarify":
            async with self.guarded(ctx) as (conn, run):
                message = await insert_message(conn, run, "assistant", None, policy.CLARIFY_TEXT)
                await conn.execute("UPDATE runs SET assistant_message_id = %s WHERE id = %s", (message.id, ctx.run_id))
                await add_event(conn, ctx.run_id, "completed", CompletedPayload(message=message))
                await finish(conn, run, "completed")
            return

        succeeded = next((row for row in attempts if row["purpose"] == "main" and row["status"] == "succeeded"), None)
        if succeeded is None:
            plan = route(run["requested_model"], run["intent"], run["complexity"])
            if not await self._main(ctx, run, plan, text, build_context(history)):
                return
        await self._complete(ctx)

    async def _gateway(self, ctx: RunContext, text: str, previous: list[str], attempts: list[dict]):
        moderation_done = next((row for row in attempts if row["purpose"] == "moderation" and row["status"] == "succeeded"), None)
        classifier_done = next((row for row in attempts if row["purpose"] == "classifier" and row["status"] == "succeeded"), None)

        async def moderation() -> bool:
            if moderation_done is not None:
                return policy.moderation_flagged(moderation_done["result"]["scores"])
            return await self._moderate(ctx, text)

        async def classification() -> Classification:
            if classifier_done is not None:
                return Classification.model_validate(classifier_done["result"])
            return await self._classify(ctx, text, previous)

        results = await asyncio.gather(moderation(), classification(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, CheckFailed):
                raise result
        flagged, labels = results
        async with self.guarded(ctx) as (conn, run):
            if isinstance(flagged, CheckFailed) or isinstance(labels, CheckFailed):
                await finish_system(conn, run, "check_unavailable")
                return None
            run = await fetch_one(
                conn,
                """
                UPDATE runs SET intent = %s, complexity = %s, risk = %s, moderation_flagged = %s, updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (labels.intent, labels.complexity, labels.risk, flagged, ctx.run_id),
            )
            if flagged or labels.risk != "none":
                await finish_system(conn, run, "refusal")
                return None
            await add_event(
                conn,
                ctx.run_id,
                "checks_passed",
                ChecksPassedPayload(intent=labels.intent, complexity=labels.complexity, risk=labels.risk),
            )
            return run

    async def _moderate(self, ctx: RunContext, text: str) -> bool:
        async def invoke(target: Target):
            return await self.providers.moderate(text), None

        async def record(conn, run, attempt_id, target, scores) -> bool:
            flagged = policy.moderation_flagged(scores)
            await conn.execute(
                "UPDATE attempts SET result = %s WHERE id = %s", (Jsonb({"scores": scores, "flagged": flagged}), attempt_id)
            )
            return flagged

        try:
            return await self._attempts(
                ctx, "moderation", [Target(policy.MODERATION_MODEL, "unsupported")], MODERATION_SECONDS, invoke, record, tokens=0
            )
        except (OutcomeUnknown, Exhausted) as error:
            raise CheckFailed() from error

    async def _classify(self, ctx: RunContext, text: str, previous: list[str]) -> Classification:
        data = policy.classifier_input(text, previous)
        tokens = budget.input_token_bound(policy.CLASSIFIER_PROMPT, [{"role": "user", "content": data}])

        async def invoke(target: Target):
            return await self.providers.classify(target.model, policy.CLASSIFIER_PROMPT, data, CLASSIFIER_OUTPUT_TOKENS)

        async def record(conn, run, attempt_id, target, labels: Classification) -> Classification:
            await conn.execute("UPDATE attempts SET result = %s WHERE id = %s", (Jsonb(dump(labels)), attempt_id))
            return labels

        try:
            return await self._attempts(
                ctx,
                "classifier",
                [Target(model, "low") for model in CLASSIFIER_MODELS],
                CLASSIFIER_SECONDS,
                invoke,
                record,
                tokens=tokens + CLASSIFIER_OUTPUT_TOKENS,
                invalid_retry=True,
            )
        except (OutcomeUnknown, Exhausted) as error:
            raise CheckFailed() from error

    async def _main(self, ctx: RunContext, run: dict, plan: Route, text: str, context: list[dict]) -> bool:
        first = plan.targets[0]
        if run["assistant_message_id"] is None:
            async with self.guarded(ctx) as (conn, run):
                if plan.switch_reason:
                    await self._switch_event(conn, run, first, plan.switch_reason, plan.switch_text)
                message = await insert_message(conn, run, "assistant", None, "", first)
                run = await fetch_one(
                    conn,
                    """
                    UPDATE runs SET assistant_message_id = %s, effective_model = %s, effort = %s, switch_reason = %s,
                        updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (message.id, first.model, first.effort, plan.switch_reason, ctx.run_id),
                )
                await self._selected_event(conn, run, first)
        message_id = str(run["assistant_message_id"])

        async def on_fallback(conn, run, target: Target) -> None:
            await self._switch_event(conn, run, target, "provider_fallback", switch_text(target.model, FALLBACK_REASON))
            await conn.execute(
                "UPDATE messages SET effective_model = %s, effort = %s WHERE id = %s", (target.model, target.effort, message_id)
            )
            run = await fetch_one(
                conn,
                """
                UPDATE runs SET effective_model = %s, effort = %s, switch_reason = 'provider_fallback', updated_at = now()
                WHERE id = %s RETURNING *
                """,
                (target.model, target.effort, ctx.run_id),
            )
            await self._selected_event(conn, run, target)

        try:
            if first.is_image:
                await self._image(ctx, plan, text, message_id)
            else:
                return not await self._text(ctx, plan, context, message_id, on_fallback)
        except OutcomeUnknown:
            await self._finish_owned(ctx, lambda conn, run: finish_failed(conn, run, "provider_outcome_unknown"))
            return False
        except Exhausted:
            await self._finish_owned(ctx, lambda conn, run: finish_failed(conn, run, "provider_unavailable"))
            return False
        return True

    async def _switch_event(self, conn, run: dict, target: Target, reason: str, text: str) -> None:
        message = await insert_message(conn, run, "system", "model_switched", text)
        await add_event(
            conn,
            str(run["id"]),
            "model_switched",
            ModelSwitchedPayload(
                requested_model=run["requested_model"], effective_model=target.model, switch_reason=reason, message=message
            ),
        )

    async def _selected_event(self, conn, run: dict, target: Target) -> None:
        await add_event(
            conn,
            str(run["id"]),
            "model_selected",
            ModelSelectedPayload(
                assistant_message_id=str(run["assistant_message_id"]),
                provider=target.provider,
                effective_model=target.model,
                effort=target.effort,
            ),
        )

    async def _text(self, ctx: RunContext, plan: Route, context: list[dict], message_id: str, on_fallback) -> bool:
        system = policy.ASSISTANT_PROMPT
        tokens = budget.input_token_bound(system, context)

        async def invoke(target: Target):
            writer = TextWriter(self, ctx, message_id)
            try:
                result = await self.providers.stream_text(target, system, context, writer.on_delta)
            except Stop:
                raise
            except Exception as error:
                if writer.started:
                    async with self.guarded(ctx, check_cancel=False) as (conn, _):
                        await writer.write(conn, final=True)
                    raise retry.StreamInterrupted(type(error).__name__) from error
                raise
            return (writer, result.truncated), result.usage

        async def record(conn, run, attempt_id, target, value) -> bool:
            writer, truncated = value
            await writer.write(conn, final=True)
            if truncated:
                await finish_failed(conn, run, "response_truncated")
            return truncated

        return await self._attempts(
            ctx,
            "main",
            list(plan.targets),
            TEXT_SECONDS,
            invoke,
            record,
            tokens=lambda target: tokens + target.max_output_tokens,
            on_fallback=on_fallback,
        )

    async def _image(self, ctx: RunContext, plan: Route, prompt: str, message_id: str) -> None:
        tokens = budget.input_token_bound("", [{"role": "user", "content": prompt}])

        async def invoke(target: Target):
            data, usage = await self.providers.generate_image(prompt, self.settings.image_quality)
            return data, Usage(usage.input_tokens, 0) if usage else None

        async def record(conn, run, attempt_id, target, data: bytes) -> None:
            asset_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO assets (id, user_id, run_id, message_id, mime_type, width, height, storage_path, pending_data)
                VALUES (%s, %s, %s, %s, 'image/png', 1024, 1024, %s, %s)
                """,
                (asset_id, run["user_id"], ctx.run_id, message_id, f"{run['user_id']}/{asset_id}.png", data),
            )
            await conn.execute("UPDATE messages SET asset_id = %s WHERE id = %s", (asset_id, message_id))

        await self._attempts(ctx, "main", list(plan.targets), IMAGE_SECONDS, invoke, record, tokens=tokens, images=1)

    async def _attempts(
        self,
        ctx: RunContext,
        purpose: str,
        targets: list[Target],
        seconds: float,
        invoke,
        record,
        *,
        tokens,
        images: int = 0,
        invalid_retry: bool = False,
        on_fallback=None,
    ):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        invalid_left = 1 if invalid_retry else 0
        for index, target in enumerate(targets):
            if index > 0 and on_fallback is not None:
                async with self.guarded(ctx) as (conn, run):
                    await on_fallback(conn, run, target)
            retries = 0
            while True:
                if loop.time() >= deadline:
                    raise Exhausted()
                reserve = tokens(target) if callable(tokens) else tokens
                attempt_id, outcome = await self._attempt(ctx, purpose, target, reserve, images, index > 0, invoke, deadline)
                if isinstance(outcome, BaseException):
                    kind = retry.classify(outcome)
                    if kind == retry.UNKNOWN:
                        raise OutcomeUnknown()
                    if kind == retry.INVALID and invalid_left:
                        invalid_left -= 1
                        continue
                    if kind == retry.INVALID:
                        raise Exhausted()
                    if kind == retry.REFUSED and retries < retry.MAX_RETRIES:
                        delay = retry.retry_delay(outcome, retries)
                        if loop.time() + delay < deadline:
                            retries += 1
                            await asyncio.sleep(delay)
                            continue
                    break
                result, usage = outcome
                async with self.guarded(ctx, check_cancel=False) as (conn, run):
                    value = await record(conn, run, attempt_id, target, result)
                    await conn.execute(
                        """
                        UPDATE attempts SET status = 'succeeded', input_tokens = %s, output_tokens = %s, finished_at = now()
                        WHERE id = %s
                        """,
                        (usage.input_tokens if usage else None, usage.output_tokens if usage else None, attempt_id),
                    )
                    await budget.settle(conn, attempt_id, usage)
                return value
        raise Exhausted()

    async def _attempt(self, ctx, purpose, target, tokens, images, fallback, invoke, deadline):
        loop = asyncio.get_running_loop()
        semaphore = self.semaphores[target.provider] if purpose != "moderation" else nullcontext()
        async with semaphore:
            if loop.time() >= deadline:
                raise Exhausted()
            attempt_id = str(uuid.uuid4())
            async with self.guarded(ctx) as (conn, run):
                await conn.execute(
                    """
                    INSERT INTO attempts (id, run_id, purpose, provider, model, effort, status, fallback_reason)
                    VALUES (%s, %s, %s, %s, %s, %s, 'started', %s)
                    """,
                    (
                        attempt_id,
                        ctx.run_id,
                        purpose,
                        target.provider,
                        target.model,
                        target.effort,
                        "provider_fallback" if fallback else None,
                    ),
                )
                if purpose != "moderation":
                    await budget.reserve(
                        conn,
                        self.settings,
                        run_id=ctx.run_id,
                        user_id=str(run["user_id"]),
                        source_hash=run["source_hash"],
                        purpose=purpose,
                        images=images,
                        tokens=tokens,
                        attempt_id=attempt_id,
                    )
            try:
                return attempt_id, await asyncio.wait_for(invoke(target), deadline - loop.time())
            except Stop:
                raise
            except Exception as error:
                await self._fail_attempt(ctx, attempt_id, error)
                return attempt_id, error

    async def _fail_attempt(self, ctx: RunContext, attempt_id: str, error: Exception) -> None:
        kind = retry.classify(error)
        async with self.guarded(ctx, check_cancel=False) as (conn, _):
            await conn.execute(
                "UPDATE attempts SET status = %s, error_type = %s, http_status = %s, finished_at = now() WHERE id = %s",
                (kind, type(error).__name__, retry.http_status(error), attempt_id),
            )
            if kind in (retry.REFUSED, retry.REJECTED):
                await budget.settle(conn, attempt_id, Usage(0, 0))
            else:
                await budget.settle(conn, attempt_id, None)

    async def _complete(self, ctx: RunContext) -> None:
        async with self.guarded(ctx) as (conn, run):
            asset = await fetch_one(
                conn, "SELECT id, storage_path, mime_type, pending_data FROM assets WHERE run_id = %s", (ctx.run_id,)
            )
        if asset is not None and asset["pending_data"] is not None:
            try:
                await self.storage.upload(asset["storage_path"], bytes(asset["pending_data"]), asset["mime_type"])
            except StorageError:
                log.error("storage upload failed for run %s", ctx.run_id)
            else:
                async with self.guarded(ctx) as (conn, run):
                    await conn.execute("UPDATE assets SET pending_data = NULL WHERE id = %s", (asset["id"],))
        async with self.guarded(ctx) as (conn, run):
            message_id = run["assistant_message_id"]
            if asset is not None:
                ref = AssetRef(id=str(asset["id"]), mime_type=asset["mime_type"], width=1024, height=1024)
                await add_event(conn, ctx.run_id, "asset_ready", AssetReadyPayload(message_id=str(message_id), asset=ref))
            row = await fetch_one(conn, MESSAGE_SELECT + " WHERE m.id = %s", (message_id,))
            await add_event(conn, ctx.run_id, "completed", CompletedPayload(message=message_out(row)))
            await finish(conn, run, "completed")
