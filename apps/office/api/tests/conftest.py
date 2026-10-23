import json
import os
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models_gateway import ModelsGateway
from app.schemas import ApiError, ModelProfile, ReleaseCandidate, ReleaseKind
from app.tasks import IntakeOutcome, TaskStore

PG_URL = os.environ.get("OFFICE_TEST_POSTGRES_URL")
ACCESS_CODE = "OFFICE-TEST-CODE"
ADMIN_CODE = "ADMIN-TEST-CODE"
BASE_URL = "https://testserver"
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "api.md"
UI_TASK = "Добавить под каждым ответом ассистента кнопку копирования текста ответа в буфер обмена."


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
            output_text=self.answer,
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
        )


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

    def new_app(self, **overrides: Any) -> FastAPI:
        settings = replace(self.settings, **overrides)
        gateway = ModelsGateway(settings, anthropic_client=self.anthropic, openai_client=self.openai)
        app = create_app(settings, gateway=gateway)
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
            "pr_url": "https://github.com/SlavaPush/vibe-chat/pull/1",
        } | overrides
        with self.store.db.locked_transaction() as conn:
            return self.store.create_candidate(conn, **fields)

    def set_task(self, task_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        with self.store.db.transaction() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = :id", fields | {"id": task_id})

    def close(self) -> None:
        for app in self.apps:
            app.state.store.db.close()


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
