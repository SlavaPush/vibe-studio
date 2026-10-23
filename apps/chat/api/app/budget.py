import json
import uuid

from app.config import Limits, Settings
from app.db import fetch_one

MESSAGE_OVERHEAD_TOKENS = 32
MAX_PAID_ATTEMPTS = 8
PAID_PURPOSES = ("classifier", "main")

USAGE_SQL = """
SELECT
    coalesce(sum(runs), 0) AS runs,
    coalesce(sum(images), 0) AS images,
    coalesce(sum(CASE WHEN state = 'settled' THEN input_tokens + output_tokens ELSE reserved_tokens END), 0) AS tokens
FROM budget_reservations
WHERE created_at > now() - interval '24 hours' AND {condition}
"""


class QuotaExceeded(Exception):
    pass


def input_token_bound(system: str, messages: list[dict]) -> int:
    payload = json.dumps({"system": system, "messages": messages}, ensure_ascii=False)
    return len(payload.encode("utf-8")) + MESSAGE_OVERHEAD_TOKENS * (len(messages) + 1)


def _exceeds(usage: dict, limits: Limits, runs: int, images: int, tokens: int) -> bool:
    return (
        usage["runs"] + runs > limits.runs
        or usage["images"] + images > limits.images
        or usage["tokens"] + tokens > limits.tokens
    )


async def reserve(
    conn,
    settings: Settings,
    *,
    run_id: str,
    user_id: str,
    source_hash: str,
    purpose: str,
    runs: int = 0,
    images: int = 0,
    tokens: int = 0,
    attempt_id: str | None = None,
) -> None:
    await conn.execute("SELECT name FROM quota_locks WHERE name = 'service' FOR UPDATE")
    subjects = (
        ("user_id = %s", (user_id,), settings.user_limits),
        ("source_hash = %s", (source_hash,), settings.source_limits),
        ("true", (), settings.service_limits),
    )
    for condition, params, limits in subjects:
        usage = await fetch_one(conn, USAGE_SQL.format(condition=condition), params)
        if _exceeds(usage, limits, runs, images, tokens):
            raise QuotaExceeded()
    if purpose in PAID_PURPOSES:
        paid = await fetch_one(
            conn,
            "SELECT count(*) AS n FROM budget_reservations WHERE run_id = %s AND purpose = ANY(%s)",
            (run_id, list(PAID_PURPOSES)),
        )
        if paid["n"] >= MAX_PAID_ATTEMPTS:
            raise QuotaExceeded()
    await conn.execute(
        """
        INSERT INTO budget_reservations
            (id, run_id, attempt_id, user_id, source_hash, purpose, runs, images, reserved_tokens, state)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved')
        """,
        (str(uuid.uuid4()), run_id, attempt_id, user_id, source_hash, purpose, runs, images, tokens),
    )


async def settle(conn, attempt_id: str, usage) -> None:
    if usage is None:
        await conn.execute(
            "UPDATE budget_reservations SET state = 'unknown' WHERE attempt_id = %s", (attempt_id,)
        )
        return
    await conn.execute(
        """
        UPDATE budget_reservations SET state = 'settled', input_tokens = %s, output_tokens = %s
        WHERE attempt_id = %s
        """,
        (usage.input_tokens, usage.output_tokens, attempt_id),
    )
