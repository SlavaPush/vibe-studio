import copy
import json
import logging
import random
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
import yaml
from conftest import FakeAnthropic, FakeOpenAI

from app.models_gateway import (
    InvalidModelResponse,
    ModelsGateway,
    ProviderError,
    TruncatedModelResponse,
    check_policy,
)
from app.roles import (
    CLIPPED,
    CRITIC,
    CRITIC_API,
    ContextTooLarge,
    CriticReview,
    QA,
    QA_API,
    QA_API_FILES,
    SUPERVISOR,
    SUPERVISOR_API,
    Entry,
    Finding,
    QaTurn,
    ResearchNotes,
    SpecDraft,
    Turn,
    assemble,
    provider_schema,
    size,
)
from app.config import Settings
from app.sandbox import CommandsPolicy
from app.schemas import AgentRole
from app.tasks import BudgetExceeded

TURN = {
    "actions": [{"kind": "read_file", "path": "web/src/App.tsx", "content": None, "step": None}],
    "done": False,
    "summary": "Читаю файл",
}
SPEC = {
    "summary": "s", "acceptance_criteria": ["c"], "plan": ["p"], "needs_research": False, "research_questions": []
}
ROUTES = {
    AgentRole.researcher: ("anthropic/claude-haiku-5-5", "openai/gpt-6-luna", "low", 8192),
    AgentRole.supervisor: ("anthropic/claude-fable-5-1", "openai/gpt-6-astra", "medium", 8192),
    AgentRole.developer: ("anthropic/claude-sonnet-5-5", "openai/gpt-6.1-sol", "high", 32000),
    AgentRole.qa: ("anthropic/claude-sonnet-5-5", "openai/gpt-6.1-sol", "medium", 16000),
    AgentRole.critic: ("anthropic/claude-sonnet-5-5", "openai/gpt-6.1-sol", "medium", 16000),
    AgentRole.pr_reviewer: ("anthropic/claude-sonnet-5-5", "openai/gpt-6.1-sol", "high", 16000),
}
SONNET_COST = (120 * 2.00 + 30 * 10.00) / 1_000_000
SOL_COST = (200 * 2.00 + 40 * 10.00) / 1_000_000


