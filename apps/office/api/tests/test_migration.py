from pathlib import Path

from app.db import Database
from app.schemas import TaskStatus
from app.tasks import TaskStore

OLD_SCHEMA = Path(__file__).with_name("data") / "schema_1cfa9a5.sql"
NOW = "2026-10-23T09:00:00.000000Z"
TABLES = (
    "scheduler", "participants", "sessions", "tasks", "task_events", "decisions", "release_candidates",
    "check_reports", "intake_reservations", "operations",
)


def create_old(url: str) -> None:
    db = Database(url)
    try:
        with db.transaction() as conn:
            for statement in OLD_SCHEMA.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement.strip())
            conn.execute(
                "INSERT INTO participants (id, display_name, role, created_at) VALUES ('prt_1', 'Мария', 'participant', :now)",
                {"now": NOW},
            )
            for task_id, status in (("tsk_old", "developing"), ("tsk_release", "awaiting_release_approval")):
                conn.execute(
                    "INSERT INTO tasks (id, owner_id, title, text, status, profile, version, created_at, updated_at, "
                    "rejection_reason, spec, branch, base_sha, pr_url, changed_files, fix_cycles, model_calls, "
                    "model_calls_limit, input_tokens, output_tokens, event_seq) VALUES (:id, 'prt_1', 'Задача', "
                    "'Текст задачи', :status, 'hybrid', 3, :now, :now, NULL, NULL, :branch, 'abc', NULL, '[]', 1, 4, "
                    "30, 100, 20, 1)",
                    {"id": task_id, "status": status, "now": NOW, "branch": f"office/{task_id}"},
                )
            conn.execute(
                "INSERT INTO task_events (event_id, task_id, seq, type, occurred_at, role, tool, artifact, payload) "
                "VALUES (1, 'tsk_old', 1, 'task_created', :now, NULL, NULL, NULL, '{}')",
                {"now": NOW},
            )
            conn.execute("UPDATE scheduler SET next_event_id = 2 WHERE id = 1")
    finally:
        db.close()


def dump(db: Database) -> dict[str, list[dict]]:
    with db.connection() as conn:
        return {
            table: conn.fetchall(f"SELECT * FROM {table} ORDER BY 1") for table in TABLES
        }


def test_old_database_is_upgraded_once(database_url):
    create_old(database_url)
    db = Database(database_url)
    try:
        db.init_schema()
        store = TaskStore(db)
        old = store.get_task("tsk_old")
        assert (old.status, old.branch, old.fix_cycles, old.usage.model_calls) == (
            TaskStatus.developing, "office/tsk_old", 1, 4
        )
        assert (old.pr_number, old.head_sha, old.failure_kind, old.checks) == (None, None, None, None)
        moved = store.get_task("tsk_release")
        assert moved.status == TaskStatus.needs_attention
        assert [event.type for event in store.events_after("tsk_old", 0)] == ["task_created"]
        first = dump(db)
        assert first["operations"] == []
        db.init_schema()
        assert dump(db) == first
    finally:
        db.close()
    again = Database(database_url)
    try:
        again.init_schema()
        assert dump(again) == first
    finally:
        again.close()
