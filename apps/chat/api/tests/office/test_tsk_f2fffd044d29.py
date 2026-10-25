import openai

from conftest import FakeProviders, client_for, login, make_app, sql, status_error

MINI = "gpt-5-mini"
EXISTING = [
    {"id": "claude-haiku-5-5", "provider": "anthropic", "label": "Claude Haiku 5.5", "kind": "text", "selectable": True},
    {"id": "claude-sonnet-5-5", "provider": "anthropic", "label": "Claude Sonnet 5.5", "kind": "text", "selectable": True},
    {"id": "gpt-6-luna", "provider": "openai", "label": "GPT-6 Luna", "kind": "text", "selectable": True},
    {"id": "gpt-6.1-sol", "provider": "openai", "label": "GPT-6.1 Sol", "kind": "text", "selectable": True},
    {"id": "gpt-image-2.5-flare", "provider": "openai", "label": "GPT Image 2.5 Flare", "kind": "image", "selectable": False},
]


def test_catalog_lists_gpt_5_mini(db):
    providers = FakeProviders()
    with client_for(make_app(db, providers)) as client:
        response = client.get("/api/models")
        assert response.status_code == 200
        items = {item["id"]: item for item in response.json()["items"]}
        assert MINI in items
        assert items[MINI] == {"id": MINI, "provider": "openai", "label": "GPT-5 mini", "kind": "text", "selectable": True}


def test_existing_catalog_entries_unchanged(db):
    with client_for(make_app(db)) as client:
        items = client.get("/api/models").json()["items"]
        others = [item for item in items if item["id"] != MINI]
        assert others == EXISTING
        assert len(items) == len(EXISTING) + 1


def test_run_with_gpt_5_mini_uses_fake_provider(db):
    providers = FakeProviders()
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        run = user.ask("Привет", model=MINI)
        assert run["status"] == "completed", run
        assert run["requested_model"] == MINI
        assert run["effective_model"] == MINI
        assert run["switch_reason"] is None
        assert run["error"] is None
        assert providers.count("text", MINI) == 1
        messages = user.get(f"/api/conversations/{run['conversation_id']}/messages").json()
        assistant = [m for m in messages["items"] if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["effective_model"] == MINI
        assert assistant[0]["text"] == "Ответ модели."


def test_gpt_5_mini_budget_uses_existing_fields(db):
    providers = FakeProviders()
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        run = user.ask("Привет", model=MINI)
        assert run["status"] == "completed", run
        attempts = sql(db, "SELECT provider, status, input_tokens, output_tokens FROM attempts WHERE run_id = %s AND purpose = 'main' AND model = %s", (run["id"], MINI))
        assert len(attempts) == 1
        assert attempts[0][0] == "openai"
        assert attempts[0][1] == "succeeded"
        assert (attempts[0][2], attempts[0][3]) == (200, 50)
        reservations = sql(db, "SELECT state, input_tokens, output_tokens FROM budget_reservations WHERE run_id = %s AND purpose = 'main'", (run["id"],))
        assert len(reservations) == 1
        assert tuple(reservations[0]) == ("settled", 200, 50)


def test_budget_schema_columns_unchanged(db):
    rows = sql(db, "SELECT column_name FROM information_schema.columns WHERE table_name = 'budget_reservations'")
    columns = {row[0] for row in rows}
    expected = {"id", "run_id", "attempt_id", "user_id", "source_hash", "purpose", "runs", "images", "reserved_tokens", "input_tokens", "output_tokens", "state", "created_at"}
    assert expected <= columns


def test_unknown_model_still_rejected(db):
    providers = FakeProviders()
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        conversation = user.conversation()
        for bad in ("gpt-5-mini-x", "GPT-5 mini", "gpt-7"):
            response = user.send(conversation, "Привет", model=bad)
            assert response.status_code == 422, bad
            body = response.json()
            assert set(body) == {"code", "message", "retryable", "operation_id"}
            assert body["code"] == "validation_error"
            assert body["retryable"] is False
        assert providers.count("text") == 0


def test_existing_model_still_works(db):
    providers = FakeProviders()
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        run = user.ask("Привет", model="claude-haiku-5-5")
        assert run["status"] == "completed", run
        assert run["effective_model"] == "claude-haiku-5-5"
        assert providers.count("text", "claude-haiku-5-5") == 1
        assert providers.count("text", MINI) == 0


def test_gpt_5_mini_falls_back_to_haiku(db):
    providers = FakeProviders()
    providers.fail(f"text:{MINI}", status_error(400, sdk=openai))
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        run = user.ask("Привет", model=MINI)
        assert run["status"] == "completed", run
        assert run["effective_model"] == "claude-haiku-5-5"
        assert run["switch_reason"] == "provider_fallback"
        assert providers.count("text", MINI) == 1
        assert providers.count("text", "claude-haiku-5-5") == 1
