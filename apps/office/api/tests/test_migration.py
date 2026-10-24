import sqlite3
from pathlib import Path

import psycopg
import pytest

from app.db import Database
from app.schemas import ReleaseKind, TaskStatus
from app.tasks import TaskStore

OLD_SCHEMA = Path(__file__).with_name("data") / "schema_1cfa9a5.sql"
P51_SCHEMA = Path(__file__).with_name("data") / "schema_36bda3f.sql"
P52_SCHEMA = Path(__file__).with_name("data") / "schema_8dc33dd.sql"
NOW = "2026-10-23T09:00:00.000000Z"
TABLES = (
    "scheduler", "participants", "sessions", "tasks", "task_events", "decisions", "release_candidates",
    "check_reports", "intake_reservations", "operations", "pr_reviews",
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


def create_p51(url: str) -> None:
    db = Database(url)
    try:
        with db.transaction() as conn:
            for statement in P51_SCHEMA.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement.strip())
            conn.execute(
                "INSERT INTO participants (id, display_name, role, created_at) VALUES ('prt_1', 'Мария', 'participant', :now)",
                {"now": NOW},
            )
            conn.execute(
                "INSERT INTO tasks (id, owner_id, title, text, status, profile, version, created_at, updated_at, "
                "rejection_reason, spec, branch, base_sha, pr_url, changed_files, fix_cycles, model_calls, "
                "model_calls_limit, input_tokens, output_tokens, event_seq, pr_number, head_sha, failure_kind, checks) "
                "VALUES ('tsk_pr', 'prt_1', 'Задача', 'Текст задачи', 'awaiting_integration', 'hybrid', 7, :now, :now, "
                "NULL, NULL, 'office/tsk_pr', 'abc', 'https://github.com/SlavaPush/vibe-studio/pull/3', '[]', 0, 12, "
                "30, 100, 20, 0, 3, 'def', NULL, NULL)",
                {"now": NOW},
            )
            for index, (kind, status, commit) in enumerate(
                (("release", "released", "c" * 40), ("rollback", "failed", "a" * 40), ("rollback", "revoked", "a" * 40))
            ):
                conn.execute(
                    "INSERT INTO release_candidates (id, task_id, kind, status, version, commit_sha, "
                    "expected_previous_sha, spec_hash, suite_hash, ci_run_ids, pr_url, created_at, approved_by, "
                    "approved_at, is_published, deploy) VALUES (:id, 'tsk_pr', :kind, :status, 1, :commit, :previous, "
                    ":spec, :suite, '[]', NULL, :now, NULL, NULL, :published, NULL)",
                    {
                        "id": f"rel_{index}", "kind": kind, "status": status, "commit": commit,
                        "previous": "c" * 40 if kind == "rollback" else "a" * 40, "now": NOW,
                        "published": kind == "release", "spec": "sha256:spec", "suite": "sha256:suite",
                    },
                )
    finally:
        db.close()


def test_p51_database_gets_review_and_merge_columns(database_url):
    create_p51(database_url)
    db = Database(database_url)
    try:
        db.init_schema()
        store = TaskStore(db)
        task = store.get_task("tsk_pr")
        assert (task.status, task.pr_number, task.head_sha, task.usage.model_calls) == (
            TaskStatus.awaiting_integration, 3, "def", 12
        )
        assert (task.merge_sha, task.pr_review) == (None, None)
        releases = {release.id: release for release in store.list_releases()}
        assert set(releases) == {"rel_0", "rel_1", "rel_2"}
        assert all(release.pr_review_id is None and release.e2e is None for release in releases.values())
        with db.connection() as conn:
            assert store.pr_review_calls_used(conn, "tsk_pr") == 0
            assert conn.fetchall("SELECT * FROM pr_reviews") == []
        with db.locked_transaction() as conn:
            store.create_candidate(
                conn, task_id="tsk_pr", kind=ReleaseKind.rollback, commit_sha="a" * 40,
                expected_previous_sha="c" * 40, spec_hash="sha256:spec", suite_hash="sha256:suite", ci_run_ids=[],
                pr_url=None,
            )
        with pytest.raises((sqlite3.IntegrityError, psycopg.errors.UniqueViolation)):
            with db.locked_transaction() as conn:
                store.create_candidate(
                    conn, task_id="tsk_pr", kind=ReleaseKind.release, commit_sha="c" * 40,
                    expected_previous_sha="a" * 40, spec_hash="sha256:spec", suite_hash="sha256:suite",
                    ci_run_ids=[], pr_url=None,
                )
        first = dump(db)
        db.init_schema()
        assert dump(db) == first
    finally:
        db.close()


