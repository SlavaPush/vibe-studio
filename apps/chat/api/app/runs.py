import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from psycopg import errors as pg_errors

from app import budget
from app.auth import CurrentUser, current_user, mutating_user
from app.conversations import DEFAULT_TITLE, RUN_COLUMNS, TITLE_CHARS, owned_conversation, run_out
from app.db import fetch_one
from app.errors import AppError
from app.executor import finish_failed
from app.schemas import AssetUrl, CreateRunRequest, Run
from app.storage import SIGNED_URL_SECONDS, StorageError

router = APIRouter()


def request_hash(conversation_id: str, body: CreateRunRequest) -> str:
    data = json.dumps({"conversation_id": conversation_id, "text": body.text, "model": body.model}, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


async def existing_run(conn, user_id: str, key: str, digest: str) -> Run | None:
    row = await fetch_one(
        conn,
        f"SELECT {RUN_COLUMNS}, request_hash FROM runs WHERE user_id = %s AND idempotency_key = %s",
        (user_id, key),
    )
    if row is None:
        return None
    if row["request_hash"] != digest:
        raise AppError("idempotency_conflict")
    return run_out(row)


async def owned_run(conn, run_id: str, user_id: str, lock: bool = False) -> dict:
    row = await fetch_one(
        conn,
        "SELECT * FROM runs WHERE id = %s AND user_id = %s" + (" FOR UPDATE" if lock else ""),
        (run_id, user_id),
    )
    if row is None:
        raise AppError("not_found", "Задание не найдено.")
    return row


@router.post("/api/conversations/{conversation_id}/runs", status_code=202)
async def create_run(
    conversation_id: uuid.UUID,
    body: CreateRunRequest,
    request: Request,
    response: Response,
    user: CurrentUser = Depends(mutating_user),
) -> Run:
    conversation_id = str(conversation_id)
    digest = request_hash(conversation_id, body)
    pool = request.app.state.pool
    try:
        async with pool.connection() as conn:
            await owned_conversation(conn, conversation_id, user.user_id)
            previous = await existing_run(conn, user.user_id, body.idempotency_key, digest)
            if previous is not None:
                response.status_code = 200
                return previous
            run_id = str(uuid.uuid4())
            message_id = str(uuid.uuid4())
            row = await fetch_one(
                conn,
                f"""
                INSERT INTO runs (id, user_id, conversation_id, source_hash, idempotency_key, request_hash,
                                  status, requested_model, user_message_id)
                VALUES (%s, %s, %s, %s, %s, %s, 'queued', %s, %s)
                RETURNING {RUN_COLUMNS}
                """,
                (run_id, user.user_id, conversation_id, user.source_hash, body.idempotency_key, digest, body.model, message_id),
            )
            try:
                await budget.reserve(
                    conn,
                    request.app.state.settings,
                    run_id=run_id,
                    user_id=user.user_id,
                    source_hash=user.source_hash,
                    purpose="run",
                    runs=1,
                )
            except budget.QuotaExceeded:
                raise AppError("quota_exceeded") from None
            await conn.execute(
                "INSERT INTO messages (id, conversation_id, user_id, role, text, run_id) VALUES (%s, %s, %s, 'user', %s, %s)",
                (message_id, conversation_id, user.user_id, body.text, run_id),
            )
            first = await fetch_one(
                conn, "SELECT count(*) AS n FROM messages WHERE conversation_id = %s", (conversation_id,)
            )
            title = body.text.strip()[:TITLE_CHARS] if first["n"] == 1 else ""
            await conn.execute(
                """
                UPDATE conversations
                SET updated_at = now(),
                    title = CASE WHEN %s::text <> '' AND NOT title_set AND title = %s THEN %s ELSE title END
                WHERE id = %s
                """,
                (title, DEFAULT_TITLE, title, conversation_id),
            )
    except pg_errors.UniqueViolation as error:
        if error.diag.constraint_name == "runs_one_active":
            raise AppError("rate_limited", "Дождитесь завершения текущего ответа.") from None
        if error.diag.constraint_name != "runs_idempotency":
            raise
        async with pool.connection() as conn:
            previous = await existing_run(conn, user.user_id, body.idempotency_key, digest)
        if previous is None:
            raise
        response.status_code = 200
        return previous
    request.app.state.executor.wake()
    return run_out(row)


@router.get("/api/runs/{run_id}")
async def get_run(run_id: uuid.UUID, request: Request, user: CurrentUser = Depends(current_user)) -> Run:
    async with request.app.state.pool.connection() as conn:
        return run_out(await owned_run(conn, str(run_id), user.user_id))


@router.post("/api/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: uuid.UUID, request: Request, user: CurrentUser = Depends(mutating_user)) -> Run:
    run_id = str(run_id)
    async with request.app.state.pool.connection() as conn:
        row = await owned_run(conn, run_id, user.user_id, lock=True)
        if row["status"] == "queued":
            await finish_failed(conn, row, "run_cancelled")
        elif row["status"] == "running":
            await conn.execute(
                "UPDATE runs SET cancel_requested = true, updated_at = now() WHERE id = %s", (run_id,)
            )
        else:
            raise AppError("run_not_active")
        row = await owned_run(conn, run_id, user.user_id)
    request.app.state.bus.notify(run_id)
    request.app.state.executor.cancel_local(run_id)
    return run_out(row)


@router.get("/api/assets/{asset_id}/url")
async def asset_url(asset_id: uuid.UUID, request: Request, user: CurrentUser = Depends(current_user)) -> AssetUrl:
    storage = request.app.state.storage
    async with request.app.state.pool.connection() as conn:
        asset = await fetch_one(
            conn,
            "SELECT id, storage_path, mime_type, pending_data FROM assets WHERE id = %s AND user_id = %s",
            (str(asset_id), user.user_id),
        )
    if asset is None:
        raise AppError("not_found", "Файл не найден.")
    try:
        if asset["pending_data"] is not None:
            await storage.upload(asset["storage_path"], bytes(asset["pending_data"]), asset["mime_type"])
            async with request.app.state.pool.connection() as conn:
                await conn.execute("UPDATE assets SET pending_data = NULL WHERE id = %s", (str(asset_id),))
        url = await storage.signed_url(asset["storage_path"])
    except StorageError:
        raise AppError("storage_unavailable") from None
    return AssetUrl(url=url, expires_at=datetime.now(timezone.utc) + timedelta(seconds=SIGNED_URL_SECONDS))
