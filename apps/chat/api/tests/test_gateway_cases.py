import json
import uuid
from pathlib import Path

import pytest
from conftest import FakeProviders, client_for, login, make_app, sql

from app import policy
from app.routing import route

CASES = json.loads((Path(__file__).parent / "data" / "gateway_cases.json").read_text(encoding="utf-8"))
BY_TEXT = {case["text"]: case for case in CASES}


def test_case_set_covers_required_groups():
    assert 20 <= len(CASES) <= 40
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert len(BY_TEXT) == len(CASES)
    risks = {case["labels"]["risk"] for case in CASES}
    intents = {case["labels"]["intent"] for case in CASES}
    assert risks == {"none", "illegal", "internal_probe"}
    assert intents == {"chat", "code", "image", "clarify"}
    assert any(case["moderation_flagged"] for case in CASES)
    with_history = [case for case in CASES if case.get("history")]
    assert len(with_history) >= 6
    assert {case["decision"] for case in with_history} == {"allow", "refuse"}


@pytest.fixture
def gateway_client(db):
    providers = FakeProviders()
    providers.labels = lambda text: BY_TEXT[text]["labels"]
    providers.scores = lambda text: {"violence": 0.99 if BY_TEXT[text]["moderation_flagged"] else 0.0}
    with client_for(make_app(db, providers)) as client:
        yield login(client), providers, db


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_gateway_case(gateway_client, case):
    user, providers, db = gateway_client
    model = case.get("model", "auto")
    history = case.get("history", [])
    conversation_id = user.conversation()
    for row in history:
        sql(
            db,
            "INSERT INTO messages (id, conversation_id, user_id, role, text) VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), conversation_id, user.id, row["role"], row["text"]),
        )
    run = user.ask(case["text"], model=model, conversation_id=conversation_id)
    assert providers.classifier_inputs[0] == json.loads(policy.classifier_input(case["text"], history))
    if history:
        assert providers.classifier_inputs[0]["previous_messages"][-1] == history[-1]
    main_calls = providers.count("text") + providers.count("image")
    labels = case["labels"]
    if case["decision"] == "refuse":
        assert run["status"] == "refused"
        assert main_calls == 0
        return
    assert run["status"] == "completed"
    assert (run["intent"], run["complexity"], run["risk"]) == (labels["intent"], labels["complexity"], labels["risk"])
    if labels["intent"] == "clarify":
        assert main_calls == 0
        assert run["effective_model"] is None
        return
    expected = route(model, labels["intent"], labels["complexity"])
    assert run["effective_model"] == expected.targets[0].model
    assert run["effort"] == expected.targets[0].effort
    assert run["switch_reason"] == expected.switch_reason
    assert main_calls == 1
    if labels["intent"] != "image":
        assert providers.contexts[0] == [
            {"role": row["role"], "content": row["text"]} for row in [*history, {"role": "user", "text": case["text"]}]
        ]
