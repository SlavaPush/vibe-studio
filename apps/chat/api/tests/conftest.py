import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

import anthropic
import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from app import retry
from app.auth import access_code_hash
from app.config import Settings
from app.main import create_app
from app.providers import Classification, TextResult, Usage
from app.storage import StorageError

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://vibe:vibe@127.0.0.1:5432/vibe_test")
SCHEMA = Path(__file__).resolve().parents[1] / "app" / "schema.sql"
ORIGIN = "https://testserver"
DEMO_CODE = "test-demo-code"
DEMO_LABEL = "demo"
PNG = b"\x89PNG\r\n\x1a\nfake-image"
TABLES = "budget_reservations, run_events, attempts, messages, assets, runs, conversations, sessions, users, access_codes"
TERMINAL = {"completed", "refused", "check_unavailable", "failed", "cancelled"}


@pytest.fixture(scope="session")
def database_url() -> str:
    params = conninfo_to_dict(TEST_DATABASE_URL)
    name = params["dbname"]
    try:
        with psycopg.connect(make_conninfo(**{**params, "dbname": "postgres"}), autocommit=True) as conn:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{name}"')
    except psycopg.OperationalError as error:
        pytest.fail(
            f"PostgreSQL для тестов недоступна ({type(error).__name__}). "
            "Запустите docker compose -f infra/docker-compose.yml up -d или задайте TEST_DATABASE_URL.",
            pytrace=False,
        )
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))
    return TEST_DATABASE_URL


@pytest.fixture
def db(database_url, monkeypatch) -> str:
    monkeypatch.setattr(retry, "BASE_DELAY", 0.0)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} CASCADE")
    add_code(database_url, DEMO_LABEL, DEMO_CODE)
    return database_url


def add_code(database_url: str, label: str, code: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO access_codes (id, label, code_hash) VALUES (%s, %s, %s)",
            (str(uuid.uuid4()), label, access_code_hash(code)),
        )


def sql(database_url: str, query: str, params=()):
    with psycopg.connect(database_url, autocommit=True) as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if cursor.description else None


def request_for(url: str = "https://api.test/v1") -> httpx2.Request:
    return httpx2.Request("POST", url)


def status_error(status: int, sdk=anthropic, headers: dict | None = None) -> Exception:
    response = httpx2.Response(status, request=request_for(), headers=headers or {})
    return sdk.APIStatusError("provider error", response=response, body=None)


def wrapped(sdk_error_type, cause: Exception) -> Exception:
    try:
        try:
            raise cause
        except Exception as inner:
            raise sdk_error_type(request=request_for()) from inner
    except Exception as outer:
        return outer


def connect_error(sdk=anthropic) -> Exception:
    return wrapped(sdk.APIConnectionError, httpx2.ConnectError("refused", request=request_for()))


def read_timeout(sdk=anthropic) -> Exception:
    return wrapped(sdk.APITimeoutError, httpx2.ReadTimeout("timeout", request=request_for()))


class AfterChunk:
    def __init__(self, error: Exception) -> None:
        self.error = error


HANG = "hang"


def default_labels(text: str) -> dict:
    lowered = text.lower()
    if "нарисуй" in lowered:
        return {"intent": "image", "complexity": "standard", "risk": "none"}
    if "системный промпт" in lowered:
        return {"intent": "chat", "complexity": "trivial", "risk": "internal_probe"}
    if "сложн" in lowered:
        return {"intent": "chat", "complexity": "complex", "risk": "none"}
    if lowered.strip() in {"?", "это"}:
        return {"intent": "clarify", "complexity": "trivial", "risk": "none"}
    return {"intent": "chat", "complexity": "trivial", "risk": "none"}


