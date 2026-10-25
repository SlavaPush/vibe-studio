import secrets
from types import SimpleNamespace

import httpx
import pytest
from conftest import UI_TASK, FakeGitHub, FakeRender, FakeService, classification, pr_review_answer
from test_graph import approve, events, transitions, wait_status
from test_server_graph import server_answer

from app import release_queue
from app.config import CHECKOUT_ROOT
from app.github import github_client
from app.release import ReleaseWorker
from app.render import RenderClient
from app.required_checks import load_required_checks
from app.schemas import ChangeClass, Contour, FailureKind, ReleaseOrigin, TaskStatus

pytestmark = pytest.mark.pg

REQUIRED = load_required_checks(CHECKOUT_ROOT)
SERVER_TASK = "Добавить в API чата эндпоинт заметок пользователя и хранить заметки на сервере."
OWNER_SUMMARY = "Добавлен модуль заметок на сервере, схема БД не менялась. Рекомендация: одобрить."
GRAPH_DONE =(TaskStatus.awaiting_integration, TaskStatus.failed, TaskStatus.needs_attention)


class E2E(SimpleNamespace):
    def start(self, **overrides) -> None:
        app = self.office.graph_app(
            self.origin, self.tmp_path, github_push_url=self.origin.url, render_api_key="render-test-key",
            render_service_id="srv-test", verify_seconds=0, **overrides,
        )
        settings = app.state.settings
        self.base = self.origin.main()
        self.fake = FakeGitHub(self.origin.bare, secrets.token_hex(16))
        github = github_client(
            "5452356", "174735588", self.app_key.path, settings.github_repo,
            transport=httpx.MockTransport(self.fake.handler),
        )
        self.service = FakeService(self.base[:7])
        self.render = FakeRender(self.service, self.base)
        render = RenderClient(
            httpx.Client(base_url="https://api.render.com", transport=httpx.MockTransport(self.render.handler)),
            "render-test-key", "srv-test",
        )
        self.worker = ReleaseWorker(
            settings, app.state.store, app.state.workspace, github, REQUIRED,
            gateway=app.state.gateway, suite=app.state.suite,
            service=httpx.Client(transport=httpx.MockTransport(self.service.handler)), render=render,
        )
        app.state.release = self.worker
        app.state.runner.materials.github = github
        app.state.runner.materials.render = render
        self.app, self.store, self.gateway = app, app.state.store, app.state.gateway
        self.client, self.me = self.office.login(app=app)

    def post(self, text: str = UI_TASK) -> str:
        response = self.office.post_task(self.client, text)
        assert response.status_code == 201, response.text
        assert response.json()["rejection_reason"] is None
        task_id = response.json()["id"]
        approve(self.client, self.app, task_id)
        wait_status(self.app, task_id, *GRAPH_DONE)
        assert self.task(task_id).status == TaskStatus.awaiting_integration, events(self.app, task_id)[-3:]
        return task_id

    def task(self, task_id: str):
        response = self.client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200, response.text
        return self.store.get_task(task_id)

    def settle(self, limit: int = 40) -> None:
        worker = self.worker
        for _ in range(limit):
            moved = [worker.step(task_id) for task_id in worker.candidates()]
            moved += [worker.revert_step(), worker.queue.step()]
            if not any(moved):
                return
        raise AssertionError("release worker does not settle")

    def open_pr(self, task_id: str) -> int:
        self.settle()
        task = self.task(task_id)
        assert task.pr_number is not None and task.head_sha == self.fake.ref(f"office/{task_id}")
        return task.pr_number

    def merge(self, task_id: str) -> str:
        task = self.task(task_id)
        assert task.merge_sha == self.origin.main()
        self.fake.add_push_runs(task.merge_sha)
        self.settle()
        return task.merge_sha

    def deploy(self, release_id: str) -> None:
        deploy = self.store.get_candidate(release_id).deploy
        assert deploy is not None and deploy.render_status == "build_in_progress"
        self.render.set(deploy.render_deploy_id, "live")
        self.settle()

    def queue(self) -> dict:
        response = self.client.get("/api/release-queue")
        assert response.status_code == 200, response.text
        return response.json()

    def candidates(self, task_id: str):
        return [item for item in self.store.list_releases() if item.task_id == task_id]


@pytest.fixture
def e2e(office, origin, tmp_path, app_key, monkeypatch):
    monkeypatch.setattr(release_queue, "VERIFY_INTERVAL", 0)
    return E2E(office=office, origin=origin, tmp_path=tmp_path, app_key=app_key)


