from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_LOCK = 724_310_001


async def open_pool(database_url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        database_url,
        min_size=1,
        max_size=5,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=10)
    return pool


async def apply_schema(pool: AsyncConnectionPool) -> None:
    sql = SCHEMA.read_text(encoding="utf-8")
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        await conn.execute(sql)


async def fetch_one(conn, query: str, params=()) -> dict | None:
    cursor = await conn.execute(query, params)
    return await cursor.fetchone()


async def fetch_all(conn, query: str, params=()) -> list[dict]:
    cursor = await conn.execute(query, params)
    return await cursor.fetchall()
