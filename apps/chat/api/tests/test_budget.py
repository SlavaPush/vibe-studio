import json
import uuid
from concurrent.futures import ThreadPoolExecutor

from conftest import FakeProviders, client_for, login, make_app, sql, status_error

from app import budget
from app.config import Limits
from app.policy import CLASSIFIER_PROMPT, classifier_input
from app.routing import CLASSIFIER_OUTPUT_TOKENS


def classifier_reservation(text: str) -> int:
    data = classifier_input(text, [])
    return budget.input_token_bound(CLASSIFIER_PROMPT, [{"role": "user", "content": data}]) + CLASSIFIER_OUTPUT_TOKENS


def charged(db) -> int:
    return sql(
        db,
        """
        SELECT coalesce(sum(CASE WHEN state = 'settled' THEN input_tokens + output_tokens ELSE reserved_tokens END), 0)
        FROM budget_reservations
        """,
    )[0][0]


def test_input_bound_covers_every_utf8_byte():
    for text in ("привет мир " * 300, "🙂" * 1000, "汉字" * 500):
        messages = [{"role": "user", "content": text}]
        assert budget.input_token_bound("Системный промпт", messages) >= len(text.encode("utf-8"))
    payload = json.dumps({"system": "", "messages": []})
    assert budget.input_token_bound("", []) >= len(payload)


def test_run_quota_is_checked_at_creation(db):
    with client_for(make_app(db, user_limits=Limits(1, 5, 10_000_000))) as client:
        user = login(client)
        user.ask("Привет")
        response = user.send(user.conversation(), "Ещё")
        assert response.status_code == 429
        assert response.json()["code"] == "quota_exceeded"
        assert response.json()["retryable"] is False


def test_source_limit_survives_new_session_and_restart(db):
    settings = dict(source_limits=Limits(2, 25, 50_000_000))
    with client_for(make_app(db, **settings)) as client:
        first = login(client)
        first.ask("Привет")
        first.ask("Привет")
        second = login(client)
        assert second.send(second.conversation(), "Привет").status_code == 429
    with client_for(make_app(db, **settings)) as client:
        third = login(client)
        response = third.send(third.conversation(), "Привет")
        assert response.status_code == 429
        assert response.json()["code"] == "quota_exceeded"


def test_dense_input_exhausts_tokens_before_classifier(db):
    text = "🙂" * 2000
    limit = classifier_reservation(text) - 1
    providers = FakeProviders()
    with client_for(make_app(db, providers, user_limits=Limits(60, 5, limit))) as client:
        user = login(client)
        run = user.ask(text)
        assert run["status"] == "failed"
        assert run["error"]["code"] == "quota_exceeded"
        assert providers.count("classifier") == 0
        assert providers.count("text") == 0
    assert charged(db) <= limit


def test_token_exhaustion_before_fallback_does_not_call_reserve_provider(db):
    providers = FakeProviders()

    def consume_budget():
        user_id = sql(db, "SELECT id FROM users")[0][0]
        run_id = sql(db, "SELECT id FROM runs")[0][0]
        sql(
            db,
            """
            INSERT INTO budget_reservations (id, run_id, user_id, source_hash, purpose, reserved_tokens, state)
            VALUES (%s, %s, %s, 'other', 'main', 999000, 'reserved')
            """,
            (str(uuid.uuid4()), run_id, user_id),
        )
        return status_error(400)

    providers.fail("text:claude-haiku-5-5", consume_budget)
    with client_for(make_app(db, providers)) as client:
        user = login(client)
        run = user.ask("Привет")
        assert run["status"] == "failed"
        assert run["error"]["code"] == "quota_exceeded"
        assert providers.count("text", "claude-haiku-5-5") == 1
        assert providers.count("text", "gpt-6-luna") == 0


def test_parallel_requests_do_not_exceed_source_limits(db):
    text = "Привет"
    limit = classifier_reservation(text) * 2 + 100
    providers = FakeProviders()
    with client_for(make_app(db, providers, source_limits=Limits(3, 25, limit))) as client:
        users = [login(client) for _ in range(6)]
        conversations = [user.conversation() for user in users]
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(lambda pair: pair[0].send(pair[1], text), zip(users, conversations)))
        accepted = [response for response in responses if response.status_code == 202]
        assert len(accepted) == 3
        assert {response.json()["code"] for response in responses if response.status_code != 202} == {"quota_exceeded"}
        for user, response in zip(users, responses):
            if response.status_code == 202:
                user.wait(response.json()["id"])
    assert sql(db, "SELECT count(*) FROM budget_reservations WHERE purpose = 'run'") == [(3,)]
    assert charged(db) <= limit