def kinds(e2e, task_id):
    found = []
    for event in events(e2e.app, task_id):
        payload = event.payload
        detail = payload.get("decision") or payload.get("to") or payload.get("render_status")
        found.append((event.type.value, detail) if detail else (event.type.value,))
    return found


def in_order(found, expected):
    position = 0
    for item in found:
        if position < len(expected) and item == expected[position]:
            position += 1
    return position == len(expected)


def test_ui_task_is_released(e2e):
    e2e.start()
    task_id = e2e.post()
    number = e2e.open_pr(task_id)
    head = e2e.task(task_id).head_sha
    assert e2e.fake.merges() == 0
    e2e.fake.add_pr_runs(head, number)
    e2e.settle()
    merge_sha = e2e.merge(task_id)
    assert e2e.fake.prs[number]["merged"] is True
    [release] = e2e.candidates(task_id)
    assert (release.commit_sha, release.expected_previous_sha, release.origin) == (merge_sha, e2e.base, ReleaseOrigin.task)
    assert e2e.task(task_id).status == TaskStatus.deploying
    e2e.deploy(release.id)
    task = e2e.task(task_id)
    assert task.status == TaskStatus.released
    released = e2e.store.get_candidate(release.id)
    assert (released.status, released.is_published, released.verify.ok) == ("released", True, True)
    assert e2e.service.version == merge_sha[:7]
    assert e2e.render.posts() == [("/deploys", {"commitId": merge_sha})]
    assert e2e.queue()["active_release_id"] is None and e2e.queue()["release_blocked_by"] is None
    assert transitions(e2e.app, task_id) == [
        ("queued", "awaiting_spec_approval"),
        ("awaiting_spec_approval", "developing"),
        ("developing", "reviewing"),
        ("reviewing", "awaiting_integration"),
        ("awaiting_integration", "awaiting_release"),
        ("awaiting_release", "deploying"),
        ("deploying", "released"),
    ]
    assert in_order(kinds(e2e, task_id), [
        ("task_created",),
        ("decision_required", "approve_spec"),
        ("decision_recorded", "approve_spec"),
        ("status_changed", "developing"),
        ("status_changed", "reviewing"),
        ("status_changed", "awaiting_integration"),
        ("pr_opened",),
        ("pr_review",),
        ("pr_merged",),
        ("release_candidate",),
        ("deploy_status", "live"),
        ("status_changed", "released"),
    ]), kinds(e2e, task_id)
    assert task.usage.cost_usd > 0
    assert task.spec.plan
    assert task.pr_review.verdict == "approve" and task.pr_review.head_sha == head
    assert len(e2e.gateway.role_calls("pr_reviewer", task_id)) == 1


def test_pr_rejected_by_agent_closes_pr(e2e):
    e2e.start()
    changes = ["Убрать запрос к внешнему сайту из кнопки копирования."]
    e2e.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer("reject", changes)
    task_id = e2e.post()
    number = e2e.open_pr(task_id)
    e2e.fake.add_pr_runs(e2e.task(task_id).head_sha, number)
    e2e.settle()
    task = e2e.task(task_id)
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.pr_rejected)
    assert e2e.fake.prs[number]["state"] == "closed" and e2e.fake.merges() == 0
    shown = e2e.client.get(f"/api/tasks/{task_id}").json()["pr_review"]
    assert shown["verdict"] == "reject"
    assert shown["reasons"] == "Добавлен внешний запрос." and shown["required_changes"] == changes
    assert e2e.candidates(task_id) == [] and e2e.render.posts() == []
    assert e2e.origin.main() == e2e.base


def test_failed_required_check_closes_pr(e2e):
    e2e.start()
    task_id = e2e.post()
    number = e2e.open_pr(task_id)
    e2e.fake.add_pr_runs(e2e.task(task_id).head_sha, number, chat_web="failure")
    e2e.settle()
    task = e2e.task(task_id)
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.checks_failed)
    assert e2e.fake.prs[number]["state"] == "closed" and e2e.fake.merges() == 0
    assert e2e.gateway.role_calls("pr_reviewer", task_id) == []
    assert e2e.candidates(task_id) == [] and e2e.render.posts() == []


