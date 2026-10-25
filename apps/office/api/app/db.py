import logging
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")
PARAM = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
ADDED_COLUMNS = (
    ("tasks", "pr_number", "INTEGER"),
    ("tasks", "head_sha", "TEXT"),
    ("tasks", "failure_kind", "TEXT"),
    ("tasks", "checks", "TEXT"),
    ("tasks", "merge_sha", "TEXT"),
    ("tasks", "pr_review_calls", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "rework_count", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "rework_reason", "TEXT"),
    ("tasks", "rework_priority", "BOOLEAN NOT NULL DEFAULT false"),
    ("tasks", "rework_context", "TEXT"),
    ("tasks", "rework_of", "TEXT"),
    ("tasks", "change_class", "TEXT NOT NULL DEFAULT 'frontend_ui'"),
    ("tasks", "contour", "TEXT"),
    ("tasks", "owner_review", "TEXT"),
    ("tasks", "cost_usd", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("pr_reviews", "owner_summary", "TEXT"),
    ("release_candidates", "pr_review_id", "TEXT"),
    ("release_candidates", "e2e", "TEXT"),
    ("release_candidates", "superseded_by", "TEXT"),
    ("release_candidates", "origin", "TEXT NOT NULL DEFAULT 'task'"),
    ("release_candidates", "origin_decision_id", "TEXT"),
    ("release_candidates", "blocked_reason", "TEXT"),
    ("release_candidates", "verify", "TEXT"),
    ("release_candidates", "rollback", "TEXT"),
    ("release_candidates", "revert", "TEXT"),
    ("release_candidates", "reason", "TEXT"),
    ("scheduler", "active_release_id", "TEXT"),
    ("scheduler", "release_blocked_by", "TEXT"),
    ("scheduler", "published_sha", "TEXT"),
    ("scheduler", "external_wait_since", "TEXT"),
    ("participants", "access_code_id", "TEXT"),
)

logger = logging.getLogger("office.db")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIME_FORMAT)


def from_text(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)


def to_postgres(sql: str) -> str:
    return PARAM.sub(r"%(\1)s", sql.replace("%", "%%"))


class Connection:
    def __init__(self, raw: Any, dialect: str, *, locked: bool) -> None:
        self._raw = raw
        self.dialect = dialect
        self.locked = locked
        self._after_commit: list[Callable[[], None]] = []

    def _run(self, sql: str, params: dict[str, Any] | None) -> Any:
        if self.dialect == "postgres":
            return self._raw.execute(to_postgres(sql), params or {})
        return self._raw.execute(sql, params or {})

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> int:
        return self._run(sql, params).rowcount

    def fetchone(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        row = self._run(sql, params).fetchone()
        return None if row is None else dict(row)

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in self._run(sql, params).fetchall()]

    def after_commit(self, callback: Callable[[], None]) -> None:
        if callback not in self._after_commit:
            self._after_commit.append(callback)

    def run_after_commit(self) -> None:
        for callback in self._after_commit:
            try:
                callback()
            except Exception:
                logger.exception("after-commit callback failed")


class Database:
    def __init__(self, url: str, max_size: int = 10) -> None:
        self._pool: ConnectionPool | None = None
        if url.startswith("sqlite:///"):
            self.dialect = "sqlite"
            self._path = url.removeprefix("sqlite:///")
        elif url.startswith(("postgresql://", "postgres://")):
            self.dialect = "postgres"
            self._pool = ConnectionPool(
                url,
                min_size=1,
                max_size=max_size,
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=True,
            )
        else:
            raise ValueError("OFFICE_DATABASE_URL: ожидается sqlite:/// или postgresql://")

    def _sqlite(self) -> sqlite3.Connection:
        raw = sqlite3.connect(self._path, timeout=30, isolation_level=None, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA busy_timeout = 30000")
        raw.execute("PRAGMA foreign_keys = ON")
        return raw

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        if self._pool is not None:
            with self._pool.connection() as raw:
                yield Connection(raw, self.dialect, locked=False)
            return
        raw = self._sqlite()
        try:
            yield Connection(raw, self.dialect, locked=False)
        finally:
            raw.close()

    @contextmanager
    def _transaction(self, locked: bool) -> Iterator[Connection]:
        if self._pool is not None:
            with self._pool.connection() as raw:
                with raw.transaction():
                    if locked:
                        raw.execute("SELECT id FROM scheduler WHERE id = 1 FOR UPDATE")
                    conn = Connection(raw, self.dialect, locked=locked)
                    yield conn
        else:
            raw = self._sqlite()
            try:
                raw.execute("BEGIN IMMEDIATE")
                conn = Connection(raw, self.dialect, locked=locked)
                try:
                    yield conn
                except BaseException:
                    raw.execute("ROLLBACK")
                    raise
                raw.execute("COMMIT")
            finally:
                raw.close()
        conn.run_after_commit()

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self._transaction(locked=False) as conn:
            yield conn

    @contextmanager
    def locked_transaction(self) -> Iterator[Connection]:
        with self._transaction(locked=True) as conn:
            yield conn

    def init_schema(self) -> None:
        if self.dialect == "sqlite":
            raw = self._sqlite()
            try:
                raw.execute("PRAGMA journal_mode = WAL")
            finally:
                raw.close()
        statements = [part.strip() for part in SCHEMA.read_text(encoding="utf-8").split(";")]
        with self.transaction() as conn:
            for statement in statements:
                if statement:
                    conn.execute(statement)
            self._upgrade(conn)

    def _upgrade(self, conn: Connection) -> None:
        for table, column, ddl in ADDED_COLUMNS:
            if self.dialect == "postgres":
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
                continue
            names = {row["name"] for row in conn.fetchall(f"PRAGMA table_info({table})")}
            if column not in names:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.execute(
            "UPDATE tasks SET status = 'needs_attention', version = version + 1 "
            "WHERE status = 'awaiting_release_approval'"
        )
        conn.execute("DROP INDEX IF EXISTS release_candidates_release_commit")
        conn.execute("UPDATE release_candidates SET status = 'pending', version = version + 1 WHERE status = 'approved'")

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