class FakeProviders:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures: dict[str, list] = {}
        self.labels = default_labels
        self.scores = lambda text: {"violence": 0.9} if "запрещ" in text.lower() else {"violence": 0.01}
        self.chunks = ["Ответ ", "модели."]
        self.contexts: list[list[dict]] = []
        self.classifier_inputs: list[dict] = []

    def fail(self, key: str, *behaviors) -> None:
        self.failures.setdefault(key, []).extend(behaviors)

    def count(self, kind: str, model: str | None = None) -> int:
        return sum(1 for call_kind, call_model in self.calls if call_kind == kind and model in (None, call_model))

    async def _behavior(self, *keys: str):
        for key in keys:
            queue = self.failures.get(key)
            if queue:
                behavior = queue.pop(0)
                if callable(behavior):
                    behavior = behavior()
                if behavior == HANG:
                    await asyncio.sleep(3600)
                if isinstance(behavior, Exception):
                    raise behavior
                return behavior
        return None

    async def moderate(self, text: str) -> dict[str, float]:
        self.calls.append(("moderation", "omni-moderation-latest"))
        await self._behavior("moderation")
        return self.scores(text)

    async def classify(self, model: str, system: str, data: str, max_tokens: int):
        self.calls.append(("classifier", model))
        behavior = await self._behavior(f"classifier:{model}", "classifier")
        if behavior == "invalid":
            raise retry.InvalidResponse("invalid")
        self.classifier_inputs.append(json.loads(data))
        current = json.loads(data)["current_message"]
        return Classification(**self.labels(current)), Usage(100, 10)

    async def stream_text(self, target, system: str, messages: list[dict], on_delta) -> TextResult:
        self.calls.append(("text", target.model))
        self.contexts.append(messages)
        behavior = await self._behavior(f"text:{target.model}", "text")
        for chunk in self.chunks:
            await on_delta(chunk)
            if isinstance(behavior, AfterChunk):
                raise behavior.error
        return TextResult(Usage(200, 50), behavior == "truncated")

    async def generate_image(self, prompt: str, quality: str):
        self.calls.append(("image", "gpt-image-2.5-flare"))
        await self._behavior("image")
        return PNG, Usage(20, 4000)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.available = True
        self.uploads = 0

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        self.uploads += 1
        if not self.available:
            raise StorageError("down")
        self.objects[path] = data

    async def signed_url(self, path: str) -> str:
        if not self.available or path not in self.objects:
            raise StorageError("down")
        return f"https://storage.test/object/sign/images/{path}?token=signed"

    async def close(self) -> None:
        pass


def settings_for(database_url: str, **overrides) -> Settings:
    values = dict(
        database_url=database_url,
        allowed_origins=(ORIGIN,),
        quota_salt="test-salt",
    )
    values.update(overrides)
    return Settings(**values)


def make_app(database_url: str, providers=None, storage=None, **overrides):
    app = create_app(settings_for(database_url, **overrides))
    app.state.providers = providers or FakeProviders()
    app.state.storage = storage or FakeStorage()
    return app


class User:
    def __init__(self, client: TestClient, token: str, csrf: str, user_id: str) -> None:
        self.client = client
        self.token = token
        self.csrf = csrf
        self.id = user_id

    def headers(self, extra: dict | None = None) -> dict:
        return {"Cookie": f"vc_session={self.token}", "X-CSRF-Token": self.csrf, **(extra or {})}

    def get(self, path: str, **kwargs):
        return self.client.get(path, headers=self.headers(kwargs.pop("headers", None)), **kwargs)

    def post(self, path: str, json=None, **kwargs):
        return self.client.post(path, json=json, headers=self.headers(kwargs.pop("headers", None)), **kwargs)

    def patch(self, path: str, json=None):
        return self.client.patch(path, json=json, headers=self.headers())

    def conversation(self) -> str:
        response = self.post("/api/conversations", {"title": None})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def send(self, conversation_id: str, text: str, model: str = "auto", key: str | None = None):
        body = {"text": text, "model": model, "idempotency_key": key or str(uuid.uuid4())}
        return self.post(f"/api/conversations/{conversation_id}/runs", body)

    def ask(self, text: str, model: str = "auto", conversation_id: str | None = None) -> dict:
        conversation_id = conversation_id or self.conversation()
        response = self.send(conversation_id, text, model)
        assert response.status_code == 202, response.text
        return self.wait(response.json()["id"])

    def wait(self, run_id: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = self.get(f"/api/runs/{run_id}").json()
            if run["status"] in TERMINAL:
                return run
            time.sleep(0.02)
        raise AssertionError(f"run {run_id} did not finish: {run}")

    def events(self, run_id: str, after: int | None = None) -> list[dict]:
        headers = self.headers({"Last-Event-ID": str(after)} if after is not None else None)
        with self.client.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as response:
            assert response.status_code == 200, response.read()
            body = "".join(response.iter_text())
        return [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: ")
        ]


def login(client: TestClient, code: str = DEMO_CODE) -> User:
    response = client.post("/api/demo-session", json={"code": code})
    assert response.status_code == 201, response.text
    token = response.cookies.get("vc_session")
    client.cookies.clear()
    body = response.json()
    return User(client, token, body["csrf_token"], body["user_id"])


def client_for(app) -> TestClient:
    return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})