@pytest.fixture
def setup(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    claude = FakeAnthropic()
    gpt = FakeOpenAI()

    def make(**overrides):
        settings = replace(office.settings, **overrides)
        return ModelsGateway(settings, anthropic_client=claude, openai_client=gpt)

    return office.store, task_id, claude, gpt, make


def raw_cost(store, task_id):
    with store.db.connection() as conn:
        return conn.fetchone("SELECT cost_usd FROM tasks WHERE id = :id", {"id": task_id})["cost_usd"]


def broken_claude(claude):
    claude.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


def model_events(store, task_id):
    return [
        event.payload["arguments"]
        for event in store.events_after(task_id, 0, 1000)
        if event.type == "tool_called" and event.tool == "model"
    ]


def test_anthropic_call_uses_effort_schema_and_accounting(setup):
    store, task_id, claude, _, make = setup
    claude.answer = json.dumps(TURN)
    reply = make().structured(store, task_id, AgentRole.developer, "implement", "SYSTEM", "USER", Turn)
    assert reply.actions[0].path == "web/src/App.tsx"
    [call] = claude.calls
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == 32000
    assert call["system"] == "SYSTEM" and call["messages"] == [{"role": "user", "content": "USER"}]
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": provider_schema(Turn)}}
    assert call["timeout"] == 300
    task = store.get_task(task_id)
    assert (task.usage.model_calls, task.usage.input_tokens, task.usage.output_tokens) == (1, 120, 30)
    assert raw_cost(store, task_id) == pytest.approx(SONNET_COST)
    assert task.usage.cost_usd == round(SONNET_COST, 4)
    assert model_events(store, task_id) == ["developer/implement anthropic/claude-sonnet-5-5"]
    [event] = [item for item in store.events_after(task_id, 0, 1000) if item.tool == "model"]
    assert event.payload == {
        "arguments": "developer/implement anthropic/claude-sonnet-5-5",
        "result": "ok",
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "effort": "high",
    }


def test_fallback_is_counted_as_separate_call(setup):
    store, task_id, claude, gpt, make = setup
    broken_claude(claude)
    gpt.answer = json.dumps(TURN)
    make().structured(store, task_id, AgentRole.qa, "qa_try", "S", "U", Turn)
    [call] = gpt.calls
    assert call["model"] == "gpt-6.1-sol"
    assert call["reasoning"] == {"effort": "medium"}
    assert call["max_output_tokens"] == 16000
    assert call["text"]["format"]["strict"] is True
    assert call["text"]["format"]["schema"] == provider_schema(Turn)
    assert store.get_task(task_id).usage.model_calls == 2
    assert raw_cost(store, task_id) == pytest.approx(SOL_COST)
    assert model_events(store, task_id) == [
        "qa/qa_try anthropic/claude-sonnet-5-5",
        "qa/qa_try openai/gpt-6.1-sol",
    ]
    fallback = [item.payload for item in store.events_after(task_id, 0, 1000) if item.tool == "model"][-1]
    assert (fallback["provider"], fallback["model"], fallback["effort"]) == ("openai", "gpt-6.1-sol", "medium")


@pytest.mark.parametrize("role", list(ROUTES))
def test_every_role_routes_to_table_and_fallback(setup, role):
    store, task_id, claude, gpt, make = setup
    primary, fallback, effort, tokens = ROUTES[role]
    gateway = make()
    targets = gateway.targets(role)
    assert [target.name for target in targets] == [primary, fallback]
    assert [(target.effort, target.max_output_tokens) for target in targets] == [(effort, tokens)] * 2
    if primary.startswith("anthropic/"):
        broken_claude(claude)
        gpt.answer = json.dumps(TURN)
        gateway.structured(store, task_id, role, "step", "S", "U", Turn)
        [anthropic_call] = claude.calls
        [openai_call] = gpt.calls
        anthropic_model, openai_model = primary.split("/")[1], fallback.split("/")[1]
    else:
        gpt.error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
        claude.answer = json.dumps(TURN)
        gateway.structured(store, task_id, role, "step", "S", "U", Turn)
        [openai_call] = gpt.calls
        [anthropic_call] = claude.calls
        openai_model, anthropic_model = primary.split("/")[1], fallback.split("/")[1]
    assert (anthropic_call["model"], anthropic_call["output_config"]["effort"], anthropic_call["max_tokens"]) == (
        anthropic_model, effort, tokens
    )
    assert (openai_call["model"], openai_call["reasoning"], openai_call["max_output_tokens"]) == (
        openai_model, {"effort": effort}, tokens
    )


def test_policy_has_one_role_table_without_profiles_and_ollama():
    models = yaml.safe_load(Settings().models_policy.read_text(encoding="utf-8"))
    assert set(models) == {"status", "capabilities", "roles"}
    assert set(models["roles"]) == {role.value for role in AgentRole} | {"intake_filter"}
    assert models["roles"]["intake_filter"] == {
        "provider": "anthropic", "model": "claude-haiku-5-5", "effort": "low", "fallback": "openai/gpt-6-luna"
    }
    assert "ollama" not in Settings().models_policy.read_text(encoding="utf-8")
    prices = {name: capability["price_usd_per_mtok"] for name, capability in models["capabilities"].items()}
    assert prices == {
        "anthropic/claude-haiku-5-5": {"input": 0.10, "output": 0.50},
        "anthropic/claude-sonnet-5-5": {"input": 2.00, "output": 10.00},
        "anthropic/claude-fable-5-1": {"input": 10.00, "output": 50.00},
        "anthropic/claude-opus-5-5": {"input": 4.00, "output": 20.00},
        "openai/gpt-6-astra": {"input": 10.00, "output": 50.00},
        "openai/gpt-5.6-sol": {"input": 5.00, "output": 30.00},
        "openai/gpt-6-luna": {"input": 0.10, "output": 0.50},
        "openai/gpt-6.1-sol": {"input": 2.00, "output": 10.00},
    }
    assert all(capability["price_source"] for capability in models["capabilities"].values())


def test_policy_without_role_or_capability_fails_at_start(tmp_path, office):
    models = yaml.safe_load(Settings().models_policy.read_text(encoding="utf-8"))
    check_policy(models)
    without_role = copy.deepcopy(models)
    del without_role["roles"]["qa"]
    with pytest.raises(ValueError, match="no route for role qa"):
        check_policy(without_role)
    unknown = copy.deepcopy(models)
    unknown["roles"]["critic"]["fallback"] = "openai/gpt-unknown"
    with pytest.raises(ValueError, match="openai/gpt-unknown of role critic"):
        check_policy(unknown)
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(without_role), encoding="utf-8")
    with pytest.raises(ValueError):
        ModelsGateway(replace(office.settings, models_policy=path))


