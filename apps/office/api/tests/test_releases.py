import pytest

from app.schemas import ReleaseKind, ReleaseOrigin, ReleaseStatus

pytestmark = pytest.mark.pg


def decide(client, release_id, decision, version, key, **extra):
    return client.post(
        f"/api/releases/{release_id}/decisions",
        json={"decision": decision, "expected_version": version, "idempotency_key": key} | extra,
    )


@pytest.fixture
def setup(office):
    owner, me = office.login("Мария")
    admin, admin_me = office.login("Вячеслав", admin=True)
    task_id = office.queued_task(me["id"])
    return owner, admin, admin_me, task_id


def test_participant_cannot_rollback_or_resume(office, setup):
    owner, _, _, task_id = setup
    pending = office.add_candidate(task_id)
    published = office.add_candidate(
        task_id, commit_sha="d" * 40, status=ReleaseStatus.released, is_published=True
    )
    for release, decision in ((pending, "resume_releases"), (published, "rollback")):
        response = decide(owner, release.id, decision, 1, f"{decision}-owner")
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden_role"


def test_approve_release_is_removed(office, setup):
    _, admin, _, task_id = setup
    candidate = office.add_candidate(task_id)
    response = decide(admin, candidate.id, "approve_release", 1, "approve-rel-1")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    release = admin.get("/api/releases").json()[0]
    assert "approved_by" not in release and "approved_at" not in release
    assert release["status"] == "pending"


def test_resume_without_release_worker_is_not_allowed(office, setup):
    _, admin, _, task_id = setup
    candidate = office.add_candidate(task_id)
    response = decide(admin, candidate.id, "resume_releases", 1, "resume-no-worker")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"
    bad = decide(admin, candidate.id, "resume_releases", 1, "resume-bad-sha", fix_sha="abc")
    assert bad.status_code == 422


def test_rollback_creates_rollback_candidate(office, setup):
    _, admin, _, task_id = setup
    previous = office.add_candidate(
        task_id, commit_sha="a" * 40, expected_previous_sha="9" * 40, status=ReleaseStatus.rolled_back,
        spec_hash="sha256:old-spec", suite_hash="sha256:old-suite", ci_run_ids=["77"],
    )
    published = office.add_candidate(task_id, status=ReleaseStatus.released, is_published=True)
    response = decide(admin, published.id, "rollback", 1, "rollback-key")
    assert response.status_code == 200
    decision = response.json()
    assert (decision["release_id"], decision["task_id"]) == (published.id, None)
    releases = office.store.list_releases()
    rollback = next(release for release in releases if release.kind == ReleaseKind.rollback)
    assert rollback.status == ReleaseStatus.pending
    assert (rollback.origin, rollback.origin_decision_id) == (ReleaseOrigin.manual_rollback, decision["decision_id"])
    assert rollback.commit_sha == published.expected_previous_sha == previous.commit_sha
    assert rollback.expected_previous_sha == published.commit_sha
    assert (rollback.spec_hash, rollback.suite_hash, rollback.ci_run_ids) == (
        "sha256:old-spec",
        "sha256:old-suite",
        ["77"],
    )
    assert rollback.pr_url is None
    assert office.store.events_after(task_id, 0)[-1].type == "release_candidate"
    again = decide(admin, published.id, "rollback", 1, "rollback-key")
    assert again.status_code == 200
    assert again.json() == decision

    duplicate = decide(admin, published.id, "rollback", 1, "rollback-key-2")
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "decision_not_allowed"
    with_fix = decide(admin, published.id, "rollback", 1, "rollback-key-fix", fix_sha="b" * 40)
    assert with_fix.json()["code"] == "decision_not_allowed"

    with office.store.db.transaction() as conn:
        conn.execute("UPDATE release_candidates SET status = 'failed' WHERE id = :id", {"id": rollback.id})
    again = decide(admin, published.id, "rollback", 1, "rollback-key-3")
    assert again.status_code == 200
    rollbacks = [release for release in office.store.list_releases() if release.kind == ReleaseKind.rollback]
    assert [release.commit_sha for release in rollbacks] == [previous.commit_sha] * 2


def test_rollback_of_unpublished_candidate_is_not_allowed(office, setup):
    _, admin, _, task_id = setup
    candidate = office.add_candidate(task_id)
    response = decide(admin, candidate.id, "rollback", 1, "rollback-pending")
    assert response.status_code == 409
    assert response.json()["code"] == "decision_not_allowed"


def test_release_key_conflicts_across_objects(office, setup):
    _, admin, _, task_id = setup
    first = office.add_candidate(task_id, commit_sha="d" * 40, status=ReleaseStatus.released, is_published=True)
    second = office.add_candidate(task_id, commit_sha="e" * 40, status=ReleaseStatus.released, is_published=True)
    assert decide(admin, first.id, "rollback", 1, "release-shared").status_code == 200
    response = decide(admin, second.id, "rollback", 1, "release-shared")
    assert response.status_code == 409
    assert response.json()["code"] == "idempotency_conflict"


def test_missing_candidate_is_not_found(office, setup):
    _, admin, _, _ = setup
    assert decide(admin, "rel_missing", "rollback", 1, "missing-rel").status_code == 404


def test_release_queue_state_without_render(office, setup):
    owner, _, _, _ = setup
    assert owner.get("/api/release-queue").json() == {
        "configured": False, "active_release_id": None, "release_blocked_by": None, "blocked_reason": None,
        "published_sha": None,
    }
