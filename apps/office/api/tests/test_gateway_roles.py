import json
import random
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from conftest import FakeAnthropic, FakeOpenAI

from app.models_gateway import InvalidModelResponse, ModelCall, ModelsGateway, ProviderError, TruncatedModelResponse
from app.roles import (
    CLIPPED,
    ContextTooLarge,
    CriticReview,
    Entry,
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
from app.schemas import AgentRole, ModelProfile
from app.tasks import BudgetExceeded

TURN = {
    "actions": [{"kind": "read_file", "path": "web/src/App.tsx", "content": None, "step": None}],
    "done": False,
    "summary": "Читаю файл",
}
SPEC = {"summary": "s", "acceptance_criteria": ["c"], "needs_research": False, "research_questions": []}


class Ollama:
    def __init__(self, answer=None, delay=0.0, error=None):
        self.answer = answer or {
            "message": {"role": "assistant", "content": json.dumps(SPEC)},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 70,
            "eval_count": 9,
        }
        self.delay = delay
        self.error = error
        self.response = None
        self.requests = []
        self.running = 0
        self.max_running = 0
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.requests.append(request)
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        try:
            time.sleep(self.delay)
            if self.error is not None:
                raise self.error
            if self.response is not None:
                return self.response
            return httpx.Response(200, json=self.answer)
        finally:
            with self.lock:
                self.running -= 1


@pytest.fixture
def setup(office):
    _, me = office.login()
    task_id = office.queued_task(me["id"])
    claude = FakeAnthropic()
    gpt = FakeOpenAI()
    ollama = Ollama()

    def make(**overrides):
        settings = replace(office.settings, **overrides)
        client = httpx.Client(transport=httpx.MockTransport(ollama))
        return ModelsGateway(settings, anthropic_client=claude, openai_client=gpt, http_client=client)

    return office.store, task_id, claude, gpt, ollama, make


def model_events(store, task_id):
    return [
        event.payload["arguments"]
        for event in store.events_after(task_id, 0, 1000)
        if event.type == "tool_called" and event.tool == "model"
    ]


def test_anthropic_call_uses_effort_schema_and_accounting(setup):
    store, task_id, claude, _, _, make = setup
    claude.answer = json.dumps(TURN)
    reply = make().structured(
        store, task_id, ModelProfile.cloud, AgentRole.developer, "implement", "SYSTEM", "USER", Turn
    )
    assert reply.actions[0].path == "web/src/App.tsx"
    [call] = claude.calls
    assert call["model"] == "claude-sonnet-5-5"
    assert call["max_tokens"] == 32000
    assert call["system"] == "SYSTEM" and call["messages"] == [{"role": "user", "content": "USER"}]
    assert call["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": provider_schema(Turn)}}
    assert call["timeout"] == 300
    task = store.get_task(task_id)
    assert (task.usage.model_calls, task.usage.input_tokens, task.usage.output_tokens) == (1, 120, 30)
    assert model_events(store, task_id) == ["developer/implement anthropic/claude-sonnet-5-5"]


def test_fallback_is_counted_as_separate_call(setup):
    store, task_id, claude, gpt, _, make = setup
    claude.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    gpt.answer = json.dumps(TURN)
    make().structured(store, task_id, ModelProfile.hybrid, AgentRole.critic, "review", "S", "U", Turn)
    [call] = gpt.calls
    assert call["model"] == "gpt-6.1-sol"
    assert call["reasoning"] == {"effort": "high"}
    assert call["max_output_tokens"] == 16000
    assert call["text"]["format"]["strict"] is True
    assert call["text"]["format"]["schema"] == provider_schema(Turn)
    assert store.get_task(task_id).usage.model_calls == 2
    assert model_events(store, task_id) == [
        "critic/review anthropic/claude-sonnet-5-5",
        "critic/review openai/gpt-6.1-sol",
    ]


def test_both_providers_failing_is_provider_error(setup):
    store, task_id, claude, gpt, _, make = setup
    claude.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    gpt.error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    with pytest.raises(ProviderError):
        make().structured(store, task_id, ModelProfile.cloud, AgentRole.qa, "qa", "S", "U", Turn)
    assert store.get_task(task_id).usage.model_calls == 2


