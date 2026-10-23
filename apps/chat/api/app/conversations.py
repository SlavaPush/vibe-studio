import uuid

from fastapi import APIRouter, Depends, Request

from app.auth import CurrentUser, current_user, mutating_user
from app.db import fetch_all, fetch_one
from app.errors import AppError
from app.schemas import (
    ApiError,
    AssetRef,
    Conversation,
    ConversationList,
    CreateConversationRequest,
    Message,
    MessageList,
    RenameConversationRequest,
    Run,
)

DEFAULT_TITLE = "Новый диалог"
TITLE_CHARS = 60
ACTIVE_STATUSES = ("queued", "running")

MESSAGE_SELECT = """
SELECT m.id, m.conversation_id, m.role, m.kind, m.text, m.run_id, m.effective_model, m.effort, m.created_at,
       a.id AS asset_ref, a.mime_type, a.width, a.height
FROM messages m LEFT JOIN assets a ON a.id = m.asset_id
"""
RUN_COLUMNS = """
id, conversation_id, status, requested_model, effective_model, effort, intent, complexity, risk,
switch_reason, user_message_id, assistant_message_id, error, created_at, updated_at
"""

router = APIRouter()


def _str(value) -> str | None:
    return None if value is None else str(value)


def message_out(row: dict) -> Message:
    asset = None
    if row["asset_ref"] is not None:
        asset = AssetRef(id=str(row["asset_ref"]), mime_type=row["mime_type"], width=row["width"], height=row["height"])
    return Message(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        role=row["role"],
        kind=row["kind"],
        text=row["text"],
        run_id=_str(row["run_id"]),
        effective_model=row["effective_model"],
        effort=row["effort"],
        asset=asset,
        created_at=row["created_at"],
    )


def run_out(row: dict) -> Run:
    return Run(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        status=row["status"],
        requested_model=row["requested_model"],
        effective_model=row["effective_model"],
        effort=row["effort"],
        intent=row["intent"],
        complexity=row["complexity"],
        risk=row["risk"],
        switch_reason=row["switch_reason"],
        user_message_id=str(row["user_message_id"]),
        assistant_message_id=_str(row["assistant_message_id"]),
        error=ApiError.model_validate(row["error"]) if row["error"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def conversation_out(row: dict) -> Conversation:
    return Conversation(id=str(row["id"]), title=row["title"], created_at=row["created_at"], updated_at=row["updated_at"])


async def load_message(conn, message_id) -> Message:
    row = await fetch_one(conn, MESSAGE_SELECT + " WHERE m.id = %s", (message_id,))
    return message_out(row)


async def owned_conversation(conn, conversation_id: str, user_id: str) -> dict:
    row = await fetch_one(
        conn, "SELECT * FROM conversations WHERE id = %s AND user_id = %s", (conversation_id, user_id)
    )
    if row is None:
        raise AppError("not_found", "Диалог не найден.")
    return row


@router.get("/api/conversations")
async def list_conversations(request: Request, user: CurrentUser = Depends(current_user)) -> ConversationList:
    async with request.app.state.pool.connection() as conn:
        rows = await fetch_all(
            conn,
            "SELECT * FROM conversations WHERE user_id = %s ORDER BY updated_at DESC, created_at DESC",
            (user.user_id,),
        )
    return ConversationList(items=[conversation_out(row) for row in rows])


@router.post("/api/conversations", status_code=201)
async def create_conversation(
    body: CreateConversationRequest, request: Request, user: CurrentUser = Depends(mutating_user)
) -> Conversation:
    async with request.app.state.pool.connection() as conn:
        row = await fetch_one(
            conn,
            "INSERT INTO conversations (id, user_id, title, title_set) VALUES (%s, %s, %s, %s) RETURNING *",
            (str(uuid.uuid4()), user.user_id, body.title or DEFAULT_TITLE, body.title is not None),
        )
    return conversation_out(row)


@router.patch("/api/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: uuid.UUID,
    body: RenameConversationRequest,
    request: Request,
    user: CurrentUser = Depends(mutating_user),
) -> Conversation:
    async with request.app.state.pool.connection() as conn:
        row = await fetch_one(
            conn,
            """
            UPDATE conversations SET title = %s, title_set = true, updated_at = now()
            WHERE id = %s AND user_id = %s RETURNING *
            """,
            (body.title, str(conversation_id), user.user_id),
        )
    if row is None:
        raise AppError("not_found", "Диалог не найден.")
    return conversation_out(row)


@router.get("/api/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID, request: Request, user: CurrentUser = Depends(current_user)
) -> MessageList:
    async with request.app.state.pool.connection() as conn:
        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        await owned_conversation(conn, str(conversation_id), user.user_id)
        runs = await fetch_all(
            conn,
            f"SELECT {RUN_COLUMNS} FROM runs WHERE conversation_id = %s ORDER BY created_at",
            (str(conversation_id),),
        )
        rows = await fetch_all(
            conn, MESSAGE_SELECT + " WHERE m.conversation_id = %s ORDER BY m.position", (str(conversation_id),)
        )
    active = next((run for run in runs if run["status"] in ACTIVE_STATUSES), None)
    if active is not None:
        rows = [row for row in rows if row["run_id"] != active["id"] or row["role"] == "user"]
    return MessageList(
        items=[message_out(row) for row in rows],
        active_run_id=_str(active["id"]) if active else None,
        runs=[run_out(row) for row in runs],
    )
