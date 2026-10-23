from conftest import FakeProviders, FakeStorage, client_for, make_app, seed_run, seeded_user, sql

CHAT = {"intent": "chat", "complexity": "trivial", "risk": "none"}
IMAGE = {"intent": "image", "complexity": "standard", "risk": "none"}
MODERATION_OK = ("moderation", "omni-moderation-latest", "succeeded", {"scores": {"violence": 0.0}, "flagged": False})
CLASSIFIER_OK = ("classifier", "claude-haiku-5-5", "succeeded", CHAT)


def recover(db, ids, providers=None, storage=None):
    providers = providers or FakeProviders()
    with client_for(make_app(db, providers, storage)) as client:
        user = seeded_user(client, ids)
        run = user.wait(ids["run"])
        events = user.events(ids["run"])
        messages = user.get(f"/api/conversations/{ids['conversation']}/messages").json()
    return run, events, messages, providers


def test_after_classification_classifier_is_not_called_again(db):
    ids = seed_run(db, labels=CHAT, attempts=[MODERATION_OK, CLASSIFIER_OK], events=["accepted", "checks_passed"])
    run, events, _, providers = recover(db, ids)
    assert run["status"] == "completed"
    assert providers.count("moderation") == 0
    assert providers.count("classifier") == 0
    assert providers.count("text") == 1
    assert [event["type"] for event in events][:3] == ["accepted", "checks_passed", "model_selected"]


def test_successful_checks_before_saved_decision_are_reused(db):
    ids = seed_run(db, attempts=[MODERATION_OK, CLASSIFIER_OK])
    run, events, _, providers = recover(db, ids)
    assert run["status"] == "completed"
    assert providers.count("classifier") == 0 and providers.count("moderation") == 0
    assert [event["type"] for event in events].count("accepted") == 1


def test_generated_image_is_uploaded_without_new_generation(db):
    ids = seed_run(
        db,
        labels=IMAGE,
        target=("gpt-image-2.5-flare", "unsupported"),
        assistant_text="",
        pending_image=True,
        attempts=[MODERATION_OK, CLASSIFIER_OK, ("main", "gpt-image-2.5-flare", "succeeded", None)],
        events=["accepted", "checks_passed", "model_selected"],
    )
    storage = FakeStorage()
    run, events, messages, providers = recover(db, ids, storage=storage)
    assert run["status"] == "completed"
    assert providers.count("image") == 0
    assert storage.uploads == 1
    assert [event["type"] for event in events][-2:] == ["asset_ready", "completed"]
    assert messages["items"][-1]["asset"]["id"] == ids["asset"]
    assert sql(db, "SELECT pending_data IS NULL FROM assets") == [(True,)]


def test_saved_text_is_completed_without_provider_call(db):
    ids = seed_run(
        db,
        labels=CHAT,
        target=("claude-haiku-5-5", "low"),
        assistant_text="Готовый ответ",
        attempts=[MODERATION_OK, CLASSIFIER_OK, ("main", "claude-haiku-5-5", "succeeded", None)],
        events=["accepted", "checks_passed", "model_selected", "text_delta"],
    )
    run, events, messages, providers = recover(db, ids)
    assert run["status"] == "completed"
    assert providers.count("text") == 0
    assert events[-1]["payload"]["message"]["text"] == "Готовый ответ"
    assert messages["items"][-1]["text"] == "Готовый ответ"


def test_started_main_attempt_gives_outcome_unknown_without_call(db):
    ids = seed_run(
        db,
        labels=CHAT,
        target=("claude-haiku-5-5", "low"),
        assistant_text="Частичный",
        attempts=[MODERATION_OK, CLASSIFIER_OK, ("main", "claude-haiku-5-5", "started", None)],
    )
    run, events, messages, providers = recover(db, ids)
    assert run["status"] == "failed"
    assert run["error"]["code"] == "provider_outcome_unknown"
    assert providers.calls == []
    assert events[-1]["type"] == "failed"
    assert messages["items"][-1]["text"] == "Частичный"
    assert sql(db, "SELECT status FROM attempts WHERE purpose = 'main'") == [("unknown",)]


def test_started_classifier_attempt_gives_check_unavailable(db):
    ids = seed_run(db, attempts=[MODERATION_OK, ("classifier", "claude-haiku-5-5", "started", None)])
    run, events, _, providers = recover(db, ids)
    assert run["status"] == "check_unavailable"
    assert providers.calls == []
    assert events[-1]["type"] == "check_unavailable"


def test_refused_main_attempt_before_retry_is_not_continued(db):
    for status in ("refused", "rejected"):
        ids = seed_run(
            db,
            labels=CHAT,
            target=("claude-haiku-5-5", "low"),
            assistant_text="",
            attempts=[MODERATION_OK, CLASSIFIER_OK, ("main", "claude-haiku-5-5", status, None)],
        )
        run, _, _, providers = recover(db, ids)
        assert run["status"] == "failed"
        assert run["error"]["code"] == "provider_unavailable"
        assert run["error"]["retryable"] is True
        assert providers.calls == []


def test_invalid_classifier_answer_finishes_run_and_next_request_is_accepted(db):
    ids = seed_run(db, attempts=[MODERATION_OK, ("classifier", "claude-haiku-5-5", "invalid", None)])
    providers = FakeProviders()
    with client_for(make_app(db, providers)) as client:
        user = seeded_user(client, ids)
        assert user.wait(ids["run"])["status"] == "check_unavailable"
        assert providers.calls == []
        response = user.send(ids["conversation"], "Привет")
        assert response.status_code == 202
        assert user.wait(response.json()["id"])["status"] == "completed"


def test_free_moderation_only_is_requeued(db):
    ids = seed_run(db, attempts=[("moderation", "omni-moderation-latest", "refused", None)])
    run, _, _, providers = recover(db, ids)
    assert run["status"] == "completed"
    assert providers.count("moderation") == 1
    assert providers.count("classifier") == 1


def test_cancel_requested_run_is_cancelled_on_recovery(db):
    ids = seed_run(db)
    sql(db, "UPDATE runs SET cancel_requested = true")
    run, _, _, providers = recover(db, ids)
    assert run["status"] == "cancelled"
    assert providers.calls == []