def test_ollama_request_and_no_fallback(setup):
    store, task_id, _, gpt, ollama, make = setup
    gateway = make(ollama_url="http://127.0.0.1:11999/")
    reply = gateway.structured(store, task_id, ModelProfile.hybrid, AgentRole.supervisor, "spec", "S", "U", SpecDraft)
    assert reply.summary == "s"
    [request] = ollama.requests
    assert str(request.url) == "http://127.0.0.1:11999/api/chat"
    body = json.loads(request.content)
    assert body["model"] == "qwen3:8b"
    assert body["format"] == provider_schema(SpecDraft)
    assert body["think"] is False and body["stream"] is False
    assert body["options"] == {"num_predict": 4096, "num_ctx": 32768}
    assert body["messages"] == [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    task = store.get_task(task_id)
    assert (task.usage.input_tokens, task.usage.output_tokens) == (70, 9)

    ollama.error = httpx.ConnectError("refused")
    with pytest.raises(ProviderError):
        gateway.structured(store, task_id, ModelProfile.local, AgentRole.developer, "implement", "S", "U", Turn)
    assert json.loads(ollama.requests[-1].content)["think"] is True
    assert gpt.calls == []
    assert store.get_task(task_id).usage.model_calls == 2


def test_ollama_error_status_carries_short_error_field(setup):
    store, task_id, _, _, ollama, make = setup
    gateway = make()
    reason = "model requires more system memory\n(10.2 GiB) than is available " + "x" * 1000
    ollama.response = httpx.Response(500, json={"error": reason, "detail": "SYSTEM PROMPT"})
    with pytest.raises(ProviderError) as error:
        gateway.structured(store, task_id, ModelProfile.local, AgentRole.qa, "qa", "SYSTEM PROMPT", "USER", QaTurn)
    message = str(error.value)
    assert message.startswith("ollama status 500: model requires more system memory (10.2 GiB) than is available xxx")
    assert len(message) == len("ollama status 500: ") + 300
    assert "SYSTEM PROMPT" not in message and "USER" not in message

    for response in (httpx.Response(502, text="<html>bad gateway</html>"), httpx.Response(500, json={"error": " "})):
        ollama.response = response
        with pytest.raises(ProviderError) as error:
            gateway.structured(store, task_id, ModelProfile.local, AgentRole.qa, "qa", "S", "U", QaTurn)
        assert str(error.value) == f"ollama status {response.status_code}"


def test_ollama_reply_carries_metrics_and_cloud_reply_does_not(setup):
    _, _, claude, _, ollama, make = setup
    gateway = make()

    def call(profile):
        target = gateway.targets(profile, AgentRole.supervisor)[0]
        return gateway.call_provider(
            ModelCall("t", AgentRole.supervisor, "spec", target, "S", "U", provider_schema(SpecDraft), 5.0)
        )

    ollama.answer |= {"eval_duration": 3_000_000_000, "prompt_eval_duration": 500_000_000, "load_duration": 70_000_000}
    reply = call(ModelProfile.local)
    assert (reply.done_reason, reply.eval_duration_ns, reply.prompt_eval_duration_ns, reply.load_duration_ns) == (
        "stop", 3_000_000_000, 500_000_000, 70_000_000
    )
    assert reply.complete and (reply.input_tokens, reply.output_tokens) == (70, 9)

    ollama.answer = {"message": {"content": "{}"}, "done": True, "done_reason": "stop", "eval_duration": "3"}
    reply = call(ModelProfile.local)
    assert (reply.done_reason, reply.eval_duration_ns, reply.prompt_eval_duration_ns, reply.load_duration_ns) == (
        "stop", None, None, None
    )

    claude.answer = json.dumps(SPEC)
    reply = call(ModelProfile.cloud)
    assert (reply.done_reason, reply.eval_duration_ns, reply.load_duration_ns) == (None, None, None)


def test_ollama_calls_are_serialized(setup):
    store, task_id, _, _, ollama, make = setup
    ollama.delay = 0.2
    gateway = make()
    threads = [
        threading.Thread(
            target=gateway.structured,
            args=(store, task_id, ModelProfile.local, AgentRole.supervisor, "spec", "S", "U", SpecDraft),
        )
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(ollama.requests) == 3
    assert ollama.max_running == 1


def test_budget_is_checked_before_each_call(setup, office):
    store, task_id, claude, gpt, _, make = setup
    office.set_task(task_id, model_calls=30)
    with pytest.raises(BudgetExceeded):
        make().structured(store, task_id, ModelProfile.cloud, AgentRole.qa, "qa", "S", "U", Turn)
    assert claude.calls == []

    office.set_task(task_id, model_calls=29)
    claude.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(BudgetExceeded):
        make().structured(store, task_id, ModelProfile.cloud, AgentRole.qa, "qa", "S", "U", Turn)
    assert len(claude.calls) == 1 and gpt.calls == []
    assert store.get_task(task_id).usage.model_calls == 30


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
    store, task_id, claude, _, _, make = setup
    claude.answer = answer
    with pytest.raises(InvalidModelResponse):
        make().structured(store, task_id, ModelProfile.cloud, AgentRole.developer, "implement", "S", "U", Turn)
    assert store.get_task(task_id).usage.output_tokens == 30


def test_incomplete_anthropic_answer_is_rejected(setup):
    store, task_id, claude, _, _, make = setup
    claude.answer = json.dumps(TURN)
    claude.stop_reason = "max_tokens"
    with pytest.raises(InvalidModelResponse):
        make().structured(store, task_id, ModelProfile.cloud, AgentRole.developer, "implement", "S", "U", Turn)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "ollama"])
