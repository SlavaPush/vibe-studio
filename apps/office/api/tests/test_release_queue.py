import json
import secrets
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app import release_queue
from app.config import CHECKOUT_ROOT
from app.db import utcnow
from app.errors import OfficeError
from app.github import github_client
from app.operations import OperationLog, OpStatus
from app.release import ReleaseWorker, task_carrier
from app.render import RenderClient
from app.service_check import MIN_TIMEOUT, SERVICE_TIMEOUT
from app.required_checks import load_required_checks
from app.schemas import (
    CheckReport,
    CheckResult,
    FailureKind,
    Participant,
    ReleaseDecisionRequest,
    ReleaseKind,
    ReleaseOrigin,
    TaskEventType,
)
from conftest import FakeGitHub, FakeRender, FakeService, FakeSuite, ScriptedGateway

REQUIRED = load_required_checks(CHECKOUT_ROOT)


class Crash(BaseException):
    pass


class Q(SimpleNamespace):
    def worker(self, **overrides) -> ReleaseWorker:
        if self.current is not None:
            self.current.shutdown()
        render = RenderClient(
            httpx.Client(base_url="https://api.render.com", transport=httpx.MockTransport(self.render.handler)),
            "render-test-key", "srv-test",
        )
        self.current = ReleaseWorker(
            replace(self.settings, **overrides), self.store, self.workspace, self.github, REQUIRED, self.clock,
            gateway=self.gateway, suite=self.suite,
            service=httpx.Client(transport=httpx.MockTransport(self.service.handler)), render=render,
        )
        return self.current

    def push(self, rel: str, content: str, login: str = "SlavaPush") -> str:
        before = self.origin.main()
        after = self.origin.push(rel, content)
        self.fake.pushed(before, after, login)
        return after

    def candidate(self, sha: str, base: str | None = None, **overrides):
        task_id = self.office.queued_task(self.me["id"])
        self.office.set_task(task_id, status="awaiting_release", merge_sha=sha)
        return self.office.add_candidate(task_id, commit_sha=sha, expected_previous_sha=base or self.base, **overrides)

    def settle(self, worker: ReleaseWorker, limit: int = 30) -> int:
        for count in range(limit):
            if not worker.queue.step():
                return count
        raise AssertionError("queue does not settle")

    def release(self, release_id: str):
        return self.store.get_candidate(release_id)

    def task(self, task_id: str):
        return self.store.get_task(task_id)

    def state(self) -> dict:
        with self.store.db.connection() as conn:
            return self.store.queue_state(conn)

    def events(self, task_id: str, kind: TaskEventType):
        return [event for event in self.store.events_after(task_id, 0, 1000) if event.type == kind]

    def ops(self, release_id: str):
        with self.store.db.connection() as conn:
            return conn.fetchall(
                "SELECT * FROM operations WHERE release_id = :id ORDER BY started_at, id", {"id": release_id}
            )

    def decide(self, release_id: str, decision: str, key: str, **extra):
        release = self.release(release_id)
        body = {"decision": decision, "expected_version": release.version, "idempotency_key": key} | extra
        if decision == "rollback":
            return self.admin.post(f"/api/releases/{release_id}/decisions", json=body)
        try:
            result = self.current.decide(self.admin_participant, release_id, ReleaseDecisionRequest(**body))
        except OfficeError as error:
            failure = {"code": error.code, "message": error.message}
            return SimpleNamespace(status_code=error.status, json=lambda: failure)
        return SimpleNamespace(status_code=200, json=lambda: result.model_dump(mode="json"))


@pytest.fixture
def q(office, origin, tmp_path, app_key, monkeypatch):
    monkeypatch.setattr(release_queue, "VERIFY_INTERVAL", 0)
    app = office.workspace_app(origin, tmp_path)
    store = app.state.store
    _, me = office.login(app=app)
    token = secrets.token_hex(16)
    fake = FakeGitHub(origin.bare, token)
    github = github_client(
        "5452356", "174735588", app_key.path, "SlavaPush/vibe-studio", transport=httpx.MockTransport(fake.handler)
    )
    base = origin.main()
    service = FakeService(base[:7])
    settings = replace(
        app.state.settings, github_push_url=origin.url, render_api_key="render-test-key",
        render_service_id="srv-test", verify_seconds=0,
    )
    state = Q(
        office=office, origin=origin, app=app, store=store, workspace=app.state.workspace, me=me, fake=fake,
        github=github, base=base, service=service, render=FakeRender(service, base), settings=settings,
        clock=Clock(), gateway=ScriptedGateway(settings), suite=FakeSuite(store), current=None,
    )
    state.admin, admin_me = office.login("Вячеслав", admin=True, app=app)
    state.admin_participant = Participant.model_validate(admin_me)
    yield state
    if state.current is not None:
        state.current.shutdown()


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self):
        return utcnow() + self.offset


def deploy_ids(q, release_id):
    return [event.payload.get("render_status") for event in q.events(q.release(release_id).task_id, TaskEventType.deploy_status)]


def published(q, release):
    worker = q.worker()
    q.settle(worker)
    q.render.set(q.release(release.id).deploy.render_deploy_id, "live")
    q.settle(worker)
    assert q.release(release.id).status == "released"
    return worker


def test_release_is_deployed_verified_and_published(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = q.worker()
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, current.deploy.render_deploy_id, current.deploy.render_status) == (
        "deploying", "dep-2", "build_in_progress"
    )
    assert q.task(release.task_id).status == "deploying"
    assert q.state()["active_release_id"] == release.id
    assert q.state()["published_sha"] == q.base
    assert q.render.posts() == [("/deploys", {"commitId": sha})]
    q.render.set("dep-2", "live")
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, current.is_published, current.verify.ok, current.verify.fault) == (
        "released", True, True, False
    )
    assert q.task(release.task_id).status == "released"
    assert q.state() == {
        "active_release_id": None, "release_blocked_by": None, "published_sha": None, "external_wait_since": None,
    }
    assert deploy_ids(q, release.id) == ["build_in_progress", "live", "live"]
    assert q.events(release.task_id, TaskEventType.deploy_status)[-1].payload["verified"] is True
    q.settle(q.worker())
    assert len(q.render.posts()) == 1
    assert [op["kind"] for op in q.ops(release.id)] == ["deploy"]