def test_cost_counts_input_and_output_separately(setup, caplog):
    gateway = setup[-1]()
    assert gateway.cost("anthropic/claude-sonnet-5-5", 1_000_000, 0) == pytest.approx(2.00)
    assert gateway.cost("anthropic/claude-sonnet-5-5", 0, 1_000_000) == pytest.approx(10.00)
    assert gateway.cost("anthropic/claude-haiku-5-5", 1_000_000, 0) == pytest.approx(0.10)
    assert gateway.cost("anthropic/claude-haiku-5-5", 0, 1_000_000) == pytest.approx(0.50)
    assert gateway.cost("openai/gpt-6-luna", 500_000, 2_000_000) == pytest.approx(0.05 + 1.00)
    assert gateway.cost("openai/gpt-6.1-sol", 250_000, 100_000) == pytest.approx(0.50 + 1.00)
    with caplog.at_level(logging.WARNING, logger="office.models"):
        assert gateway.cost("openai/gpt-free", 1000, 1000) == 0.0
    assert "openai/gpt-free has no price" in caplog.text


def test_model_without_price_adds_tokens_but_not_cost(setup, caplog):
    store, task_id, claude, _, make = setup
    gateway = make()
    del gateway.models["capabilities"]["anthropic/claude-sonnet-5-5"]["price_usd_per_mtok"]
    claude.answer = json.dumps(TURN)
    with caplog.at_level(logging.WARNING, logger="office.models"):
        gateway.structured(store, task_id, AgentRole.developer, "implement", "S", "U", Turn)
    task = store.get_task(task_id)
    assert (task.usage.input_tokens, task.usage.output_tokens, raw_cost(store, task_id)) == (120, 30, 0)
    assert "anthropic/claude-sonnet-5-5 has no price" in caplog.text


def test_qa_prompt_requires_mock_module_and_exact_locators():
    for rule in (
        'import { mockApi, fixtures } from "./support/mock-api"',
        "await mockApi(page)",
        "exact: true",
        'page.getByLabel("Сообщение", { exact: true })',
        "не кликай и не жди элементы, которых нет в подмене",
        "expect(...).toBeVisible()",
        "падать на проверке expect, а не на ошибке локатора",
        'page.goto("/")',
        "без сети",
        "Тест пишется в файл, указанный в данных",
        "Тесты прежних задач, которые не проходят на версии задачи",
        "Ослаблять или удалять проверки, не связанные с задачей, нельзя.",
    ):
        assert rule in QA
    assert "apps/chat/web/e2e/office/support/mock-api.ts" in QA_API_FILES


def test_both_providers_failing_is_provider_error(setup):
    store, task_id, claude, gpt, make = setup
    broken_claude(claude)
    gpt.error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    with pytest.raises(ProviderError):
        make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
    assert store.get_task(task_id).usage.model_calls == 2
    assert raw_cost(store, task_id) == 0


def test_supervisor_reply_requires_short_plan():
    assert SpecDraft.model_validate(SPEC).plan == ["p"]
    schema = provider_schema(SpecDraft)
    assert "plan" in schema["required"] and schema["properties"]["plan"]["type"] == "array"
    for broken in ({"plan": []}, {"plan": ["p"] * 9}, {"plan": [" "]}, {"plan": ["п" * 301]}):
        with pytest.raises(ValueError):
            SpecDraft.model_validate(SPEC | broken)
    without = dict(SPEC)
    del without["plan"]
    with pytest.raises(ValueError):
        SpecDraft.model_validate(without)


def test_budget_is_checked_before_each_call(setup, office):
    store, task_id, claude, gpt, make = setup
    office.set_task(task_id, model_calls=30)
    with pytest.raises(BudgetExceeded):
        make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
    assert claude.calls == []

    office.set_task(task_id, model_calls=29)
    broken_claude(claude)
    with pytest.raises(BudgetExceeded):
        make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
    assert len(claude.calls) == 1 and gpt.calls == []
    assert store.get_task(task_id).usage.model_calls == 30


