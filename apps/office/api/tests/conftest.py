import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models_gateway import ModelCall, ModelsGateway, ProviderReply
from app.runner import Checkpoints, Runner
from app.sandbox import E2EResult, SandboxCancelled
from app.schemas import ApiError, CheckReport, CheckResult, ModelProfile, ReleaseCandidate, ReleaseKind
from app.tasks import IntakeOutcome, TaskStore

PG_URL = os.environ.get("OFFICE_TEST_POSTGRES_URL")
ACCESS_CODE = "OFFICE-TEST-CODE"
ADMIN_CODE = "ADMIN-TEST-CODE"
BASE_URL = "https://testserver"
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "api.md"
UI_TASK = "Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена."


ORIGIN_FILES = {
    "apps/chat/web/src/App.tsx": "export function App() {\n  return null\n}\n",
    "apps/chat/web/src/App.test.tsx": "import { test } from 'vitest'\n\ntest('app', () => {})\n",
    "apps/chat/web/src/test/setup.ts": "export {}\n",
    "apps/chat/web/package.json": '{"name": "web"}\n',
    "apps/chat/web/package-lock.json": '{"name": "web", "lockfileVersion": 3}\n',
    "apps/chat/web/index.html": "<title>vibe-chat</title>\n",
    "apps/chat/api/main.py": "app = None\n",
    "apps/chat/contracts/api.md": "# API\n",
    "apps/office/policies/paths.yaml": "deny: []\n",
    "apps/office/web/src/Panel.test.tsx": "export {}\n",
    ".github/workflows/ci.yml": "name: CI\n",
    "README.md": "# vibe-studio\n",
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class Origin:
    def __init__(self, root: Path) -> None:
        self.work = root / "origin-src"
        self.bare = root / "origin.git"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        for rel, content in ORIGIN_FILES.items():
            self.write(rel, content)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", "init")
        git(root, "clone", "-q", "--bare", str(self.work), str(self.bare))
        self.url = self.bare.as_uri()

    def write(self, rel: str, content: str) -> None:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def push(self, rel: str, content: str) -> str:
        self.write(rel, content)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", f"update {rel}")
        git(self.work, "push", "-q", str(self.bare), "main")
        return self.main()

    def main(self) -> str:
        return git(self.bare, "rev-parse", "main")


def contract_error_codes() -> dict[str, int]:
    text = CONTRACT.read_text(encoding="utf-8")
    return {code: int(status) for status, code in re.findall(r"^\| (\d{3}) \| `(\w+)` \|", text, re.MULTILINE)}


CONTRACT_CODES = contract_error_codes()


def classification(related: bool = True, change_class: str = "frontend_ui", reason: str = "ok") -> str:
    return json.dumps(
        {"related_to_vibe_chat": related, "change_class": change_class, "reason": reason},
        ensure_ascii=False,
    )


class FakeAnthropic:
    def __init__(self) -> None:
        self.answer = classification()
        self.stop_reason = "end_turn"
        self.error: Exception | None = None
        self.delay = 0.0
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="text", text=self.answer)],
            usage=SimpleNamespace(input_tokens=120, output_tokens=30),
        )


class FakeOpenAI:
    def __init__(self) -> None:
        self.flagged = False
        self.moderation_error: Exception | None = None
        self.moderation_delay = 0.0
        self.answer = classification()
        self.status = "completed"
        self.incomplete_details: Any = None
        self.error: Exception | None = None
        self.delay = 0.0
        self.moderation_calls: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.moderations = SimpleNamespace(create=self._moderate)
        self.responses = SimpleNamespace(create=self._respond)

    def _moderate(self, **kwargs: Any) -> Any:
        self.moderation_calls.append(kwargs)
        if self.moderation_delay:
            time.sleep(self.moderation_delay)
        if self.moderation_error is not None:
            raise self.moderation_error
        return SimpleNamespace(results=[SimpleNamespace(flagged=self.flagged)])

    def _respond(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            status=self.status,
            incomplete_details=self.incomplete_details,
            output_text=self.answer,
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
        )