def test_render_deploy_error_fails_without_rollback(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = q.worker()
    q.settle(worker)
    q.render.set("dep-2", "build_failed")
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, current.deploy.error, current.is_published) == ("failed", "Render: build_failed", False)
    task = q.task(release.task_id)
    assert (task.status, task.failure_kind) == ("failed", FailureKind.deploy_failed)
    assert q.state()["active_release_id"] is None
    assert q.state()["release_blocked_by"] is None
    assert len(q.render.posts()) == 1


def test_refused_deploy_request_fails_and_frees_queue(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.render.post_status = 400
    q.settle(q.worker())
    assert q.release(release.id).status == "failed"
    assert q.state()["active_release_id"] is None
    assert [op["status"] for op in q.ops(release.id)] == ["failed"]


def test_post_timeout_is_reconciled_by_deploy_list(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.render.lose_post = True
    worker = q.worker()
    assert not worker.queue.step()
    assert [op["status"] for op in q.ops(release.id)] == ["started"]
    assert q.release(release.id).deploy is None
    q.render.lose_post = False
    q.settle(worker)
    current = q.release(release.id)
    assert current.deploy.render_deploy_id == "dep-2"
    assert [(op["status"], op["external_ref"]) for op in q.ops(release.id)] == [("succeeded", "dep-2")]
    q.render.set("dep-2", "live")
    q.settle(worker)
    assert q.release(release.id).status == "released"
    assert len(q.render.posts()) == 1


@pytest.mark.parametrize("moment", ["before_request", "lost_unlisted_response"])
def test_restart_in_started_without_id_needs_attention_without_second_post(q, monkeypatch, moment):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = q.worker()
    if moment == "before_request":
        monkeypatch.setattr(worker.queue.render, "deploy", lambda sha: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            worker.queue.step()
    else:
        q.render.lose_post, q.render.hide_new = True, True
        worker.queue.step()
    assert [op["status"] for op in q.ops(release.id)] == ["started"]
    assert q.release(release.id).deploy is None
    restarted = q.worker()
    q.settle(restarted)
    current = q.release(release.id)
    assert [op["status"] for op in q.ops(release.id)] == ["unknown"]
    assert current.status == "deploying" and "не установлен" in current.blocked_reason
    assert q.task(release.task_id).status == "needs_attention"
    assert q.state()["active_release_id"] == release.id
    q.settle(q.worker())
    assert len(q.render.posts()) == (0 if moment == "before_request" else 1)
    response = q.decide(release.id, "resume_releases", f"resume-{moment}")
    assert response.status_code == 200, response.text
    current = q.release(release.id)
    assert current.status == "failed"
    assert q.state()["active_release_id"] is None
    assert q.task(release.task_id).status == "failed"
    assert [op["status"] for op in q.ops(release.id)] == ["failed"]


def test_answer_lost_after_request_is_adopted_without_second_post(q, monkeypatch):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = q.worker()
    monkeypatch.setattr(worker.queue, "_started", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        worker.queue.step()
    restarted = q.worker()
    q.settle(restarted)
    assert q.release(release.id).deploy.render_deploy_id == "dep-2"
    assert len(q.render.posts()) == 1


def test_two_deploys_of_same_commit_after_start_are_unknown(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.render.lose_post = True
    worker = q.worker()
    worker.queue.step()
    q.render.add(sha, "build_in_progress")
    q.render.lose_post = False
    q.settle(worker)
    assert [op["status"] for op in q.ops(release.id)] == ["unknown"]
    assert q.task(release.task_id).status == "needs_attention"
    assert q.state()["active_release_id"] == release.id


def test_restart_after_saved_id_and_during_verification_does_not_post_again(q, monkeypatch):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.settle(q.worker())
    q.render.set("dep-2", "live")
    worker = q.worker()
    real = worker.queue._check
    monkeypatch.setattr(worker.queue, "_check", lambda expected: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        q.settle(worker)
    assert q.release(release.id).status == "verifying"
    monkeypatch.setattr(worker.queue, "_check", real)
    restarted = q.worker()
    monkeypatch.setattr(restarted.queue, "_publish_in", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        q.settle(restarted)
    assert q.release(release.id).status == "verifying"
    q.settle(q.worker())
    assert q.release(release.id).status == "released"
    assert q.task(release.task_id).status == "released"
    released = [
        event for event in q.events(release.task_id, TaskEventType.status_changed) if event.payload["to"] == "released"
    ]
    assert len(released) == 1
    assert len(q.render.posts()) == 1


def failed_verification(q, **settings):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    admit(q, sha)
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker(**settings)
    q.settle(worker)
    q.render.set("dep-2", "live")
    q.settle(worker)
    return worker, release, sha


def test_failed_verification_rolls_back_once_and_blocks(q):
    worker, release, sha = failed_verification(q)
    current = q.release(release.id)
    assert current.status == "rolling_back"
    assert (current.verify.ok, current.verify.health) == (False, False)
    assert current.rollback.target_deploy_id == "dep-1"
    assert q.render.posts()[-1] == ("/rollback", {"deployId": "dep-1"})
    q.render.set("dep-3", "live")
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, current.is_published) == ("failed", False)
    assert current.rollback.verify.ok and current.rollback.render_deploy_id == "dep-3"
    assert "откат к" in current.blocked_reason
    assert current.revert.status == "pending" and current.revert.source == "auto"
    task = q.task(release.task_id)
    assert (task.status, task.failure_kind) == ("failed", FailureKind.verify_failed)
    assert q.state()["active_release_id"] is None
    assert q.state()["release_blocked_by"] == release.id
    assert q.service.version == q.base[:7]
    assert [event.payload["release_id"] for event in q.events(release.task_id, TaskEventType.release_blocked)] == [
        release.id
    ]
    statuses = [event.payload["render_status"] for event in q.events(release.task_id, TaskEventType.rollback_status)]
    assert statuses == ["requested", "update_in_progress", "live", "live"]
    q.settle(q.worker())
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]


def test_verify_fault_forces_rollback_and_rollback_check_is_real(q):
    worker = q.worker(verify_fault=True)
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.settle(worker)
    q.render.set("dep-2", "live")
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, current.verify.fault, current.verify.attempts) == ("rolling_back", True, 0)
    event = q.events(release.task_id, TaskEventType.deploy_status)[-1]
    assert (event.payload["fault"], event.payload["verified"]) == (True, False)
    q.service.broken.add(q.base[:7])
    q.render.set("dep-3", "live")
    q.settle(worker)
    current = q.release(release.id)
    assert current.status == "rolling_back"
    assert current.rollback.verify.ok is False
    assert "Проверка после отката" in current.blocked_reason
    assert q.state()["active_release_id"] == release.id
    assert q.task(release.task_id).status == "needs_attention"


@pytest.mark.parametrize("failure", ["refused", "render_failed"])
def test_rollback_error_needs_attention_and_keeps_queue(q, failure):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker()
    q.settle(worker)
    if failure == "refused":
        q.render.post_status = 400
    q.render.set("dep-2", "live")
    q.settle(worker)
    if failure == "render_failed":
        q.render.set("dep-3", "update_failed")
        q.settle(worker)
    current = q.release(release.id)
    assert current.status == "rolling_back" and current.blocked_reason
    assert q.task(release.task_id).status == "needs_attention"
    assert q.state()["active_release_id"] == release.id
    assert q.state()["release_blocked_by"] is None


def test_lost_rollback_answer_is_reconciled_without_second_post(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker()
    q.settle(worker)
    q.render.lose_post = True
    q.render.set("dep-2", "live")
    q.settle(worker)
    q.render.lose_post = False
    q.settle(q.worker())
    current = q.release(release.id)
    assert current.rollback.render_deploy_id == "dep-3"
    assert [(op["kind"], op["status"]) for op in q.ops(release.id)] == [("deploy", "succeeded"), ("rollback", "succeeded")]
    q.render.set("dep-3", "live")
    q.settle(q.worker())
    assert q.release(release.id).status == "failed"
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]


def test_external_deploy_during_verification_stops_without_rollback(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker()
    q.settle(worker)
    q.render.set("dep-2", "live")
    q.render.add(q.base, "build_in_progress")
    q.settle(worker)
    current = q.release(release.id)
    assert current.blocked_reason and "вне офиса" in current.blocked_reason
    assert [path for path, _ in q.render.posts()] == ["/deploys"]
    assert q.state()["active_release_id"] == release.id


def test_external_deploy_before_rollback_stops_without_rollback(q, monkeypatch):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker()
    q.settle(worker)
    q.render.set("dep-2", "live")
    real = worker.queue._check

    def external_after_check(expected):
        result = real(expected)
        q.service.on_version = lambda: q.render.add(q.base, "build_in_progress")
        return result

    monkeypatch.setattr(worker.queue, "_check", external_after_check)
    q.settle(worker)
    current = q.release(release.id)
    assert "Перед откатом" in current.blocked_reason
    assert current.verify is not None and not current.verify.ok
    assert [path for path, _ in q.render.posts()] == ["/deploys"]
    assert q.state()["active_release_id"] == release.id


def test_external_deploy_during_rollback_check_keeps_queue(q):
    worker, release, sha = failed_verification(q)
    assert q.release(release.id).status == "rolling_back"
    q.service.on_version = lambda: q.render.add(q.base, "build_in_progress")
    q.render.set("dep-3", "live")
    q.settle(worker)
    current = q.release(release.id)
    assert current.status == "rolling_back"
    assert current.rollback.verify.ok
    assert "проверки отката" in current.blocked_reason
    assert q.state()["active_release_id"] == release.id
    assert q.state()["release_blocked_by"] is None
    assert current.revert is None
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]


def test_verification_window_limits_each_request(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker(verify_seconds=120)
    q.settle(worker)
    q.render.set("dep-2", "live")
    q.service.down = True
    seen = []

    def slow(request):
        seen.append((q.clock(), request.extensions["timeout"]["read"]))
        q.clock.offset += timedelta(seconds=request.extensions["timeout"]["read"])

    q.service.on_request = slow
    start = q.clock()
    q.settle(worker)
    q.service.on_request = None
    assert [timeout for _, timeout in seen[:3]] == pytest.approx([SERVICE_TIMEOUT, SERVICE_TIMEOUT, MIN_TIMEOUT], abs=1)
    verify = q.release(release.id).verify
    assert verify is not None and not verify.ok and verify.attempts == 1
    elapsed = (verify.checked_at - start).total_seconds()
    assert 120 <= elapsed < 120 + MIN_TIMEOUT + 5


def test_external_running_deploy_delays_start_then_needs_attention(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    q.render.add(q.base, "build_in_progress")
    worker = q.worker()
    q.settle(worker)
    assert q.release(release.id).status == "pending"
    assert q.state()["external_wait_since"] is not None
    assert q.render.posts() == []
    q.clock.offset = timedelta(minutes=31)
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, q.task(release.task_id).status) == ("pending", "needs_attention")
    assert "вне офиса" in current.blocked_reason
    assert q.state()["active_release_id"] is None
    assert q.render.posts() == []
    q.render.set("dep-2", "live")
    assert q.decide(release.id, "resume_releases", "resume-external-wait").status_code == 200
    assert q.task(release.task_id).status == "awaiting_release"
    q.settle(worker)
    assert q.release(release.id).status == "deploying"
    assert q.state()["external_wait_since"] is None
    assert q.render.posts() == [("/deploys", {"commitId": sha})]


def test_newest_candidate_supersedes_older_and_older_cannot_deploy_later(q):
    first = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    second = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    newer = q.candidate(second)
    older = q.candidate(first)
    worker = published(q, newer)
    assert q.release(older.id).status == "superseded"
    assert q.release(older.id).superseded_by == newer.id
    assert q.task(older.task_id).status == "released"
    summary = q.events(older.task_id, TaskEventType.role_finished)[-1].payload["summary"]
    assert summary == f"Опубликовано в составе {second[:7]}."
    q.settle(worker)
    assert q.render.posts() == [("/deploys", {"commitId": second})]


def test_rollback_candidate_has_priority(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = published(q, release)
    newer = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    pending = q.candidate(newer, base=sha)
    assert q.decide(release.id, "rollback", "manual-rollback").status_code == 200
    [rollback] = [item for item in q.store.list_releases() if item.kind == ReleaseKind.rollback]
    assert rollback.origin == ReleaseOrigin.manual_rollback
    q.settle(worker)
    assert q.render.posts()[-1] == ("/rollback", {"deployId": "dep-1"})
    assert q.release(rollback.id).status == "deploying"
    q.render.set("dep-3", "live")
    q.settle(worker)
    current = q.release(rollback.id)
    assert (current.status, current.is_published) == ("released", True)
    selected = q.release(release.id)
    assert (selected.status, selected.is_published, selected.revert.status) == ("rolled_back", False, "pending")
    assert q.state()["release_blocked_by"] == release.id
    assert q.release(pending.id).status == "pending"
    assert q.task(release.task_id).status == "released"
    q.settle(worker)
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]


def test_published_version_change_revokes_and_recreates_candidate(q):
    first = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    released = q.candidate(first)
    worker = published(q, released)
    external = q.push("apps/chat/web/src/C.tsx", "export const c = 1\n")
    newer = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    stale = q.candidate(external, base=first)
    pending = q.candidate(newer, base=first)
    q.render.add(external, "live")
    q.settle(worker)
    assert q.release(released.id).is_published is False
    assert q.state()["published_sha"] == external
    assert q.release(stale.id).status == "superseded"
    assert q.task(stale.task_id).status == "released"
    assert "внешний деплой" in q.events(stale.task_id, TaskEventType.role_finished)[-1].payload["summary"]
    assert q.release(pending.id).status == "revoked"
    [recreated] = [
        item for item in q.store.list_releases() if item.commit_sha == newer and item.status != "revoked"
    ]
    assert (recreated.expected_previous_sha, recreated.status, recreated.task_id) == (external, "deploying", pending.task_id)
    assert q.events(pending.task_id, TaskEventType.external_publish)[0].payload == {"published_sha": external}


@pytest.mark.parametrize("failure", ["down", "body"])
def test_unavailable_version_before_deploy_frees_queue(q, failure):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    if failure == "down":
        q.service.down = True
    else:
        q.service.version = "dev"
    q.settle(q.worker())
    current = q.release(release.id)
    assert (current.status, q.task(release.task_id).status) == ("pending", "needs_attention")
    assert "Деплой не начат" in current.blocked_reason
    assert q.state()["active_release_id"] is None
    assert q.render.posts() == []


def test_active_release_defers_new_candidate_and_rollback_keeps_publication(q):
    first = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    previous = q.candidate(first)
    worker = published(q, previous)
    older = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    newer = q.push("apps/chat/web/src/C.tsx", "export const c = 1\n")
    ancestor = q.candidate(older, base=first)
    active = q.candidate(newer, base=first)
    q.service.broken.add(newer[:7])
    q.settle(worker)
    assert q.release(ancestor.id).status == "superseded"
    q.render.set(q.release(active.id).deploy.render_deploy_id, "live")
    assert q.service.version == newer[:7]
    ready = q.office.queued_task(q.me["id"])
    late = q.push("apps/chat/web/src/D.tsx", "export const d = 1\n")
    q.office.set_task(ready, status="awaiting_integration", merge_sha=late)
    report = CheckReport(task_id=ready, commit_sha=late, spec_hash=None, suite_hash="sha256:fake", checks=[
        CheckResult(command="npm run lint", exit_code=0, duration_ms=1, artifact_urls=[])
    ])
    assert worker.queue.busy()
    assert worker._candidate(task_carrier(q.task(ready)), report, []) is False
    assert [item for item in q.store.list_releases() if item.task_id == ready] == []
    assert q.release(previous.id).is_published is True
    q.settle(worker)
    q.render.set(q.release(active.id).rollback.render_deploy_id, "live")
    q.settle(worker)
    assert q.release(active.id).status == "failed"
    assert q.release(previous.id).is_published is True
    assert q.task(ancestor.task_id).status == "failed"
    assert q.task(previous.task_id).status == "released"
    assert worker._candidate(task_carrier(q.task(ready)), report, []) is True
    [created] = [item for item in q.store.list_releases() if item.task_id == ready]
    assert created.expected_previous_sha == first


def admit(q, sha: str, head: str = "f" * 40) -> str:
    task_id = q.office.queued_task(q.me["id"])
    log = OperationLog(q.store.db)
    for kind, key, ref in (("push", f"push:{task_id}:{head}", head), ("merge_pr", f"merge_pr:{task_id}:{head}", sha)):
        operation, _ = log.begin(kind, key, task_id)
        with q.store.db.transaction() as conn:
            log.finish(conn, operation, OpStatus.succeeded, external_ref=ref)
    with q.store.db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO pr_reviews (id, task_id, head_sha, verdict, reasons, required_changes, checklist, model, "
            "created_at) VALUES (:id, :task, :head, 'approve', 'ok', '[]', '[]', 'model', '2026-10-24T12:00:00.000000Z')",
            {"id": f"prv_{sha[:12]}", "task": task_id, "head": head},
        )
        q.store.record_event(
            conn, task_id, TaskEventType.checks_status,
            {"head_sha": head, "checks": [{"name": "office-paths", "status": "completed", "conclusion": "success", "url": None}]},
        )
    return task_id


def office_merge(q, sha: str, number: int, head: str = "f" * 40) -> None:
    task_id = admit(q, sha, head)
    q.fake.prs[number] = {
        "number": number, "state": "closed", "merged": True, "merge_commit_sha": sha, "merged_at": "2026-10-24T12:00:00Z",
        "merged_by": {"login": "vibe-office[bot]"}, "head": {"ref": f"office/{task_id}", "sha": head},
    }


def human_pr(q, sha: str, number: int, login: str = "SlavaPush") -> None:
    q.fake.prs[number] = {
        "number": number, "state": "closed", "merged": True, "merge_commit_sha": sha, "merged_at": "2026-10-24T12:00:00Z",
        "merged_by": {"login": login}, "head": {"ref": "feature", "sha": "e" * 40},
    }


def test_composition_accepts_owner_pushes_human_and_office_prs(q):
    pushed = q.push("apps/office/api/app/x.py", "x = 1\n")
    q.push(".github/workflows/ci.yml", "name: CI 2\n")
    merged_by_owner = q.push("apps/chat/api/main.py", "app = 1\n", login="someone")
    human_pr(q, merged_by_owner, 7)
    office = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n", login="vibe-office[bot]")
    office_merge(q, office, 8)
    worker = q.worker()
    assert worker.queue.composition(q.base, office) == []
    assert worker.queue.composition(q.base, pushed) == []


def test_composition_rejects_unconfirmed_commits(q):
    app_commit = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n", login="vibe-office[bot]")
    q.fake.prs[3] = {
        "number": 3, "state": "closed", "merged": True, "merge_commit_sha": app_commit,
        "merged_at": "2026-10-24T12:00:00Z", "merged_by": {"login": "vibe-office[bot]"}, "head": {"ref": "x", "sha": "1" * 40},
    }
    stranger = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n", login="stranger")
    foreign_pr = q.push("apps/chat/web/src/C.tsx", "export const c = 1\n", login="stranger")
    human_pr(q, foreign_pr, 4, login="stranger")
    worker = q.worker()
    problems = worker.queue.composition(q.base, foreign_pr)
    assert [problem[:7] for problem in problems] == [foreign_pr[:7], stranger[:7], app_commit[:7]]
    assert "офис не допускал" in problems[2]
    assert "OFFICE_TRUSTED_GITHUB_LOGINS" in problems[0]
    release = q.candidate(foreign_pr)
    q.settle(worker)
    current = q.release(release.id)
    assert (current.status, q.task(release.task_id).status) == ("pending", "needs_attention")
    assert "Состав выпуска не подтверждён" in current.blocked_reason
    assert q.state()["active_release_id"] is None
    assert q.render.posts() == []


def test_push_activity_follows_pages_and_multi_commit_push(q):
    before = q.origin.main()
    first = q.origin.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    second = q.origin.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    q.fake.pushed(before, second)
    for index in range(150):
        q.fake.pushed("0" * 40, f"{index:040x}", login="stranger")
    worker = q.worker()
    assert worker.queue.composition(q.base, second) == []
    requests = [request for request in q.fake.requests if request.url.path.endswith("/activity")]
    assert len(requests) == 2
    assert first != second


def blocked(q):
    worker, release, sha = failed_verification(q)
    q.render.set("dep-3", "live")
    q.settle(worker)
    assert q.state()["release_blocked_by"] == release.id
    return worker, release, sha


def drive_revert(q, worker, limit=20):
    for _ in range(limit):
        if not worker.revert_step():
            return
    raise AssertionError("revert does not settle")


def test_rollback_starts_revert_pr_that_publishes_fix_and_unblocks(q):
    worker, release, sha = blocked(q)
    later = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    stale = q.candidate(later, base=q.base)
    drive_revert(q, worker)
    current = q.release(release.id)
    assert current.revert.status == "active" and current.revert.pr_number == 1
    pr = q.fake.prs[1]
    assert pr["head"]["ref"] == f"office/revert-{release.id}"
    assert pr["title"].startswith("Revert: ")
    assert "Причина отката" in pr["body"]
    q.settle(worker)
    assert q.release(stale.id).status == "pending"
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]
    head = current.revert.head_sha
    q.fake.add_pr_runs(head, 1)
    drive_revert(q, worker)
    current = q.release(release.id)
    fix = current.revert.fix_sha
    assert fix == q.origin.main()
    assert current.revert.status == "active"
    [review] = q.gateway.role_calls("pr_reviewer", release.task_id)
    assert "Причина отката" in review.user and "Revert выпуска" in review.user
    q.fake.add_push_runs(fix)
    drive_revert(q, worker)
    current = q.release(release.id)
    assert current.revert.status == "merged"
    [candidate] = [item for item in q.store.list_releases() if item.commit_sha == fix]
    assert (candidate.origin, candidate.task_id, candidate.expected_previous_sha) == (
        ReleaseOrigin.auto_revert, release.task_id, q.base
    )
    assert q.suite.calls[-1] == (release.task_id, fix, fix, False)
    assert not q.workspace.worktree_path(f"revert-{release.id}").exists()
    q.settle(worker)
    deploy_id = q.release(candidate.id).deploy.render_deploy_id
    q.render.set(deploy_id, "live")
    q.settle(worker)
    assert q.release(candidate.id).status == "released"
    assert q.state()["release_blocked_by"] is None
    assert q.release(stale.id).status == "superseded"
    assert q.task(release.task_id).status == "failed"
    assert q.events(release.task_id, TaskEventType.release_unblocked)[-1].payload == {
        "release_id": release.id, "fixed_by": candidate.id,
    }
    marked = [event for event in q.events(release.task_id, TaskEventType.pr_opened)]
    assert marked[-1].payload["revert_of"] == release.id
    drive_revert(q, worker)
    assert len(q.fake.prs) == 1


def test_revert_branch_is_created_once_after_restart(q, monkeypatch):
    worker, release, _ = blocked(q)
    real = worker._integrate
    monkeypatch.setattr(worker, "_integrate", lambda c: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        drive_revert(q, worker)
    monkeypatch.setattr(worker, "_integrate", real)
    restarted = q.worker()
    drive_revert(q, restarted)
    with q.store.db.connection() as conn:
        rows = conn.fetchall("SELECT kind, status, attempt FROM operations WHERE key = :key", {"key": f"revert:{release.id}"})
    assert rows == [{"kind": "revert", "status": "succeeded", "attempt": 1}]
    assert list(q.fake.prs) == [1]
    assert q.fake.mutations().count(("POST", "/repos/SlavaPush/vibe-studio/pulls")) == 1


def test_revert_conflict_needs_attention_and_keeps_block(q):
    worker, release, _ = failed_verification(q)
    q.push("apps/chat/web/src/A.tsx", "export const a = 2\n")
    q.render.set("dep-3", "live")
    q.settle(worker)
    drive_revert(q, worker)
    current = q.release(release.id)
    assert current.revert.status == "needs_attention"
    assert "конфликт" in current.revert.reason
    assert q.state()["release_blocked_by"] == release.id
    assert q.fake.prs == {}
    assert q.task(release.task_id).status == "failed"


def roll_back(q, sha, **settings):
    release = q.candidate(sha)
    q.service.broken.add(sha[:7])
    worker = q.worker(**settings)
    q.settle(worker)
    q.render.set("dep-2", "live")
    q.settle(worker)
    q.render.set("dep-3", "live")
    q.settle(worker)
    assert q.state()["release_blocked_by"] == release.id
    return worker, release


def finish_revert(q, worker, release):
    head = q.release(release.id).revert.head_sha
    q.fake.add_pr_runs(head, q.release(release.id).revert.pr_number)
    drive_revert(q, worker)
    fix = q.release(release.id).revert.fix_sha
    q.fake.add_push_runs(fix)
    drive_revert(q, worker)
    assert q.release(release.id).revert.status == "merged"
    [candidate] = [item for item in q.store.list_releases() if item.commit_sha == fix]
    return candidate


def test_revert_undoes_only_office_merges_and_their_qa_test(q):
    first = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    admit(q, first, "1" * 40)
    owner = q.push("apps/office/api/app/x.py", "x = 1\n")
    qa = "apps/chat/web/e2e/office/tsk_merged.pw.ts"
    q.origin.write(qa, "import { test } from '@playwright/test'\n\ntest('a', async () => {})\n")
    second = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    admit(q, second, "2" * 40)
    worker, release = roll_back(q, second)
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.pr_number) == ("active", 1)
    [event] = [item for item in q.events(release.task_id, TaskEventType.tool_called) if item.tool == "git_revert"]
    assert event.payload["arguments"] == f"office/revert-{release.id} {second[:7]} {first[:7]}"
    tree = f"revert-{release.id}"
    assert q.workspace.verify_branch(tree, current.revert.base_sha) == []
    assert sorted(q.workspace.changed_names(tree, current.revert.base_sha, current.revert.head_sha)) == [
        "apps/chat/web/e2e/office/tsk_merged.pw.ts", "apps/chat/web/src/A.tsx", "apps/chat/web/src/B.tsx",
    ]
    candidate = finish_revert(q, worker, release)
    files = q.fake.git("ls-tree", "-r", "--name-only", candidate.commit_sha).splitlines()
    assert "apps/office/api/app/x.py" in files
    assert not {"apps/chat/web/src/A.tsx", "apps/chat/web/src/B.tsx", qa} & set(files)
    assert q.suite.calls[-1] == (release.task_id, candidate.commit_sha, candidate.commit_sha, False)
    assert owner != first


def test_revert_without_office_merges_needs_attention(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    worker, release = roll_back(q, sha)
    drive_revert(q, worker)
    current = q.release(release.id)
    assert current.revert.status == "needs_attention"
    assert "нет слияний PR офиса" in current.revert.reason and "fix_sha" in current.revert.reason
    assert q.state()["release_blocked_by"] == release.id
    assert q.fake.prs == {}
    assert not q.workspace.worktree_path(f"revert-{release.id}").exists()
    assert [(row["key"], row["status"]) for row in q.ops(release.id) if row["kind"] == "revert"] == [
        (f"revert:{release.id}", "failed")
    ]


def test_verify_fault_applies_only_to_task_candidates(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    admit(q, sha)
    worker, release = roll_back(q, sha, verify_fault=True)
    assert q.release(release.id).verify.fault is True
    drive_revert(q, worker)
    candidate = finish_revert(q, worker, release)
    assert candidate.origin == ReleaseOrigin.auto_revert
    q.settle(worker)
    q.render.set(q.release(candidate.id).deploy.render_deploy_id, "live")
    q.settle(worker)
    current = q.release(candidate.id)
    assert (current.status, current.verify.ok, current.verify.fault) == ("released", True, False)
    assert q.state()["release_blocked_by"] is None
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback", "/deploys"]


def test_retry_revert_after_conflict_reuses_unpushed_branch(q):
    worker, release, sha = failed_verification(q)
    q.push("apps/chat/web/src/A.tsx", "export const a = 2\n")
    q.render.set("dep-3", "live")
    q.settle(worker)
    drive_revert(q, worker)
    assert q.release(release.id).revert.status == "needs_attention"
    q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    log = OperationLog(q.store.db)
    stuck, _ = log.begin("push", f"push:revert:{release.id}:{'0' * 40}", release.task_id, release.id)
    log.mark(stuck, OpStatus.unknown)
    refused = q.decide(release.id, "resume_releases", "retry-unknown")
    assert refused.status_code == 409 and "не установлен" in refused.json()["message"]
    log.mark(stuck, OpStatus.failed)
    response = q.decide(release.id, "resume_releases", "retry-revert")
    assert response.status_code == 200
    current = q.release(release.id)
    assert (current.revert.status, current.revert.attempt, current.revert.decision_id) == (
        "pending", 2, response.json()["decision_id"]
    )
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.tree, current.revert.pr_number) == ("active", None, 1)
    assert q.fake.prs[1]["head"]["ref"] == f"office/revert-{release.id}"
    candidate = finish_revert(q, worker, release)
    assert (candidate.origin, candidate.origin_decision_id) == (ReleaseOrigin.auto_revert, response.json()["decision_id"])
    keys = [(row["key"], row["status"]) for row in q.ops(release.id) if row["kind"] == "revert"]
    assert keys == [(f"revert:{release.id}", "failed"), (f"revert:{release.id}:2", "succeeded")]
    assert sha != candidate.commit_sha


def test_retry_revert_closes_pushed_pr_and_survives_restarts(q, monkeypatch):
    worker, release, _ = blocked(q)
    drive_revert(q, worker)
    first = q.release(release.id).revert
    q.fake.add_pr_runs(first.head_sha, 1)
    merge = ("PUT", "/repos/SlavaPush/vibe-studio/pulls/1/merge")
    q.fake.status[merge] = 405
    drive_revert(q, worker)
    current = q.release(release.id)
    assert current.revert.status == "needs_attention" and q.fake.prs[1]["state"] == "open"
    del q.fake.status[merge]
    version = current.version
    response = q.decide(release.id, "resume_releases", "retry-merge")
    assert response.status_code == 200
    replay = q.decide(release.id, "resume_releases", "retry-merge", expected_version=version)
    assert replay.json() == response.json()
    assert q.release(release.id).revert.attempt == 2
    close = q.github.close_pr

    def close_and_crash(number):
        close(number)
        raise Crash()

    monkeypatch.setattr(q.github, "close_pr", close_and_crash)
    with pytest.raises(Crash):
        q.worker().revert_step()
    monkeypatch.setattr(q.github, "close_pr", close)
    assert q.fake.prs[1]["state"] == "closed"
    assert [row["status"] for row in q.ops(release.id) if row["kind"] == "close_pr"] == ["started"]
    real = q.workspace.revert_branch
    monkeypatch.setattr(q.workspace, "revert_branch", lambda *args: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        q.worker().revert_step()
    monkeypatch.setattr(q.workspace, "revert_branch", real)

    def restarted():
        for _ in range(20):
            if not q.worker().revert_step():
                return
        raise AssertionError("revert does not settle")

    restarted()
    current = q.release(release.id)
    assert (current.revert.status, current.revert.tree, current.revert.pr_number) == (
        "active", f"revert-{release.id}-2", 2
    )
    assert q.fake.prs[2]["head"]["ref"] == f"office/revert-{release.id}-2"
    assert not q.workspace.worktree_path(f"revert-{release.id}").exists()
    q.fake.add_pr_runs(current.revert.head_sha, 2)
    restarted()
    fix = q.release(release.id).revert.fix_sha
    q.fake.add_push_runs(fix)
    restarted()
    assert q.release(release.id).revert.status == "merged"
    again = q.decide(release.id, "resume_releases", "retry-merge", expected_version=version)
    assert again.json() == response.json()
    restarted()
    mutations = q.fake.mutations()
    assert mutations.count(("POST", "/repos/SlavaPush/vibe-studio/pulls")) == 2
    assert mutations.count(("PATCH", "/repos/SlavaPush/vibe-studio/pulls/1")) == 1
    assert q.fake.merges() == 2
    with q.store.db.connection() as conn:
        rows = conn.fetchall(
            "SELECT key, status FROM operations WHERE release_id = :id AND kind IN ('revert', 'close_pr') ORDER BY key",
            {"id": release.id},
        )
    assert [(row["key"], row["status"]) for row in rows] == [
        (f"close_pr:revert:{release.id}:2:1", "succeeded"),
        (f"revert:{release.id}", "succeeded"),
        (f"revert:{release.id}:2", "succeeded"),
    ]
    assert [row["key"] for row in q.ops(release.id) if row["status"] in ("started", "unknown")] == []
    [candidate] = [item for item in q.store.list_releases() if item.commit_sha == fix]
    q.settle(q.worker())
    q.render.set(q.release(candidate.id).deploy.render_deploy_id, "live")
    q.settle(q.current)
    assert q.release(candidate.id).status == "released"
    assert q.state()["release_blocked_by"] is None


def refuse_merge(q, worker, release):
    revert = q.release(release.id).revert
    q.fake.add_pr_runs(revert.head_sha, revert.pr_number)
    merge = ("PUT", f"/repos/SlavaPush/vibe-studio/pulls/{revert.pr_number}/merge")
    q.fake.status[merge] = 405
    drive_revert(q, worker)
    del q.fake.status[merge]
    current = q.release(release.id)
    assert current.revert.status == "needs_attention" and q.fake.prs[revert.pr_number]["state"] == "open"


def test_retry_after_rejected_fix_sha_takes_new_attempt_and_closes_open_pr(q):
    worker, release, _ = blocked(q)
    drive_revert(q, worker)
    refuse_merge(q, worker, release)
    assert q.decide(release.id, "resume_releases", "retry-first").status_code == 200
    drive_revert(q, worker)
    assert (q.release(release.id).revert.attempt, q.release(release.id).revert.pr_number) == (2, 2)
    refuse_merge(q, worker, release)
    assert q.decide(release.id, "resume_releases", "fix-missing", fix_sha="0" * 40).status_code == 200
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.source, current.revert.attempt) == ("needs_attention", "resume", 2)
    assert current.revert.tree == f"revert-{release.id}-2"
    assert q.decide(release.id, "resume_releases", "retry-second").status_code == 200
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.attempt, current.revert.pr_number) == ("active", 3, 3)
    assert q.fake.prs[3]["head"]["ref"] == f"office/revert-{release.id}-3"
    assert [q.fake.prs[number]["state"] for number in (1, 2, 3)] == ["closed", "closed", "open"]
    finish_revert(q, worker, release)
    keys = [(row["key"], row["status"]) for row in q.ops(release.id) if row["kind"] in ("revert", "close_pr")]
    assert sorted(keys) == [
        (f"close_pr:revert:{release.id}:2:1", "succeeded"),
        (f"close_pr:revert:{release.id}:3:2", "succeeded"),
        (f"revert:{release.id}", "succeeded"),
        (f"revert:{release.id}:2", "succeeded"),
        (f"revert:{release.id}:3", "succeeded"),
    ]


def test_revert_saved_before_retry_fields_is_read_as_first_attempt(q):
    worker, release, _ = blocked(q)
    saved = q.release(release.id).revert.model_dump(mode="json")
    del saved["attempt"], saved["tree"]
    with q.store.db.locked_transaction() as conn:
        conn.execute("UPDATE release_candidates SET revert = :revert WHERE id = :id", {"revert": json.dumps(saved), "id": release.id})
    current = q.release(release.id)
    assert (current.revert.attempt, current.revert.tree) == (1, None)
    drive_revert(q, worker)
    assert q.fake.prs[1]["head"]["ref"] == f"office/revert-{release.id}"


def test_resume_with_fix_sha_publishes_fix_and_unblocks(q):
    worker, release, sha = failed_verification(q)
    q.push("apps/chat/web/src/A.tsx", "export const a = 2\n")
    q.render.set("dep-3", "live")
    q.settle(worker)
    assert q.release(release.id).revert.status == "pending"
    assert q.decide(release.id, "resume_releases", "resume-no-fix").json()["code"] == "decision_not_allowed"
    drive_revert(q, worker)
    fix = q.push("apps/chat/web/src/A.tsx", "export const a = 3\n")
    newer = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    old = q.candidate(sha, base=q.base, origin=ReleaseOrigin.task)
    outside = q.decide(release.id, "resume_releases", "resume-wrong-target", fix_sha="0" * 40)
    assert outside.status_code == 200
    drive_revert(q, worker)
    assert "не найден" in q.release(release.id).revert.reason
    version = q.release(release.id).version
    response = q.decide(release.id, "resume_releases", "resume-fix", fix_sha=fix)
    assert response.status_code == 200
    again = q.decide(release.id, "resume_releases", "resume-fix", fix_sha=fix, expected_version=version)
    assert again.json() == response.json()
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.source, current.revert.status) == ("resume", "active")
    q.settle(worker)
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]
    q.fake.add_push_runs(fix)
    drive_revert(q, worker)
    [candidate] = [item for item in q.store.list_releases() if item.commit_sha == fix]
    assert (candidate.origin, candidate.origin_decision_id) == (ReleaseOrigin.resume, response.json()["decision_id"])
    before = q.release(release.id)
    repeated = q.decide(release.id, "resume_releases", "resume-fix-again", fix_sha=fix)
    assert repeated.status_code == 409 and repeated.json()["code"] == "decision_not_allowed"
    assert q.release(release.id).revert == before.revert and q.release(release.id).version == before.version
    drive_revert(q, worker)
    q.settle(worker)
    q.render.set(q.release(candidate.id).deploy.render_deploy_id, "live")
    q.settle(worker)
    assert q.release(candidate.id).status == "released"
    assert q.state()["release_blocked_by"] is None
    assert q.release(old.id).status == "superseded"
    assert newer != fix


def resumed_fix(q):
    worker, release, _ = failed_verification(q)
    q.push("apps/chat/web/src/A.tsx", "export const a = 2\n")
    q.render.set("dep-3", "live")
    q.settle(worker)
    drive_revert(q, worker)
    fix = q.push("apps/chat/web/src/A.tsx", "export const a = 3\n")
    other = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    assert q.decide(release.id, "resume_releases", "resume-a", fix_sha=fix).status_code == 200
    q.fake.add_push_runs(fix)
    drive_revert(q, worker)
    [candidate] = [item for item in q.store.list_releases() if item.commit_sha == fix]
    assert q.decide(release.id, "resume_releases", "resume-b", fix_sha=other).status_code == 200
    drive_revert(q, worker)
    assert q.release(release.id).revert.fix_sha == other
    assert q.decide(release.id, "resume_releases", "resume-a-again", fix_sha=fix).status_code == 200
    return worker, release, candidate, other


def test_resume_returning_to_created_fix_publishes_it(q):
    worker, release, candidate, other = resumed_fix(q)
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.candidate_id) == ("merged", candidate.id)
    assert [item.commit_sha for item in q.store.list_releases()].count(candidate.commit_sha) == 1
    assert not [item for item in q.store.list_releases() if item.commit_sha == other]
    q.settle(worker)
    q.render.set(q.release(candidate.id).deploy.render_deploy_id, "live")
    q.settle(worker)
    assert q.release(candidate.id).status == "released"
    assert q.state()["release_blocked_by"] is None


def test_resume_returning_to_rebased_fix_links_pending(q):
    worker, release, candidate, _ = resumed_fix(q)
    assert worker.queue._rebase(candidate, candidate.expected_previous_sha)
    [fresh] = [
        item for item in q.store.list_releases() if item.commit_sha == candidate.commit_sha and item.status == "pending"
    ]
    assert fresh.id != candidate.id
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.candidate_id) == ("merged", fresh.id)
    q.settle(worker)
    q.render.set(q.release(fresh.id).deploy.render_deploy_id, "live")
    q.settle(worker)
    assert q.release(fresh.id).status == "released"
    assert q.state()["release_blocked_by"] is None