def test_answer_cut_by_output_limit_is_truncated_error(setup, provider):
    store, task_id, claude, gpt, ollama, make = setup
    profile = ModelProfile.local if provider == "ollama" else ModelProfile.cloud
    if provider == "anthropic":
        claude.answer = '{"actions": ['
        claude.stop_reason = "max_tokens"
    elif provider == "openai":
        claude.error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
        gpt.answer = '{"actions": ['
        gpt.status = "incomplete"
        gpt.incomplete_details = SimpleNamespace(reason="max_output_tokens")
    else:
        ollama.answer = {
            "message": {"role": "assistant", "content": '{"actions": ['},
            "done": True,
            "done_reason": "length",
            "prompt_eval_count": 70,
            "eval_count": 4096,
        }
    with pytest.raises(TruncatedModelResponse, match="output tokens"):
        make().structured(store, task_id, profile, AgentRole.qa, "qa", "S", "U", Turn)

    if provider == "openai":
        gpt.incomplete_details = SimpleNamespace(reason="content_filter")
        with pytest.raises(InvalidModelResponse) as error:
            make().structured(store, task_id, profile, AgentRole.qa, "qa", "S", "U", Turn)
        assert not isinstance(error.value, TruncatedModelResponse)


def test_input_budget_and_timeouts(setup):
    _, _, _, _, _, make = setup
    gateway = make()
    assert gateway.input_budget(ModelProfile.local, AgentRole.developer) == int((32768 - 4096) * 0.9)
    assert gateway.input_budget(ModelProfile.cloud, AgentRole.developer) == int(922000 * 0.9)
    assert gateway.input_budget(ModelProfile.cloud, AgentRole.supervisor) == int(922000 * 0.9)
    assert (gateway.timeout("anthropic"), gateway.timeout("ollama")) == (300, 600)
    assert make(model_timeout_seconds=42.0).timeout("ollama") == 42.0


def test_provider_schema_is_inline_and_keeps_title_property():
    schema = provider_schema(ResearchNotes)
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema
    source = schema["properties"]["sources"]["items"]
    assert set(source["properties"]) == {"title", "url"}
    assert source["required"] == ["title", "url"] and source["additionalProperties"] is False
    assert "maxLength" not in json.dumps(schema)


@pytest.mark.parametrize("line", [0, -3])
def test_finding_line_below_one_means_unknown(setup, line):
    store, task_id, claude, _, _, make = setup
    finding = {"blocking": True, "criterion": None, "file": "web/src/App.tsx", "line": line, "text": "Нет кнопки."}
    claude.answer = json.dumps({"verdict": "changes_requested", "findings": [finding]})
    review = make().structured(store, task_id, ModelProfile.cloud, AgentRole.critic, "review", "S", "U", CriticReview)
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


@pytest.mark.parametrize("reply_type", [Turn, QaTurn])
def test_long_text_fields_come_last_in_turn_schema(reply_type):
    schema = provider_schema(reply_type)
    assert list(schema["properties"])[-1] == "actions" and schema["required"][-1] == "actions"
    action = schema["properties"]["actions"]["items"]
    assert list(action["properties"]) == ["kind", "path", "step", "content"]
    assert action["required"] == ["kind", "path", "step", "content"]


def test_long_descriptive_text_is_clipped_and_strict_fields_stay_strict(setup):
    store, task_id, claude, _, _, make = setup
    long = "Заголовок вкладки " * 100
    claude.answer = json.dumps(TURN | {"summary": long})
    reply = make().structured(
        store, task_id, ModelProfile.cloud, AgentRole.developer, "implement", "S", "U", Turn
    )
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
