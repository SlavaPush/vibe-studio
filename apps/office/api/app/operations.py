from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.db import Connection, Database, from_text, to_text, utcnow
from app.tasks import new_id


class OpStatus(StrEnum):
    started = "started"
    succeeded = "succeeded"
    failed = "failed"
    unknown = "unknown"


@dataclass(frozen=True)
class Operation:
    id: str
    task_id: str | None
    kind: str
    key: str
    status: OpStatus
    attempt: int
    external_ref: str | None
    detail: str | None
    started_at: datetime


def operation_from_row(row: dict[str, Any]) -> Operation:
    return Operation(
        id=row["id"],
        task_id=row["task_id"],
        kind=row["kind"],
        key=row["key"],
        status=OpStatus(row["status"]),
        attempt=row["attempt"],
        external_ref=row["external_ref"],
        detail=row["detail"],
        started_at=from_text(row["started_at"]),
    )


class OperationLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def begin(self, kind: str, key: str, task_id: str | None, release_id: str | None = None) -> tuple[Operation, bool]:
        with self.db.transaction() as conn:
            return self.begin_in(conn, kind, key, task_id, release_id)

    def begin_in(
        self, conn: Connection, kind: str, key: str, task_id: str | None, release_id: str | None = None
    ) -> tuple[Operation, bool]:
        now = to_text(utcnow())
        row = conn.fetchone("SELECT * FROM operations WHERE key = :key", {"key": key})
        if row is None:
            conn.execute(
                "INSERT INTO operations (id, task_id, release_id, kind, key, status, attempt, external_ref, "
                "detail, started_at, finished_at) VALUES (:id, :task_id, :release_id, :kind, :key, :status, 1, "
                "NULL, NULL, :now, NULL)",
                {
                    "id": new_id("op"),
                    "task_id": task_id,
                    "release_id": release_id,
                    "kind": kind,
                    "key": key,
                    "status": OpStatus.started.value,
                    "now": now,
                },
            )
            allowed = True
        elif row["status"] == OpStatus.failed:
            conn.execute(
                "UPDATE operations SET status = :status, attempt = attempt + 1, started_at = :now, "
                "finished_at = NULL, external_ref = NULL WHERE id = :id",
                {"status": OpStatus.started.value, "now": now, "id": row["id"]},
            )
            allowed = True
        else:
            allowed = False
        row = conn.fetchone("SELECT * FROM operations WHERE key = :key", {"key": key})
        return operation_from_row(row), allowed

    def finish(
        self,
        conn: Connection,
        operation: Operation,
        status: OpStatus,
        *,
        external_ref: str | None = None,
        detail: str | None = None,
    ) -> None:
        conn.execute(
            "UPDATE operations SET status = :status, external_ref = :ref, detail = :detail, finished_at = :now "
            "WHERE id = :id",
            {
                "status": status.value,
                "ref": external_ref,
                "detail": detail,
                "now": to_text(utcnow()),
                "id": operation.id,
            },
        )

    def mark(self, operation: Operation, status: OpStatus, *, detail: str | None = None) -> None:
        with self.db.transaction() as conn:
            self.finish(conn, operation, status, detail=detail)

    def open_for(self, task_id: str) -> list[Operation]:
        with self.db.connection() as conn:
            rows = conn.fetchall(
                "SELECT * FROM operations WHERE task_id = :id AND status IN ('started', 'unknown') "
                "ORDER BY started_at, id",
                {"id": task_id},
            )
        return [operation_from_row(row) for row in rows]

    def find(self, key: str) -> Operation | None:
        with self.db.connection() as conn:
            row = conn.fetchone("SELECT * FROM operations WHERE key = :key", {"key": key})
        return None if row is None else operation_from_row(row)
