from datetime import timedelta

import pytest
from conftest import FakeSuite, pr_review_answer
from test_release_queue import admit, drive_revert, q, roll_back  # noqa: F401

from app.release import SCHEMA_ONLY
from app.schemas import Contour, TaskEventType

pytestmark = pytest.mark.pg

SCHEMA = "CREATE TABLE IF NOT EXISTS notes (\n    id uuid PRIMARY KEY\n);\n"


def test_combined_revert_with_server_change_waits_for_owner_and_runs_api_tests(q):
    q.suite = FakeSuite(q.store, q.workspace)
    q.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer(summary="Revert серверного изменения. Одобрить.")
    first = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    admit(q, first, "1" * 40)
    second = q.push("apps/chat/api/app/notes.py", "NOTES = []\n")
    admit(q, second, "2" * 40)
    worker, release = roll_back(q, second)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert (revert.status, revert.pr_number) == ("active", 1)
    q.fake.add_pr_runs(revert.head_sha, 1)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert revert.contour == Contour.server
    assert (revert.owner_review.state, revert.owner_review.head_sha) == ("requested", revert.head_sha)
    assert revert.fix_sha is None and q.fake.merges() == 0
    [review] = q.gateway.role_calls("pr_reviewer", release.task_id)
    assert "Revert выпуска" in review.user and "server" in review.user
    assert "## Сводка для владельца" in q.fake.prs[1]["body"]
    [requested] = q.events(release.task_id, TaskEventType.owner_review_requested)
    assert requested.payload["revert_of"] == release.id

    q.fake.review(1, "SlavaPush", "APPROVED", revert.head_sha)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert revert.fix_sha == q.origin.main() and q.fake.merges() == 1
    assert revert.owner_review.state == "approved"
    q.fake.add_push_runs(revert.fix_sha)
    drive_revert(q, worker)
    assert q.release(release.id).revert.status == "merged"
    assert "api-test" in q.suite.steps[-1]
    assert q.state()["release_blocked_by"] == release.id


def test_final_checks_timeout_applies_after_owner_approved_revert(q):
    q.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer(summary="Revert серверного изменения. Одобрить.")
    sha = q.push("apps/chat/api/app/notes.py", "NOTES = []\n")
    admit(q, sha)
    worker, release = roll_back(q, sha)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    q.fake.add_pr_runs(revert.head_sha, 1)
    drive_revert(q, worker)
    q.fake.review(1, "SlavaPush", "APPROVED", revert.head_sha)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert revert.fix_sha is not None and revert.status == "active"
    q.clock.offset = timedelta(minutes=31)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert revert.status == "needs_attention" and "итогового commit" in revert.reason
    assert q.state()["release_blocked_by"] == release.id


def test_revert_of_schema_only_change_keeps_block_without_pr(q):
    sha = q.push("apps/chat/api/app/schema.sql", SCHEMA)
    admit(q, sha)
    worker, release = roll_back(q, sha)
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert (revert.status, revert.reason) == ("needs_attention", SCHEMA_ONLY)
    assert q.fake.prs == {}
    assert q.state()["release_blocked_by"] == release.id
    assert [(row["status"], row["detail"]) for row in q.ops(release.id) if row["kind"] == "revert"] == [
        ("failed", "schema only")
    ]


def test_rollback_returns_previous_version_and_revert_keeps_schema(q):
    q.origin.write("apps/chat/api/app/schema.sql", SCHEMA)
    sha = q.push("apps/chat/api/app/notes.py", "NOTES = []\n")
    admit(q, sha)
    worker, release = roll_back(q, sha)
    assert q.render.posts()[-1] == ("/rollback", {"deployId": "dep-1"})
    assert q.service.version == q.base[:7]
    drive_revert(q, worker)
    revert = q.release(release.id).revert
    assert (revert.status, revert.pr_number) == ("active", 1)
    assert q.workspace.file_at(revert.head_sha, "apps/chat/api/app/schema.sql") == SCHEMA.encode()
    assert q.workspace.file_at(revert.head_sha, "apps/chat/api/app/notes.py") is None