def seed_run(
    database_url: str,
    *,
    status: str = "running",
    text: str = "Привет",
    requested_model: str = "auto",
    labels: dict | None = None,
    target: tuple[str, str] | None = None,
    assistant_text: str | None = None,
    pending_image: bool = False,
    attempts: list[tuple] = (),
    events: list[str] = ("accepted",),
) -> dict:
    ids = {name: str(uuid.uuid4()) for name in ("user", "conversation", "message", "run", "assistant", "asset")}
    ids["token"] = f"token-{ids['user']}"
    ids["csrf"] = "seed-csrf"
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, source_hash, access_code_id) VALUES (%s, 'seed', (SELECT id FROM access_codes WHERE label = %s))",
            (ids["user"], DEMO_LABEL),
        )
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (%s, %s, %s, now() + interval '1 day')",
            (hashlib.sha256(ids["token"].encode()).hexdigest(), ids["user"], ids["csrf"]),
        )
        conn.execute(
            "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, 'Диалог')", (ids["conversation"], ids["user"])
        )
        conn.execute(
            "INSERT INTO messages (id, conversation_id, user_id, role, text, run_id) VALUES (%s, %s, %s, 'user', %s, %s)",
            (ids["message"], ids["conversation"], ids["user"], text, ids["run"]),
        )
        labels = labels or {}
        conn.execute(
            """
            INSERT INTO runs (id, user_id, conversation_id, source_hash, idempotency_key, request_hash, status,
                              requested_model, user_message_id, intent, complexity, risk, effective_model, effort,
                              assistant_message_id, lease_token, lease_until)
            VALUES (%s, %s, %s, 'seed', %s, 'seed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s::text = 'running' THEN now() - interval '1 minute' END)
            """,
            (
                ids["run"],
                ids["user"],
                ids["conversation"],
                ids["run"],
                status,
                requested_model,
                ids["message"],
                labels.get("intent"),
                labels.get("complexity"),
                labels.get("risk"),
                target[0] if target else None,
                target[1] if target else None,
                ids["assistant"] if assistant_text is not None else None,
                str(uuid.uuid4()) if status == "running" else None,
                status,
            ),
        )
        if assistant_text is not None:
            conn.execute(
                """
                INSERT INTO messages (id, conversation_id, user_id, role, text, run_id, effective_model, effort)
                VALUES (%s, %s, %s, 'assistant', %s, %s, %s, %s)
                """,
                (ids["assistant"], ids["conversation"], ids["user"], assistant_text, ids["run"], target[0], target[1]),
            )
        if pending_image:
            conn.execute(
                """
                INSERT INTO assets (id, user_id, run_id, message_id, mime_type, width, height, storage_path, pending_data)
                VALUES (%s, %s, %s, %s, 'image/png', 1024, 1024, %s, %s)
                """,
                (ids["asset"], ids["user"], ids["run"], ids["assistant"], f"{ids['user']}/{ids['asset']}.png", PNG),
            )
            conn.execute("UPDATE messages SET asset_id = %s WHERE id = %s", (ids["asset"], ids["assistant"]))
        for purpose, model, attempt_status, result in attempts:
            conn.execute(
                """
                INSERT INTO attempts (id, run_id, purpose, provider, model, status, result)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    ids["run"],
                    purpose,
                    "anthropic" if model.startswith("claude") else "openai",
                    model,
                    attempt_status,
                    Jsonb(result) if result is not None else None,
                ),
            )
        for seq, event_type in enumerate(events, start=1):
            conn.execute(
                "INSERT INTO run_events (id, run_id, seq, type, payload) VALUES (%s, %s, %s, %s, '{}')",
                (str(uuid.uuid4()), ids["run"], seq, event_type),
            )
    return ids


def seeded_user(client: TestClient, ids: dict) -> User:
    return User(client, ids["token"], ids["csrf"], ids["user"])