def test_budget_uses_limit_of_the_task(setup, office):
    store, task_id, claude, _, make = setup
    office.set_task(task_id, model_calls=30, model_calls_limit=60)
    claude.answer = json.dumps(TURN)
    make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
    assert store.get_task(task_id).usage.model_calls == 31
    office.set_task(task_id, model_calls=60)
    with pytest.raises(BudgetExceeded):
        make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
    assert len(claude.calls) == 1


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"actions": [{"kind": "write_file", "path": "web/src/App.tsx"}], "done": True, "summary": "x"}),
        json.dumps({"actions": [{"kind": "rm", "path": "x", "content": None, "step": None}], "done": True, "summary": "x"}),
        json.dumps(TURN | {"extra": 1}),
    ],
)
def test_answer_outside_schema_is_rejected(setup, answer):
    store, task_id, claude, _, make = setup
    claude.answer = answer
    with pytest.raises(InvalidModelResponse):
        make().structured(store, task_id, AgentRole.developer, "implement", "S", "U", Turn)
    assert len(claude.calls) == 2
    task = store.get_task(task_id)
    assert task.usage.output_tokens == 60 and task.usage.model_calls == 2
    assert raw_cost(store, task_id) == pytest.approx(2 * SONNET_COST)


def test_openai_answer_outside_schema_is_counted(setup):
    store, task_id, claude, gpt, make = setup
    broken_claude(claude)
    gpt.answer = "not json"
    with pytest.raises(InvalidModelResponse):
        make().structured(store, task_id, AgentRole.qa, "qa_try", "S", "U", Turn)
    assert len(claude.calls) == 1 and len(gpt.calls) == 2
    task = store.get_task(task_id)
    assert (task.usage.input_tokens, task.usage.output_tokens) == (400, 80)
    assert raw_cost(store, task_id) == pytest.approx(2 * SOL_COST)


def test_incomplete_anthropic_answer_is_rejected(setup):
    store, task_id, claude, _, make = setup
    claude.answer = json.dumps(TURN)
    claude.stop_reason = "pause_turn"
    with pytest.raises(InvalidModelResponse) as error:
        make().structured(store, task_id, AgentRole.developer, "implement", "S", "U", Turn)
    assert not isinstance(error.value, TruncatedModelResponse)
    assert len(claude.calls) == 2
    assert "incomplete answer" in claude.calls[1]["messages"][0]["content"]


def answer_in_turn(claude, answers):
    create = claude.messages.create
    queue = iter(answers)

    def step(**kwargs):
        claude.answer, claude.stop_reason = next(queue)
        return create(**kwargs)

    claude.messages = SimpleNamespace(create=step)


def test_valid_second_answer_is_accepted_after_schema_error(setup):
    store, task_id, claude, gpt, make = setup
    answer_in_turn(claude, [(json.dumps(TURN | {"extra": 1}), "end_turn"), (json.dumps(TURN), "end_turn")])
    reply = make().structured(store, task_id, AgentRole.developer, "implement", "S", "U", Turn)
    assert reply == Turn.model_validate(TURN)
    assert gpt.calls == []
    first, second = claude.calls
    assert first["system"] == second["system"] == "S"
    assert first["messages"][0]["content"] == "U"
    retry = second["messages"][0]["content"]
    assert retry.startswith("U\n\nПредыдущий ответ отклонён: anthropic/claude-sonnet-5-5: answer does not match schema")
    assert "extra" in retry and retry.endswith("Верни ответ заново строго по схеме.")
    task = store.get_task(task_id)
    assert task.usage.model_calls == 2 and task.usage.output_tokens == 60
    assert raw_cost(store, task_id) == pytest.approx(2 * SONNET_COST)