def test_resume_returning_to_finished_fix_needs_attention(q):
    worker, release, candidate, _ = resumed_fix(q)
    with q.store.db.locked_transaction() as conn:
        q.store.update_candidate(conn, candidate.id, status="revoked")
    drive_revert(q, worker)
    current = q.release(release.id)
    assert current.revert.status == "needs_attention"
    assert "revoked" in current.revert.reason
    assert q.state()["release_blocked_by"] == release.id


def test_rejected_fix_sha_does_not_release_through_block(q):
    worker, release, sha = failed_verification(q)
    q.push("apps/chat/web/src/A.tsx", "export const a = 2\n")
    q.render.set("dep-3", "live")
    q.settle(worker)
    drive_revert(q, worker)
    assert q.release(release.id).revert.status == "needs_attention"
    later = q.push("apps/chat/web/src/B.tsx", "export const b = 1\n")
    waiting = q.candidate(later, base=q.base)
    response = q.decide(release.id, "resume_releases", "resume-base", fix_sha=q.base)
    assert response.status_code == 200
    drive_revert(q, worker)
    current = q.release(release.id)
    assert (current.revert.status, current.revert.fix_sha) == ("needs_attention", q.base)
    assert "не является потомком" in current.revert.reason
    q.settle(worker)
    assert [path for path, _ in q.render.posts()] == ["/deploys", "/rollback"]
    assert q.release(waiting.id).status == "pending"
    assert q.state()["release_blocked_by"] == release.id