QA_FILE = re.compile(r"apps/chat/web/e2e/office/tsk_[0-9a-f]+\.pw\.ts")
QA_TEST = 'import { test, expect } from "@playwright/test";\n\ntest("feature", async ({ page }) => {\n  await page.goto("/");\n});\n'
SPEC_ANSWER = {
    "summary": "Под ответом ассистента появляется кнопка копирования.",
    "acceptance_criteria": ["Кнопка копирования видна под ответом.", "Нажатие копирует текст ответа."],
    "needs_research": False,
    "research_questions": [],
}


def developer_answer(content: str, path: str = "apps/chat/web/src/App.tsx", done: bool = True) -> dict[str, Any]:
    return {
        "actions": [{"kind": "write_file", "path": path, "content": content, "step": None}],
        "done": done,
        "summary": "Кнопка копирования ответа",
    }


def qa_answer(call: ModelCall, criteria: list[int] | None = None, findings: list | None = None) -> dict[str, Any]:
    path = QA_FILE.search(call.user).group(0)
    return {
        "actions": [{"kind": "write_file", "path": path, "content": QA_TEST, "step": None}],
        "done": True,
        "summary": "Тест кнопки копирования",
        "criteria_covered": [1] if criteria is None else criteria,
        "findings": findings or [],
    }


class ScriptedGateway(ModelsGateway):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, anthropic_client=FakeAnthropic(), openai_client=FakeOpenAI())
        self.calls: list[ModelCall] = []
        self.lock = threading.Lock()
        self.versions = 0
        self.handlers: dict[str, Callable[[ModelCall], Any]] = {
            "supervisor": lambda call: SPEC_ANSWER,
            "researcher": lambda call: {"urls": []},
            "developer": self.next_version,
            "qa": qa_answer,
            "critic": lambda call: {"verdict": "pass", "findings": []},
        }

    def next_version(self, call: ModelCall) -> dict[str, Any]:
        with self.lock:
            self.versions += 1
            number = self.versions
        return developer_answer(f"export function App() {{\n  return 'v{number}'\n}}\n")

    def call_provider(self, call: ModelCall) -> ProviderReply:
        with self.lock:
            self.calls.append(call)
        answer = self.handlers[call.role.value](call)
        if isinstance(answer, ProviderReply):
            return answer
        return ProviderReply(json.dumps(answer, ensure_ascii=False), 100, 20, True)

    def role_calls(self, role: str, task_id: str | None = None) -> list[ModelCall]:
        with self.lock:
            return [
                call for call in self.calls
                if call.role.value == role and (task_id is None or call.task_id == task_id)
            ]


SUITE_COMMANDS = {
    "lint": "npm run lint",
    "test": "npm run test",
    "build": "npm run build",
    "e2e": "playwright test --config /office-e2e/playwright.config.ts",
}


class FakeSuite:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str, str, bool]] = []
        self.exits: Callable[[str, str, str, bool], dict[str, int]] = lambda task_id, app, qa, e2e: {}
        self.e2e: Callable[[str, str, str], E2EResult] = self.default_e2e
        self.on_suite: Callable[[str, str, str, bool], None] | None = None
        self.cancelled: set[str] = set()
        self.stale_removed = 0
        self.lock = threading.Lock()

    def remove_stale(self) -> None:
        self.stale_removed += 1

    def default_e2e(self, task_id: str, app_sha: str, test_file: str) -> E2EResult:
        if app_sha == self.store.get_task(task_id).base_sha:
            return E2EResult("failed", line=4, message="expect(locator).toBeVisible() failed")
        return E2EResult("passed")

    def check_cancelled(self, task_id: str) -> None:
        if task_id in self.cancelled:
            raise SandboxCancelled("cancelled")

    def run_suite(self, task_id: str, app_sha: str, qa_sha: str, *, require_e2e: bool = False) -> CheckReport:
        self.check_cancelled(task_id)
        with self.lock:
            self.calls.append((task_id, app_sha, qa_sha, require_e2e))
        if self.on_suite is not None:
            self.on_suite(task_id, app_sha, qa_sha, require_e2e)
        exits = self.exits(task_id, app_sha, qa_sha, require_e2e)
        steps = ["lint", "test", "build"] + (["e2e"] if require_e2e else [])
        task = self.store.get_task(task_id)
        report = CheckReport(
            task_id=task_id,
            commit_sha=app_sha,
            spec_hash=task.spec.spec_hash if task.spec else None,
            suite_hash="sha256:fake",
            checks=[
                CheckResult(command=SUITE_COMMANDS[step], exit_code=exits.get(step, 0), duration_ms=1, artifact_urls=[])
                for step in steps
            ],
        )
        self.store.add_check_report(report)
        return report

    def e2e_result(self, task_id: str, report: CheckReport, test_file: str) -> E2EResult:
        return self.e2e(task_id, report.commit_sha, test_file)

    def run_step(self, task_id: str, step: str, role: Any) -> CheckResult:
        self.check_cancelled(task_id)
        return CheckResult(command=SUITE_COMMANDS.get(step, step), exit_code=0, duration_ms=1, artifact_urls=[])

    def cancel_task(self, task_id: str) -> None:
        self.cancelled.add(task_id)

    def reset_task(self, task_id: str) -> None:
        self.cancelled.discard(task_id)

    def has_containers(self, task_id: str) -> bool:
        return False


