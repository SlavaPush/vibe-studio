import json

import httpx
import pytest
from conftest import FakeAnthropic, FakeOpenAI

from app.config import Settings
from app.models_gateway import ModelsGateway
from app.roles import CriticReview, QaTurn, SpecDraft, Turn, provider_schema
from scripts.measure_local import COMPONENT_PATH, STEPS, Sample, main, run, summarize

ANSWERS = {
    json.dumps(provider_schema(SpecDraft), sort_keys=True): {
        "summary": "Счётчик символов",
        "acceptance_criteria": ["Счётчик виден"],
        "needs_research": False,
        "research_questions": [],
    },
    json.dumps(provider_schema(Turn), sort_keys=True): {"summary": "Готово", "done": True, "actions": []},
    json.dumps(provider_schema(QaTurn), sort_keys=True): {
        "summary": "Тест",
        "criteria_covered": [1],
        "findings": [],
        "done": True,
        "actions": [],
    },
    json.dumps(provider_schema(CriticReview), sort_keys=True): {"verdict": "pass", "findings": []},
}


class Ollama:
    def __init__(self):
        self.loaded = [{"name": "qwen3:8b"}]
        self.chats = []
        self.broken = {}

    def __call__(self, request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": self.loaded})
        body = json.loads(request.content)
        self.chats.append(body)
        key = json.dumps(body["format"], sort_keys=True)
        if self.broken.get(key) == "status":
            return httpx.Response(500, json={"error": "boom"})
        content = "not json" if self.broken.get(key) == "schema" else json.dumps(ANSWERS[key])
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": content},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 1000 + len(self.chats),
                "eval_count": 100,
                "eval_duration": 4_000_000_000,
                "prompt_eval_duration": 1_000_000_000,
                "load_duration": 50_000_000,
            },
        )


@pytest.fixture
def bench():
    ollama = Ollama()
    claude = FakeAnthropic()
    gpt = FakeOpenAI()
    client = httpx.Client(transport=httpx.MockTransport(ollama))
    gateway = ModelsGateway(Settings(), anthropic_client=claude, openai_client=gpt, http_client=client)
    yield gateway, ollama, claude, gpt
    client.close()


def lines(output):
    return [line.split("\t") for line in output.splitlines() if line]


def test_all_steps_go_to_ollama_with_role_context(bench, capsys):
    gateway, ollama, claude, gpt = bench
    assert run(gateway, STEPS, 2) == 0
    assert claude.calls == [] and gpt.calls == []
    assert len(ollama.chats) == 8
    thinking = {step.name: body["think"] for step, body in zip([s for s in STEPS for _ in range(2)], ollama.chats)}
    assert thinking == {
        "supervisor/specify": False,
        "developer/implement": True,
        "qa/qa": False,
        "critic/review": True,
    }
    developer = ollama.chats[2]
    assert developer["options"] == {"num_predict": 4096, "num_ctx": 32768}
    assert developer["format"] == provider_schema(Turn)
    assert f"read_file {COMPONENT_PATH}" in developer["messages"][1]["content"]
    assert "export function MessageComposer" in developer["messages"][1]["content"]
    assert "diff --git" in ollama.chats[6]["messages"][1]["content"]
    assert "apps/chat/web/e2e/office/tsk_measure.pw.ts" in ollama.chats[4]["messages"][1]["content"]
    assert "Файлы apps/chat/web/src" in ollama.chats[4]["messages"][1]["content"]

    output = capsys.readouterr().out
    assert output.splitlines()[0] == "загруженные модели: qwen3:8b"
    rows = lines(output)
    calls = [row for row in rows if len(row) == 9 and row[1] in ("1", "2")]
    assert len(calls) == 8
    assert calls[0] == ["supervisor/specify", "1", calls[0][2], "1001", "100", "25.0", "0.1", "stop", "ok"]
    summary = {row[0]: row for row in rows if len(row) == 8 and row[1] == "2/2"}
    assert list(summary) == [step.name for step in STEPS]
    assert summary["qa/qa"][5:] == ["1005", "100", "25.0"]


def test_invalid_answer_and_failed_call_do_not_stop_other_steps(bench, capsys):
    gateway, ollama, _, _ = bench
    ollama.broken[json.dumps(provider_schema(QaTurn), sort_keys=True)] = "schema"
    ollama.broken[json.dumps(provider_schema(SpecDraft), sort_keys=True)] = "status"
    assert run(gateway, STEPS, 1) == 1
    assert len(ollama.chats) == 4
    rows = {row[0]: row for row in lines(capsys.readouterr().out) if len(row) == 9 and row[1] == "1"}
    assert rows["supervisor/specify"][8] == "нет (ollama status 500: boom)"
    assert rows["qa/qa"][8] == "нет (schema: JSONDecodeError)"
    assert rows["developer/implement"][8] == rows["critic/review"][8] == "ok"


def test_more_than_one_loaded_model_is_reported(bench, capsys):
    gateway, ollama, _, _ = bench
    ollama.loaded = [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}]
    assert run(gateway, STEPS[:1], 1) == 0
    output = capsys.readouterr().out
    assert "загруженные модели: qwen3:8b, llama3.1:8b" in output
    assert "предупреждение: загружено моделей 2" in output


def test_cloud_route_in_local_profile_is_refused(bench, capsys):
    gateway, ollama, claude, gpt = bench
    gateway.models["profiles"]["local"]["critic"] = {
        "provider": "anthropic", "model": "claude-sonnet-5-5", "effort": "high", "max_output_tokens": 16000
    }
    assert run(gateway, STEPS, 1) == 2
    assert ollama.chats == [] and claude.calls == [] and gpt.calls == []
    assert "critic/review" in capsys.readouterr().err


def test_summary_uses_median_min_max():
    samples = [
        Sample("qa/qa", 1, 30.0, 900, 120, 20.0, 5.0, "stop", True),
        Sample("qa/qa", 2, 10.0, 1000, 100, 25.0, 0.1, "stop", True),
        Sample("qa/qa", 3, 12.0, 1100, 80, 30.0, 0.1, "length", False, "incomplete answer"),
        Sample("critic/review", 1, 50.0, 0, 0, None, None, None, False, "ollama failed"),
    ]
    assert summarize(samples) == [
        {
            "step": "qa/qa",
            "runs": 3,
            "valid": 2,
            "median_seconds": 12.0,
            "min_seconds": 10.0,
            "max_seconds": 30.0,
            "median_input_tokens": 1000,
            "median_output_tokens": 100,
            "median_tokens_per_second": 25.0,
        },
        {
            "step": "critic/review",
            "runs": 1,
            "valid": 0,
            "median_seconds": 50.0,
            "min_seconds": 50.0,
            "max_seconds": 50.0,
            "median_input_tokens": 0,
            "median_output_tokens": 0,
            "median_tokens_per_second": None,
        },
    ]


def test_runs_must_be_positive():
    with pytest.raises(SystemExit):
        main(["--runs", "0"])