def test_resume_reconciles_only_after_render_finished(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = q.worker()
    q.settle(worker)
    q.render.add(q.base, "build_in_progress")
    q.settle(worker)
    assert q.release(release.id).blocked_reason
    refused = q.decide(release.id, "resume_releases", "resume-running")
    assert refused.status_code == 409
    assert "ещё выполняется" in refused.json()["message"]
    q.render.set("dep-2", "live")
    q.render.set("dep-3", "deactivated")
    q.service.version = sha[:7]
    version = q.release(release.id).version
    response = q.decide(release.id, "resume_releases", "resume-done")
    assert response.status_code == 200
    q.render.add(q.base, "build_in_progress")
    replay = q.decide(release.id, "resume_releases", "resume-done", expected_version=version)
    assert replay.json() == response.json()
    current = q.release(release.id)
    assert (current.status, current.is_published) == ("released", True)
    assert q.task(release.task_id).status == "released"
    assert q.state()["active_release_id"] is None


def test_queue_does_not_start_without_render(q):
    sha = q.push("apps/chat/web/src/A.tsx", "export const a = 1\n")
    release = q.candidate(sha)
    worker = ReleaseWorker(
        q.settings, q.store, q.workspace, q.github, REQUIRED, q.clock, gateway=q.gateway, suite=q.suite,
        service=httpx.Client(transport=httpx.MockTransport(q.service.handler)),
    )
    try:
        assert worker.queue.step() is False
        assert q.release(release.id).status == "pending"
        request = ReleaseDecisionRequest(decision="resume_releases", expected_version=1, idempotency_key="resume-off")
        with pytest.raises(OfficeError) as refused:
            worker.decide(q.admin_participant, release.id, request)
        assert refused.value.status == 409
    finally:
        worker.shutdown()


def test_render_key_does_not_leak(q):
    worker, release, _ = failed_verification(q)
    with q.store.db.connection() as conn:
        rows = [conn.fetchall(f"SELECT * FROM {table}") for table in ("operations", "release_candidates", "task_events")]
    assert "render-test-key" not in json.dumps(rows, default=str)
    assert release.id


def test_api_routes_release_decisions_to_worker(office, origin, tmp_path, app_key, monkeypatch):
    monkeypatch.setattr(ReleaseWorker, "start", lambda self: None)
    app = office.new_app(
        workspace=True, runner=True, target_repo_url=origin.url, workspace_dir=tmp_path / "ws",
        github_app_id="1", github_app_installation_id="2", github_app_private_key_path=app_key.path,
        render_api_key="render-test-key", render_service_id="srv-test",
    )
    admin, me = office.login("Вячеслав", admin=True, app=app)
    assert admin.get("/api/release-queue").json()["configured"] is True
    candidate = office.add_candidate(office.queued_task(me["id"]))
    response = admin.post(
        f"/api/releases/{candidate.id}/decisions",
        json={"decision": "resume_releases", "expected_version": 1, "idempotency_key": "resume-api"},
    )
    assert response.status_code == 409
    assert "не остановлен" in response.json()["message"]