def test_retry_note_is_clipped(setup):
    store, task_id, claude, _, make = setup
    finding = {"blocking": True, "criterion": None, "file": None, "line": None, "text": "т" * 900}
    review = {"verdict": "changes_requested", "findings": [finding] * 10}
    answer_in_turn(claude, [(json.dumps(review), "end_turn"), (json.dumps({"verdict": "pass", "findings": []}), "end_turn")])
    assert make().structured(store, task_id, AgentRole.critic, "review", "S", "U", CriticReview).verdict == "pass"
    retry = claude.calls[1]["messages"][0]["content"]
    detail = retry.removeprefix("U\n\nПредыдущий ответ отклонён: ").removesuffix(". Верни ответ заново строго по схеме.")
    assert 0 < len(detail) <= 1000 and "criterion or file" in detail


def test_valid_second_answer_is_accepted_after_truncation(setup):
    store, task_id, claude, _, make = setup
    answer_in_turn(claude, [('{"actions": [', "max_tokens"), (json.dumps(TURN), "end_turn")])
    assert make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn) == Turn.model_validate(TURN)
    assert claude.calls[1]["messages"][0]["content"] == (
        "U\n\nПредыдущий ответ обрезан пределом выходных токенов: "
        "сократи ответ, разбей действия на несколько ходов."
    )


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_answer_cut_by_output_limit_is_truncated_error(setup, provider):
    store, task_id, claude, gpt, make = setup
    if provider == "anthropic":
        claude.answer = '{"actions": ['
        claude.stop_reason = "max_tokens"
    else:
        broken_claude(claude)
        gpt.answer = '{"actions": ['
        gpt.status = "incomplete"
        gpt.incomplete_details = SimpleNamespace(reason="max_output_tokens")
    with pytest.raises(TruncatedModelResponse, match="output tokens"):
        make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
    assert len(claude.calls if provider == "anthropic" else gpt.calls) == 2
    assert raw_cost(store, task_id) == pytest.approx(2 * (SONNET_COST if provider == "anthropic" else SOL_COST))

    if provider == "openai":
        gpt.incomplete_details = SimpleNamespace(reason="content_filter")
        with pytest.raises(InvalidModelResponse) as error:
            make().structured(store, task_id, AgentRole.qa, "qa", "S", "U", Turn)
        assert not isinstance(error.value, TruncatedModelResponse)


def test_input_budget_and_timeouts(setup):
    make = setup[-1]
    gateway = make()
    assert gateway.input_budget(AgentRole.developer) == int(922000 * 0.9)
    assert gateway.input_budget(AgentRole.supervisor) == int(922000 * 0.9)
    assert gateway.timeout("anthropic") == 300
    assert make(model_timeout_seconds=42.0).timeout("openai") == 42.0


def test_provider_schema_is_inline_and_keeps_title_property():
    schema = provider_schema(ResearchNotes)
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    source = schema["properties"]["sources"]["items"]
    assert set(source["properties"]) == {"title", "url"}
    assert source["required"] == ["title", "url"] and source["additionalProperties"] is False
    assert "maxLength" not in json.dumps(schema)


@pytest.mark.parametrize("line", [0, -3])
def test_finding_line_below_one_means_unknown(setup, line):
    store, task_id, claude, _, make = setup
    finding = {"blocking": True, "criterion": None, "file": "web/src/App.tsx", "line": line, "text": "Нет кнопки."}
    claude.answer = json.dumps({"verdict": "changes_requested", "findings": [finding]})
    review = make().structured(store, task_id, AgentRole.critic, "review", "S", "U", CriticReview)
    assert review.findings[0].line is None
    qa = QaTurn.model_validate(
        {"summary": "s", "criteria_covered": [1], "findings": [finding], "done": True, "actions": []}
    )
    assert qa.findings[0].line is None
    assert QaTurn.model_validate(
        {"summary": "s", "criteria_covered": [1], "findings": [finding | {"line": 5}], "done": True, "actions": []}
    ).findings[0].line == 5
    for broken in ({"line": "x"}, {"line": 1.5}, {"criterion": 0}, {"file": None}):
        with pytest.raises(ValueError):
            CriticReview.model_validate({"verdict": "pass", "findings": [finding | broken]})


