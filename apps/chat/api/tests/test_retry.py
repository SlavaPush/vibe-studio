import httpx2
import openai
import pytest
from conftest import (
    AfterChunk,
    FakeProviders,
    client_for,
    connect_error,
    login,
    make_app,
    read_timeout,
    request_for,
    sql,
    status_error,
)

from app import retry


def attempts(db, run_id: str, purpose: str) -> list[tuple]:
    return sql(
        db,
        "SELECT model, status, fallback_reason FROM attempts WHERE run_id = %s AND purpose = %s ORDER BY created_at",
        (run_id, purpose),
    )


def run_with(db, providers: FakeProviders, text: str = "Привет", model: str = "auto"):
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        run = user.ask(text, model=model)
        return run, user.events(run["id"])


def test_classify_error_classes():
    assert retry.classify(connect_error()) == retry.REFUSED
    assert retry.classify(connect_error(openai)) == retry.REFUSED
    assert retry.classify(status_error(429)) == retry.REFUSED
    assert retry.classify(status_error(529)) == retry.REFUSED
    for status in (400, 401, 403, 404, 413, 422):
        assert retry.classify(status_error(status, openai)) == retry.REJECTED
    for status in (500, 502, 503, 504):
        assert retry.classify(status_error(status)) == retry.UNKNOWN
    assert retry.classify(read_timeout()) == retry.UNKNOWN
    assert retry.classify(TimeoutError()) == retry.UNKNOWN
    assert retry.classify(httpx2.RemoteProtocolError("closed", request=request_for())) == retry.UNKNOWN
    assert retry.retry_after(status_error(429, headers={"retry-after": "3"})) == 3.0


@pytest.mark.parametrize("error", [connect_error(), status_error(429), status_error(529)])
def test_confirmed_refusal_is_retried_then_falls_back(db, error):
    providers = FakeProviders()
    providers.fail("text:claude-haiku-5-5", error, error, error)
    run, events = run_with(db, providers)
    assert run["status"] == "completed"
    assert run["effective_model"] == "gpt-6-luna"
    assert run["switch_reason"] == "provider_fallback"
    assert attempts(db, run["id"], "main") == [
        ("claude-haiku-5-5", "refused", None),
        ("claude-haiku-5-5", "refused", None),
        ("claude-haiku-5-5", "refused", None),
        ("gpt-6-luna", "succeeded", "provider_fallback"),
    ]
    types = [event["type"] for event in events]
    assert types.count("model_selected") == 2
    switched = [event for event in events if event["type"] == "model_switched"]
    assert switched[0]["payload"]["switch_reason"] == "provider_fallback"
    selected = [event for event in events if event["type"] == "model_selected"]
    assert selected[0]["payload"]["assistant_message_id"] == selected[1]["payload"]["assistant_message_id"]


def test_refusal_then_success_on_retry(db):
    providers = FakeProviders()
    providers.fail("text:claude-haiku-5-5", status_error(429))
    run, _ = run_with(db, providers)
    assert run["effective_model"] == "claude-haiku-5-5"
    assert [row[1] for row in attempts(db, run["id"], "main")] == ["refused", "succeeded"]


@pytest.mark.parametrize("status", [400, 401])
def test_rejected_request_falls_back_without_retry(db, status):
    providers = FakeProviders()
    providers.fail("text:claude-haiku-5-5", status_error(status))
    run, _ = run_with(db, providers)
    assert run["status"] == "completed"
    assert [row[:2] for row in attempts(db, run["id"], "main")] == [
        ("claude-haiku-5-5", "rejected"),
        ("gpt-6-luna", "succeeded"),
    ]


