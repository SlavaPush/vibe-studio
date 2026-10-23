"""Управление индивидуальными кодами доступа участников: создать, показать список, отозвать.

Запуск из каталога api:
  uv run python scripts/access_codes.py add <метка>
  uv run python scripts/access_codes.py list
  uv run python scripts/access_codes.py revoke <метка>
Строка подключения читается из переменной DATABASE_URL (окружение, затем ../.env). Параметр --database-url-env ИМЯ
задаёт другую переменную, например DATABASE_URL_SUPABASE. Значение строки подключения не печатается.
Перед работой применяется schema.sql, как при старте сервера. Код печатается один раз, при создании; в БД хранится его хеш.
Код возврата: 0 — успех; 1 — ошибка; 2 — переменная не задана.
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

from app.auth import access_code_hash
from app.config import ENV_FILE, load_env_file
from app.db import apply_schema, fetch_all, fetch_one, open_pool

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LENGTH = 12

LIST_SQL = """
SELECT c.label, c.created_at, c.revoked_at,
    (SELECT count(*) FROM users u WHERE u.access_code_id = c.id) AS users,
    (SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE u.access_code_id = c.id AND s.expires_at > now() AND c.revoked_at IS NULL) AS active_sessions,
    usage.runs AS runs_24h,
    usage.images AS images_24h
FROM access_codes c
CROSS JOIN LATERAL (
    SELECT coalesce(sum(b.runs), 0) AS runs, coalesce(sum(b.images), 0) AS images
    FROM budget_reservations b JOIN users u ON u.id = b.user_id
    WHERE u.access_code_id = c.id AND b.created_at > now() - interval '24 hours'
) usage
ORDER BY c.created_at
"""


def report(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[index:index + 4] for index in range(0, CODE_LENGTH, 4))


async def add(conn, label: str) -> int:
    code = new_code()
    try:
        await conn.execute(
            "INSERT INTO access_codes (id, label, code_hash) VALUES (%s, %s, %s)",
            (str(uuid.uuid4()), label, access_code_hash(code)),
        )
    except psycopg.errors.UniqueViolation:
        report(ok=False, error="метка уже существует", label=label)
        return 1
    report(ok=True, label=label, code=code, note="код показывается только сейчас")
    return 0


async def list_codes(conn) -> int:
    for row in await fetch_all(conn, LIST_SQL):
        report(**row)
    return 0


async def revoke(conn, label: str) -> int:
    row = await fetch_one(conn, "SELECT revoked_at FROM access_codes WHERE label = %s FOR UPDATE", (label,))
    if row is None:
        report(ok=False, error="метка не найдена", label=label)
        return 1
    if row["revoked_at"] is not None:
        report(ok=True, label=label, revoked_at=row["revoked_at"], note="код уже отозван")
        return 0
    row = await fetch_one(
        conn, "UPDATE access_codes SET revoked_at = now() WHERE label = %s RETURNING revoked_at", (label,)
    )
    report(ok=True, label=label, revoked_at=row["revoked_at"])
    return 0


async def run(args, database_url: str) -> int:
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)
    try:
        pool = await open_pool(database_url)
    except Exception as error:
        report(ok=False, error=f"нет подключения к БД ({type(error).__name__})")
        return 1
    try:
        await apply_schema(pool)
        async with pool.connection() as conn:
            if args.command == "add":
                return await add(conn, args.label)
            if args.command == "list":
                return await list_codes(conn)
            return await revoke(conn, args.label)
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("add").add_argument("label")
    commands.add_parser("list")
    commands.add_parser("revoke").add_argument("label")
    args = parser.parse_args()
    if args.command in ("add", "revoke") and not args.label.strip():
        report(ok=False, error="метка не может быть пустой")
        return 1
    name = args.database_url_env
    database_url = os.environ.get(name) or load_env_file(ENV_FILE).get(name)
    if not database_url:
        report(ok=False, error=f"переменная {name} не задана")
        return 2
    return asyncio.run(run(args, database_url))


if __name__ == "__main__":
    sys.exit(main())