def test_prompts_keep_requirements_and_blocking_proportional():
    for prompt in (CRITIC, CRITIC_API):
        assert "verdict = pass, даже если есть неблокирующие замечания" in prompt
        assert "не требуй добавления тестов" in prompt
    for prompt in (CRITIC, CRITIC_API, QA, QA_API):
        assert "У блокирующего замечания должен быть заполнен criterion или file." in prompt
        assert "У замечания должен" not in prompt
    for prompt in (QA, QA_API):
        assert "blocking = true только когда критерий приёмки не выполнен" in prompt
    for prompt in (SUPERVISOR, SUPERVISOR_API):
        assert "только из того, что написал участник" in prompt
        assert "критериев от 1 до 3, а план — не длиннее 4 шагов" in prompt
        assert "не задают способ реализации" in prompt


def test_only_blocking_finding_requires_anchor():
    loose = {"blocking": False, "criterion": None, "file": None, "line": None, "text": "Можно короче."}
    stored = loose | {"id": "qa-0-1", "source": "qa"}
    assert CriticReview.model_validate({"verdict": "pass", "findings": [loose]}).findings[0].file is None
    assert QaTurn.model_validate(
        {"summary": "s", "criteria_covered": [1], "findings": [loose], "done": True, "actions": []}
    ).findings[0].criterion is None
    assert Finding.model_validate(stored).model_dump() == stored
    with pytest.raises(ValueError, match="criterion or file"):
        CriticReview.model_validate({"verdict": "pass", "findings": [loose | {"blocking": True}]})
    with pytest.raises(ValueError, match="criterion or file"):
        Finding.model_validate(stored | {"blocking": True})


@pytest.mark.parametrize("reply_type", [Turn, QaTurn])
def test_long_text_fields_come_last_in_turn_schema(reply_type):
    schema = provider_schema(reply_type)
    assert list(schema["properties"])[-1] == "actions" and schema["required"][-1] == "actions"
    action = schema["properties"]["actions"]["items"]
    assert list(action["properties"]) == ["kind", "path", "step", "content"]
    assert action["required"] == ["kind", "path", "step", "content"]


def test_long_descriptive_text_is_clipped_and_strict_fields_stay_strict(setup):
    store, task_id, claude, _, make = setup
    long = "Заголовок вкладки " * 100
    claude.answer = json.dumps(TURN | {"summary": long})
    reply = make().structured(store, task_id, AgentRole.developer, "implement", "S", "U", Turn)
    assert len(reply.summary) == 500 and reply.summary == long[: 500 - len(CLIPPED)] + CLIPPED
    assert reply.actions[0].path == "web/src/App.tsx"
    assert Turn.model_validate(TURN | {"summary": "x" * 500}).summary == "x" * 500

    finding = {"blocking": True, "criterion": 1, "file": None, "line": None, "text": "т" * 3000}
    qa = QaTurn.model_validate(
        {"summary": long, "criteria_covered": [1], "findings": [finding], "done": True, "actions": []}
    )
    assert len(qa.summary) == 500 and qa.summary.endswith(CLIPPED)
    assert len(qa.findings[0].text) == 1000 and qa.findings[0].text.endswith(CLIPPED)
    review = CriticReview.model_validate({"verdict": "changes_requested", "findings": [finding]})
    assert len(review.findings[0].text) == 1000
    notes = ResearchNotes.model_validate(
        {"notes": "н" * 5000, "sources": [{"title": "т" * 300, "url": "https://example.com"}]}
    )
    assert len(notes.notes) == 4000 and len(notes.sources[0].title) == 200

    for broken in (
        TURN | {"summary": None},
        TURN | {"actions": [{"kind": "read_file", "path": None, "content": None, "step": None}]},
        TURN | {"actions": [{"kind": "run_step", "path": None, "content": None, "step": "e2e"}]},
        TURN | {"actions": [TURN["actions"][0]] * 9},
    ):
        with pytest.raises(ValueError):
            Turn.model_validate(broken)
    with pytest.raises(ValueError):
        CriticReview.model_validate({"verdict": "pass", "findings": [finding | {"text": ""}]})
    with pytest.raises(ValueError):
        CriticReview.model_validate({"verdict": "pass", "findings": [finding | {"file": "a" * 401}]})
    with pytest.raises(ValueError):
        ResearchNotes.model_validate({"notes": "", "sources": [{"title": "t", "url": "h" * 2001}]})
    with pytest.raises(ValueError):
        SpecDraft.model_validate(SPEC | {"summary": "s" * 2001})