@pytest.mark.parametrize(
    "behavior",
    [
        read_timeout(),
        status_error(500),
        status_error(503),
        httpx2.RemoteProtocolError("closed", request=request_for()),
        AfterChunk(status_error(429)),
        AfterChunk(httpx2.ReadError("reset", request=request_for())),
    ],
)
def test_unknown_outcome_is_not_retried(db, behavior):
    providers = FakeProviders()
    providers.fail("text:claude-haiku-5-5", behavior)
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        conversation_id = user.conversation()
        run = user.ask("Привет", conversation_id=conversation_id)
        assert run["status"] == "failed"
        assert run["error"]["code"] == "provider_outcome_unknown"
        assert run["error"]["retryable"] is False
        assert providers.count("text") == 1
        assert attempts(db, run["id"], "main") == [("claude-haiku-5-5", "unknown", None)]
        assert sql(db, "SELECT state FROM budget_reservations WHERE purpose = 'main'") == [("unknown",)]
        messages = user.get(f"/api/conversations/{conversation_id}/messages").json()
        assistant = [item for item in messages["items"] if item["role"] == "assistant"]
        expected = "Ответ " if isinstance(behavior, AfterChunk) else ""
        assert [item["text"] for item in assistant] == [expected]
        assert messages["runs"][0]["error"]["code"] == "provider_outcome_unknown"


def test_truncated_answer_keeps_text_and_fails_without_retry(db):
    providers = FakeProviders()
    providers.fail("text:claude-haiku-5-5", "truncated")
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        conversation_id = user.conversation()
        run = user.ask("Привет", conversation_id=conversation_id)
        assert run["status"] == "failed"
        assert run["error"]["code"] == "response_truncated"
        assert run["error"]["retryable"] is False
        assert providers.count("text") == 1
        assert [row[:2] for row in attempts(db, run["id"], "main")] == [("claude-haiku-5-5", "succeeded")]
        assert sql(db, "SELECT state, output_tokens FROM budget_reservations WHERE purpose = 'main'") == [("settled", 50)]
        events = user.events(run["id"])
        assert [event["type"] for event in events][-1] == "failed"
        assert "".join(e["payload"]["delta"] for e in events if e["type"] == "text_delta") == "Ответ модели."
        messages = user.get(f"/api/conversations/{conversation_id}/messages").json()
        assert messages["items"][-1]["text"] == "Ответ модели."
        assert messages["runs"][0]["error"]["code"] == "response_truncated"


def test_exhausted_providers_give_retryable_provider_unavailable(db):
    providers = FakeProviders()
    providers.fail("text:claude-haiku-5-5", status_error(400))
    providers.fail("text:gpt-6-luna", status_error(403, openai))
    run, events = run_with(db, providers)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "provider_unavailable"
    assert run["error"]["retryable"] is True
    assert events[-1]["payload"]["error"]["code"] == "provider_unavailable"


def test_classifier_follows_the_same_error_classes(db):
    providers = FakeProviders()
    providers.fail("classifier:claude-haiku-5-5", connect_error(), connect_error(), connect_error())
    run, _ = run_with(db, providers)
    assert run["status"] == "completed"
    assert [row[:2] for row in attempts(db, run["id"], "classifier")][-1] == ("gpt-6-luna", "succeeded")

    providers = FakeProviders()
    providers.fail("classifier:claude-haiku-5-5", status_error(400))
    run, _ = run_with(db, providers)
    assert run["status"] == "completed"
    assert providers.count("classifier", "gpt-6-luna") == 1

    providers = FakeProviders()
    providers.fail("classifier:claude-haiku-5-5", status_error(500))
    run, _ = run_with(db, providers)
    assert run["status"] == "check_unavailable"
    assert providers.count("classifier") == 1
    assert providers.count("text") == 0


def test_image_errors_follow_the_same_classes(db):
    providers = FakeProviders()
    providers.fail("image", status_error(429, openai))
    run, _ = run_with(db, providers, "Нарисуй кота", "image")
    assert run["status"] == "completed"
    assert providers.count("image") == 2

    providers = FakeProviders()
    providers.fail("image", status_error(500, openai))
    run, _ = run_with(db, providers, "Нарисуй кота", "image")
    assert run["error"]["code"] == "provider_outcome_unknown"
    assert providers.count("image") == 1

    providers = FakeProviders()
    providers.fail("image", status_error(400, openai))
    run, _ = run_with(db, providers, "Нарисуй кота", "image")
    assert run["error"]["code"] == "provider_unavailable"
    assert providers.count("image") == 1