def test_failed_verification_rolls_back_and_reverts(e2e):
    e2e.start(rework_limit=0)
    task_id = e2e.post()
    number = e2e.open_pr(task_id)
    e2e.fake.add_pr_runs(e2e.task(task_id).head_sha, number)
    e2e.settle()
    merge_sha = e2e.merge(task_id)
    [release] = e2e.candidates(task_id)
    e2e.service.broken.add(merge_sha[:7])
    e2e.deploy(release.id)
    rolling = e2e.store.get_candidate(release.id)
    assert rolling.status == "rolling_back" and rolling.rollback.target_deploy_id == "dep-1"
    assert e2e.render.posts()[-1] == ("/rollback", {"deployId": "dep-1"})
    e2e.render.set(rolling.rollback.render_deploy_id, "live")
    e2e.settle()
    failed = e2e.store.get_candidate(release.id)
    assert (failed.status, failed.is_published) == ("failed", False)
    assert e2e.service.version == e2e.base[:7]
    task = e2e.task(task_id)
    assert (task.status, task.failure_kind) == (TaskStatus.failed, FailureKind.verify_failed)
    revert = failed.revert
    assert revert.status == "active" and revert.source == "auto"
    assert e2e.queue()["release_blocked_by"] == release.id and e2e.queue()["blocked_reason"]
    pr = e2e.fake.prs[revert.pr_number]
    assert pr["head"]["ref"] == f"office/revert-{release.id}" and pr["title"].startswith("Revert: ")
    e2e.fake.add_pr_runs(revert.head_sha, revert.pr_number)
    e2e.settle()
    fix = e2e.store.get_candidate(release.id).revert.fix_sha
    assert fix == e2e.origin.main() != merge_sha
    assert e2e.fake.git("rev-parse", f"{fix}^{{tree}}") == e2e.fake.git("rev-parse", f"{e2e.base}^{{tree}}")
    e2e.fake.add_push_runs(fix)
    e2e.settle()
    assert e2e.store.get_candidate(release.id).revert.status == "merged"
    [candidate] = [item for item in e2e.candidates(task_id) if item.commit_sha == fix]
    assert (candidate.origin, candidate.expected_previous_sha) == (ReleaseOrigin.auto_revert, e2e.base)
    e2e.deploy(candidate.id)
    assert e2e.store.get_candidate(candidate.id).status == "released"
    assert e2e.service.version == fix[:7]
    assert e2e.queue()["release_blocked_by"] is None
    task = e2e.task(task_id)
    assert (task.status, task.failure_kind, task.rework_count) == (TaskStatus.failed, FailureKind.verify_failed, 0)
    assert in_order(kinds(e2e, task_id), [
        ("pr_merged",), ("release_candidate",), ("deploy_status", "live"), ("rollback_status", "live"),
        ("release_blocked",), ("pr_opened",), ("pr_merged",), ("release_unblocked",),
    ]), kinds(e2e, task_id)
    assert [path for path, _ in e2e.render.posts()] == ["/deploys", "/rollback", "/deploys"]


def test_server_task_waits_for_owner_approval(e2e, office):
    office.anthropic.answer = classification(change_class="backend_api")
    e2e.start()
    e2e.gateway.handlers["developer"] = server_answer
    e2e.gateway.handlers["pr_reviewer"] = lambda call: pr_review_answer(summary=OWNER_SUMMARY)
    task_id = e2e.post(SERVER_TASK)
    task = e2e.task(task_id)
    assert task.change_class == ChangeClass.backend_api and task.contour == Contour.server
    assert f"apps/chat/api/tests/office/test_{task_id}.py" in task.changed_files
    number = e2e.open_pr(task_id)
    head = e2e.task(task_id).head_sha
    e2e.fake.add_pr_runs(head, number)
    e2e.settle()
    task = e2e.task(task_id)
    assert task.status == TaskStatus.awaiting_integration, events(e2e.app, task_id)[-3:]
    assert (task.owner_review.state, task.owner_review.head_sha) == ("requested", head)
    assert task.pr_review.verdict == "approve" and OWNER_SUMMARY in task.pr_review.owner_summary
    assert OWNER_SUMMARY in e2e.fake.prs[number]["body"]
    e2e.settle()
    assert e2e.fake.merges() == 0 and e2e.task(task_id).merge_sha is None
    e2e.fake.review(number, "SlavaPush", "APPROVED", head)
    e2e.settle()
    assert e2e.fake.merges() == 1
    merge_sha = e2e.merge(task_id)
    [release] = e2e.candidates(task_id)
    e2e.deploy(release.id)
    task = e2e.task(task_id)
    assert task.status == TaskStatus.released
    assert (task.owner_review.state, task.owner_review.login) == ("approved", "SlavaPush")
    assert e2e.service.version == merge_sha[:7]
    assert in_order(kinds(e2e, task_id), [
        ("pr_review",), ("owner_review_requested",), ("owner_review",), ("pr_merged",), ("release_candidate",),
        ("deploy_status", "live"), ("status_changed", "released"),
    ]), kinds(e2e, task_id)