@pytest.mark.parametrize(("reply_type", "role"), [(Turn, AgentRole.developer), (QaTurn, AgentRole.qa)])
def test_action_step_is_enum_of_role_steps(reply_type, role):
    policy = CommandsPolicy(Settings().commands_policy)
    allowed = [name for name, step in policy.steps.items() if role in step.roles]
    if role == AgentRole.developer:
        allowed.append("api-test")
    step = provider_schema(reply_type)["properties"]["actions"]["items"]["properties"]["step"]
    assert step == {"anyOf": [{"enum": allowed, "type": "string"}, {"type": "null"}]}
    assert "const" not in json.dumps(provider_schema(reply_type))
    base = {"summary": "s", "done": True, "criteria_covered": [], "findings": []}
    fields = {name: base[name] for name in reply_type.model_fields if name in base}
    for name in allowed:
        action = {"kind": "run_step", "path": None, "step": name, "content": None}
        assert reply_type.model_validate(fields | {"actions": [action]}).actions[0].step == name
    for name in ("run_e2e_tests", "install", *{"lint", "test", "build", "e2e"} - set(allowed)):
        action = {"kind": "run_step", "path": None, "step": name, "content": None}
        with pytest.raises(ValueError):
            reply_type.model_validate(fields | {"actions": [action]})


def test_context_keeps_required_parts_and_last_turn():
    fixed = [Entry("Требования", "критерий " * 20, required=True), Entry("Файлы", "x" * 500)]
    turns = [
        [Entry("Ход 1", "старый результат " * 200)],
        [Entry("Ход 2", "средний результат " * 200)],
        [Entry("Ход 3", "последний результат " * 100)],
    ]
    budget = 6000
    text = assemble("SYSTEM", fixed, turns, budget)
    assert size("SYSTEM") + size(text) <= budget
    assert "критерий " * 20 in text
    assert "последний результат " * 100 in text
    assert "x" * 500 in text
    assert "[обрезано: Ход 2," in text and "Ход 1" not in text
    assert text.index("Ход 2") < text.index("Ход 3")

    small = assemble("SYSTEM", fixed[:1] + [Entry("Файлы", "x" * 5000)], turns, budget)
    assert "последний результат " * 100 in small
    assert "[обрезано: Файлы, 5000 байтов]" in small and "Ход 2" not in small


def test_context_truncates_large_file_of_last_turn():
    turns = [[Entry("Результат действия 1: read_file web/src/App.tsx", "ок\n" + "строка\n" * 10000)]]
    text = assemble("S", [Entry("Требования", "r", required=True)], turns, 3000)
    assert "[обрезано: Результат действия 1: read_file web/src/App.tsx" in text
    assert size("S") + size(text) <= 3000


def test_context_keeps_every_result_of_last_turn_when_first_is_huge():
    turns = [[
        Entry("Результат действия 1: read_file web/src/Big.tsx", "ok\n" + "x" * 100_000),
        Entry("Результат действия 2: read_file web/src/App.tsx", "ok\nexport function App() {}"),
        Entry("Результат действия 3: write_file web/src/App.tsx", "denied\nЗапись запрещена."),
    ]]
    text = assemble("S", [Entry("Требования", "r", required=True)], turns, 4000)
    assert size("S") + size(text) <= 4000
    assert "[обрезано: Результат действия 1: read_file web/src/Big.tsx, 100003 байтов]" in text
    assert "Результат действия 2" in text and "Результат действия 3" in text
    with pytest.raises(ContextTooLarge):
        assemble("S", [Entry("Требования", "r", required=True)], turns, 400)


def test_context_without_room_for_required_part_fails():
    with pytest.raises(ContextTooLarge):
        assemble("S" * 900, [Entry("Требования", "к" * 200, required=True)], [], 1000)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_dense_text_fits_byte_budget(seed):
    generator = random.Random(seed)
    alphabet = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя😀漢字🙂" + "".join(chr(code) for code in range(33, 127))
    dense = "".join(generator.choice(alphabet) for _ in range(20000))
    budget = int((32768 - 4096) * 0.9)
    text = assemble("SYSTEM", [Entry("Задача", "t", required=True)], [[Entry("Файл", dense)]], budget)
    assert size("SYSTEM") + size(text) <= budget
    assert "[обрезано: Файл," in text