def wait_for(predicate: Callable[[], Any], timeout: float = 30.0, message: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timeout waiting for {message}")
        time.sleep(0.02)


def check_error_body(response: Any) -> None:
    if response.status_code >= 400:
        response.read()
        error = ApiError.model_validate(response.json())
        assert error.code in CONTRACT_CODES, error.code
        assert error.operation_id.startswith("op_")
        if response.status_code == 429:
            assert int(response.headers["Retry-After"]) >= 1


class Office:
    def __init__(self, database_url: str, tmp_path: Path) -> None:
        self.anthropic = FakeAnthropic()
        self.openai = FakeOpenAI()
        self.settings = Settings(
            database_url=database_url,
            access_code=ACCESS_CODE,
            admin_code=ADMIN_CODE,
            allowed_hosts=("testserver",),
            allowed_origins=(BASE_URL,),
            artifacts_dir=tmp_path / "artifacts",
            sse_ping_seconds=15.0,
            sse_poll_seconds=0.2,
        )
        self.apps: list[FastAPI] = []
        self.app = self.new_app()

    def new_app(
        self,
        *,
        workspace: bool = False,
        runner: bool = False,
        gateway: ModelsGateway | None = None,
        **overrides: Any,
    ) -> FastAPI:
        settings = replace(self.settings, **overrides)
        if gateway is None:
            gateway = ModelsGateway(settings, anthropic_client=self.anthropic, openai_client=self.openai)
        app = create_app(settings, gateway=gateway, workspace=workspace, runner=runner)
        self.apps.append(app)
        return app

    @property
    def store(self) -> TaskStore:
        return self.app.state.store

    def client(self, app: FastAPI | None = None, **kwargs: Any) -> TestClient:
        client = TestClient(app or self.app, base_url=BASE_URL, headers={"Origin": BASE_URL}, **kwargs)
        client.event_hooks["response"] = [check_error_body]
        return client

    def login(self, name: str = "Мария", *, admin: bool = False, app: FastAPI | None = None) -> tuple[TestClient, dict]:
        client = self.client(app)
        response = client.post(
            "/api/office-session",
            json={"access_code": ADMIN_CODE if admin else ACCESS_CODE, "display_name": name},
        )
        assert response.status_code == 200, response.text
        return client, response.json()

    def post_task(self, client: TestClient, text: str = UI_TASK, profile: str = "hybrid") -> Any:
        return client.post("/api/tasks", json={"text": text, "profile": profile})

    def queued_task(self, owner_id: str, text: str = UI_TASK) -> str:
        task = self.store.create_task(owner_id, text, ModelProfile.hybrid, IntakeOutcome(True, None))
        return task.id

    def add_candidate(self, task_id: str, **overrides: Any) -> ReleaseCandidate:
        fields: dict[str, Any] = {
            "task_id": task_id,
            "kind": ReleaseKind.release,
            "commit_sha": "c" * 40,
            "expected_previous_sha": "a" * 40,
            "spec_hash": "sha256:spec",
            "suite_hash": "sha256:suite",
            "ci_run_ids": ["101"],
            "pr_url": "https://github.com/SlavaPush/vibe-studio/pull/1",
        } | overrides
        with self.store.db.locked_transaction() as conn:
            return self.store.create_candidate(conn, **fields)

    def set_task(self, task_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self.store.db.transaction() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", fields | {"id": task_id})

    def workspace_app(self, origin: "Origin", tmp_path: Path) -> FastAPI:
        return self.new_app(workspace=True, target_repo_url=origin.url, workspace_dir=tmp_path / "workspace")

    def graph_app(
        self,
        origin: "Origin",
        tmp_path: Path,
        *,
        gateway: ScriptedGateway | None = None,
        suite: Any = None,
        **overrides: Any,
    ) -> FastAPI:
        app = self.new_app(
            workspace=True, target_repo_url=origin.url, workspace_dir=tmp_path / "workspace", **overrides
        )
        settings = app.state.settings
        app.state.gateway = gateway or ScriptedGateway(settings)
        app.state.suite = suite or FakeSuite(app.state.store)
        app.state.runner = Runner(
            settings,
            app.state.store,
            app.state.workspace,
            app.state.suite,
            app.state.gateway,
            Checkpoints(settings.database_url),
        )
        app.state.runner.recover()
        return app

    def stop_app(self, app: FastAPI, timeout: float = 10) -> None:
        app.state.runner.shutdown(timeout=timeout)
        app.state.workspace.shutdown()

    def start_tasks(self, app: FastAPI, owner_id: str, count: int = 1) -> list[str]:
        store: TaskStore = app.state.store
        ids = [
            store.create_task(owner_id, UI_TASK, ModelProfile.hybrid, IntakeOutcome(True, None)).id
            for _ in range(count)
        ]
        store.promote_queue()
        app.state.workspace.wait_idle()
        return ids

    def close(self) -> None:
        for app in self.apps:
            if getattr(app.state, "release", None) is not None:
                app.state.release.shutdown(timeout=10)
            if app.state.runner is not None:
                app.state.runner.shutdown(timeout=10)
            if app.state.workspace is not None:
                app.state.workspace.shutdown()
            app.state.store.db.close()


REPO_PATH = "/repos/SlavaPush/vibe-studio"
CI_JOBS = ("chat-api", "chat-web", "chat-containers", "office-api", "office-web", "office-sandbox")


class FakeGitHub:
    def __init__(self, bare: Path, token: str) -> None:
        self.bare = bare
        self.token = token
        self.prs: dict[int, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.jobs: dict[int, list[dict[str, Any]]] = {}
        self.suites: dict[int, int] = {}
        self.requests: list[httpx.Request] = []
        self.down = False
        self.lose_create = False
        self.status: dict[tuple[str, str], int] = {}
        self.jwts: list[str] = []
        self.lock = threading.Lock()

    def mutations(self) -> list[tuple[str, str]]:
        return [
            (request.method, request.url.path) for request in self.requests
            if request.method in ("POST", "PATCH") and request.url.path.startswith(REPO_PATH)
        ]

    def ref(self, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=self.bare, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def pr(self, number: int) -> dict[str, Any]:
        pr = self.prs[number]
        if pr["state"] == "open":
            pr["head"]["sha"] = self.ref(pr["head"]["ref"])
        return pr

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        method, path = request.method, request.url.path
        for (status_method, prefix), code in self.status.items():
            if method == status_method and path.startswith(prefix):
                return httpx.Response(code, json={"message": "error"})
        if path.startswith("/app/installations/"):
            self.jwts.append(request.headers["Authorization"].removeprefix("Bearer "))
            return httpx.Response(201, json={"token": self.token, "expires_at": "2099-01-01T00:00:00Z"})
        rel = path.removeprefix(REPO_PATH)
        body = json.loads(request.content) if request.content else {}
        if rel == "/pulls" and method == "GET":
            head = request.url.params["head"].split(":", 1)[1]
            state = request.url.params["state"]
            found = [
                self.pr(number) for number, pr in self.prs.items()
                if pr["head"]["ref"] == head and state in ("all", pr["state"])
            ]
            return httpx.Response(200, json=found)
        if rel == "/pulls" and method == "POST":
            number = len(self.prs) + 1
            pr = {
                "number": number,
                "html_url": f"https://github.com/SlavaPush/vibe-studio/pull/{number}",
                "state": "open",
                "head": {"ref": body["head"]},
                "base": {"ref": body["base"]},
                "title": body["title"],
                "body": body["body"],
            }
            self.prs[number] = pr
            if self.lose_create:
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(201, json=self.pr(number))
        match = re.fullmatch(r"/pulls/(\d+)", rel)
        if match:
            number = int(match.group(1))
            if number not in self.prs:
                return httpx.Response(404, json={"message": "Not Found"})
            pr = self.pr(number)
            if method == "PATCH":
                pr["state"] = body["state"]
            return httpx.Response(200, json=pr)
        match = re.fullmatch(r"/git/ref/heads/(.+)", rel)
        if match:
            sha = self.ref(match.group(1))
            if sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": sha}})
        if rel == "/actions/runs":
            sha = request.url.params["head_sha"]
            return httpx.Response(200, json={"workflow_runs": [run for run in self.runs if run["head_sha"] == sha]})
        match = re.fullmatch(r"/actions/runs/(\d+)/jobs", rel)
        if match:
            return httpx.Response(200, json={"jobs": self.jobs.get(int(match.group(1)), [])})
        match = re.fullmatch(r"/check-suites/(\d+)", rel)
        if match:
            return httpx.Response(200, json={"app": {"id": self.suites.get(int(match.group(1)), 15368)}})
        return httpx.Response(404, json={"message": "Not Found"})

    def add_run(
        self,
        workflow: str,
        event: str,
        sha: str,
        pr_numbers: list[int],
        jobs: dict[str, str | None],
        *,
        run_number: int = 1,
        run_attempt: int = 1,
        app_id: int = 15368,
    ) -> int:
        run_id = 1000 + len(self.runs)
        self.runs.append(
            {
                "id": run_id,
                "path": f".github/workflows/{workflow}",
                "event": event,
                "head_sha": sha,
                "run_number": run_number,
                "run_attempt": run_attempt,
                "status": "completed" if all(jobs.values()) else "in_progress",
                "check_suite_id": 5000 + run_id,
                "pull_requests": [{"number": number} for number in pr_numbers],
                "html_url": f"https://github.com/SlavaPush/vibe-studio/actions/runs/{run_id}",
            }
        )
        self.suites[5000 + run_id] = app_id
        self.jobs[run_id] = [
            {
                "name": name,
                "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion,
                "html_url": f"https://github.com/SlavaPush/vibe-studio/actions/runs/{run_id}/job/{index}",
            }
            for index, (name, conclusion) in enumerate(jobs.items())
        ]
        return run_id

    def add_pr_runs(self, sha: str, number: int, conclusion: str | None = "success", **overrides: str | None) -> None:
        jobs = {name: overrides.get(name.replace("-", "_"), conclusion) for name in CI_JOBS}
        self.add_run("ci.yml", "pull_request", sha, [number], jobs)
        self.add_run(
            "office-paths.yml", "pull_request_target", sha, [number],
            {"office-paths": overrides.get("office_paths", conclusion)},
        )


@pytest.fixture(scope="session")
def app_key(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("github-app") / "app.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return SimpleNamespace(path=path, public=key.public_key())


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'office.sqlite'}"
        return
    if not PG_URL:
        pytest.skip("OFFICE_TEST_POSTGRES_URL не задан")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in PG_URL else "?"
    yield f"{PG_URL}{separator}options=-csearch_path%3D{schema}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def office(database_url: str, tmp_path: Path):
    instance = Office(database_url, tmp_path)
    yield instance
    instance.close()


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path)