def test_p52_database_gets_release_queue_columns(database_url):
    db = Database(database_url)
    try:
        with db.transaction() as conn:
            for statement in P52_SCHEMA.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement.strip())
            conn.execute(
                "INSERT INTO participants (id, display_name, role, created_at) VALUES ('prt_1', 'Мария', 'participant', :now)",
                {"now": NOW},
            )
            conn.execute(
                "INSERT INTO tasks (id, owner_id, title, text, status, profile, version, created_at, updated_at, "
                "rejection_reason, spec, branch, base_sha, pr_url, changed_files, fix_cycles, model_calls, "
                "model_calls_limit, input_tokens, output_tokens, event_seq, merge_sha) VALUES ('tsk_pr', 'prt_1', "
                "'Задача', 'Текст задачи', 'awaiting_release', 'hybrid', 7, :now, :now, NULL, NULL, 'office/tsk_pr', "
                "'abc', NULL, '[]', 0, 12, 30, 100, 20, 0, :merge)",
                {"now": NOW, "merge": "e" * 40},
            )
            for release_id, status, commit, published in (
                ("rel_0", "released", "c" * 40, True), ("rel_1", "approved", "e" * 40, False)
            ):
                conn.execute(
                    "INSERT INTO release_candidates (id, task_id, kind, status, version, commit_sha, "
                    "expected_previous_sha, spec_hash, suite_hash, ci_run_ids, pr_url, created_at, approved_by, "
                    "approved_at, is_published, deploy, pr_review_id, e2e) VALUES (:id, 'tsk_pr', 'release', :status, "
                    "1, :commit, :previous, :spec, :suite, '[]', NULL, :now, NULL, NULL, :published, NULL, NULL, NULL)",
                    {"id": release_id, "status": status, "commit": commit, "previous": "a" * 40, "now": NOW,
                     "published": published, "spec": "sha256:spec", "suite": "sha256:suite"},
                )
        db.init_schema()
        store = TaskStore(db)
        releases = {release.id: release for release in store.list_releases()}
        assert (releases["rel_1"].status, releases["rel_1"].version) == ("pending", 2)
        assert releases["rel_0"].status == "released" and releases["rel_0"].is_published
        assert all(release.origin == "task" and release.revert is None for release in releases.values())
        with db.connection() as conn:
            assert store.queue_state(conn) == {
                "active_release_id": None, "release_blocked_by": None, "published_sha": None,
                "external_wait_since": None,
            }
        with db.locked_transaction() as conn:
            store.update_candidate(conn, "rel_1", status="revoked")
            store.create_candidate(
                conn, task_id="tsk_pr", kind=ReleaseKind.release, commit_sha="e" * 40,
                expected_previous_sha="c" * 40, spec_hash="sha256:spec", suite_hash="sha256:suite", ci_run_ids=[],
                pr_url=None,
            )
        with pytest.raises((sqlite3.IntegrityError, psycopg.errors.UniqueViolation)):
            with db.locked_transaction() as conn:
                store.create_candidate(
                    conn, task_id="tsk_pr", kind=ReleaseKind.release, commit_sha="e" * 40,
                    expected_previous_sha="d" * 40, spec_hash="sha256:spec", suite_hash="sha256:suite",
                    ci_run_ids=[], pr_url=None,
                )
        first = dump(db)
        db.init_schema()
        assert dump(db) == first
    finally:
        db.close()
