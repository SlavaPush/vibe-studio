import json

from conftest import FakeProviders, client_for, login, make_app

from app import executor, policy
from app.executor import build_context
from app.routing import route

KEY = "sk-" + "ant-api03-" + "Ab1_" * 12


def test_split_safe_holds_key_split_between_chunks():
    text = "Обычный текст. " * 8 + KEY[:20]
    emitted, held = policy.split_safe(text, final=False)
    assert "sk-ant" not in emitted
    emitted_final, _ = policy.split_safe(held + KEY[20:] + " конец", final=True)
    assert KEY not in emitted + emitted_final
    assert policy.REDACTED in emitted_final


def test_redact_keeps_ordinary_words():
    assert policy.redact("risk-free task-list") == "risk-free task-list"
    assert policy.redact(f"ключ {KEY}") == f"ключ {policy.REDACTED}"
    assert policy.redact("sb_" + "secret_" + "x" * 30) == policy.REDACTED


def test_streamed_key_is_hidden_in_events_and_message(db, monkeypatch):
    monkeypatch.setattr(executor, "SAVE_SECONDS", 0.0)
    providers = FakeProviders()
    providers.chunks = ["Вот ответ. " * 10 + KEY[:18], KEY[18:] + " и продолжение ответа."]
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        conversation_id = user.conversation()
        run = user.ask("Привет", conversation_id=conversation_id)
        deltas = [event["payload"]["delta"] for event in user.events(run["id"]) if event["type"] == "text_delta"]
        assert len(deltas) >= 2
        joined = "".join(deltas)
        assert "sk-ant" not in joined
        assert policy.REDACTED in joined
        stored = user.get(f"/api/conversations/{conversation_id}/messages").json()["items"][-1]["text"]
        assert stored == joined


def test_context_keeps_recent_messages_with_marker():
    rows = [{"role": "user" if index % 2 == 0 else "assistant", "text": "x" * 20_000} for index in range(5)]
    context = build_context(rows)
    assert len(context) == 1
    assert context[0]["role"] == "user"
    assert context[0]["content"].startswith(policy.CONTEXT_OMITTED)
    short = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}, {"role": "user", "text": "c"}]
    assert build_context(short) == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]


def test_history_skips_refused_and_unchecked_requests(db):
    providers = FakeProviders()
    providers.chunks = ["y" * 1500]
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        conversation_id = user.conversation()
        assert user.ask("Привет", conversation_id=conversation_id)["status"] == "completed"
        assert user.ask("Запрещённый вопрос", conversation_id=conversation_id)["status"] == "refused"
        providers.fail("moderation", TimeoutError())
        assert user.ask("Непроверенный вопрос", conversation_id=conversation_id)["status"] == "check_unavailable"
        providers.chunks = ["Ответ ", "модели."]
        assert user.ask("Второй вопрос", conversation_id=conversation_id)["status"] == "completed"
        assert user.ask("а подробнее?", conversation_id=conversation_id)["status"] == "completed"
    assert providers.contexts[-1] == [
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": "y" * 1500},
        {"role": "user", "content": "Второй вопрос"},
        {"role": "assistant", "content": "Ответ модели."},
        {"role": "user", "content": "а подробнее?"},
    ]
    assert providers.classifier_inputs[-1] == {
        "previous_messages": [
            {"role": "user", "text": "Привет"},
            {"role": "assistant", "text": "y" * policy.CLASSIFIER_REPLY_CHARS},
            {"role": "user", "text": "Второй вопрос"},
            {"role": "assistant", "text": "Ответ модели."},
        ],
        "current_message": "а подробнее?",
    }


def test_classifier_input_keeps_last_messages():
    previous = [{"role": "user" if index % 2 == 0 else "assistant", "text": str(index)} for index in range(6)]
    data = json.loads(policy.classifier_input("текущее", previous))
    assert data["previous_messages"] == previous[-policy.CLASSIFIER_HISTORY:]
    assert json.loads(policy.classifier_input("текущее", []))["previous_messages"] == []


def test_route_table():
    assert route("auto", "chat", "trivial").targets[0].model == "claude-haiku-5-5"
    assert route("auto", "chat", "standard").targets[0].effort == "medium"
    assert route("auto", "code", "trivial").targets[0].effort == "high"
    assert [target.model for target in route("auto", "chat", "complex").targets] == ["claude-sonnet-5-5", "gpt-6.1-sol"]
    assert [target.model for target in route("gpt-6-luna", "chat", "trivial").targets] == ["gpt-6-luna", "claude-haiku-5-5"]
    image = route("claude-haiku-5-5", "image", "standard")
    assert [target.model for target in image.targets] == ["gpt-image-2.5-flare"]
    assert image.switch_reason == "intent_mismatch"
    assert route("image", "image", "trivial").switch_reason is None
    assert route("auto", "chat", "trivial").targets[0].max_output_tokens == 2048
    assert route("auto", "chat", "complex").targets[0].max_output_tokens == 16000
