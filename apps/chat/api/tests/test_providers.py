import asyncio
import base64
import json

import anthropic
import httpx2
import openai
import pytest

from app import config, retry
from app.config import Settings
from app.policy import CLASSIFIER_PROMPT
from app.providers import Providers
from app.routing import Target


def sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events).encode()


ANTHROPIC_STREAM = [
    ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "При"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "вет"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
    ("message_stop", {"type": "message_stop"}),
]
USAGE = {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
OPENAI_STREAM = [
    ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, "delta": "Хо", "sequence_number": 1, "logprobs": []}),
    ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, "delta": "рошо", "sequence_number": 2, "logprobs": []}),
    ("response.completed", {"type": "response.completed", "sequence_number": 3, "response": {"id": "resp_1", "object": "response", "created_at": 1, "status": "completed", "model": "gpt-6-luna", "output": [], "usage": USAGE}}),
]


class Recorder:
    def __init__(self, handler) -> None:
        self.requests: list[httpx2.Request] = []
        self.handler = handler

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.handler(request)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def providers_with(handler) -> tuple[Providers, Recorder]:
    recorder = Recorder(handler)
    providers = Providers(Settings(anthropic_api_key="test", openai_api_key="test"))
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(recorder))
    providers.anthropic = anthropic.AsyncAnthropic(
        api_key="test", base_url="https://anthropic.test", max_retries=0, http_client=client
    )
    providers.openai = openai.AsyncOpenAI(api_key="test", base_url="https://openai.test/v1", max_retries=0, http_client=client)
    return providers, recorder


def stream_response(events):
    return lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(events))


def collect(providers: Providers, target: Target):
    chunks: list[str] = []

    async def on_delta(text: str) -> None:
        chunks.append(text)

    result = asyncio.run(providers.stream_text(target, "system", [{"role": "user", "content": "Привет"}], on_delta))
    return chunks, result


def test_clients_use_explicit_base_url_and_no_sdk_retries(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://shell.example")
    providers = Providers(Settings(anthropic_api_key="test", openai_api_key="test"))
    assert str(providers.anthropic.base_url).startswith("https://api.anthropic.com")
    assert providers.anthropic.max_retries == 0
    assert providers.openai.max_retries == 0


def test_anthropic_base_url_is_read_only_from_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://shell.example")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    assert config.load_settings().anthropic_base_url == "https://api.anthropic.com"
    (tmp_path / ".env").write_text("ANTHROPIC_BASE_URL=https://proxy.test\n", encoding="utf-8")
    assert config.load_settings().anthropic_base_url == "https://proxy.test"


def test_anthropic_stream_sends_effort_and_returns_usage():
    providers, recorder = providers_with(stream_response(ANTHROPIC_STREAM))
    chunks, result = collect(providers, Target("claude-sonnet-5-5", "high"))
    assert chunks == ["При", "вет"]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (12, 5)
    assert result.truncated is False
    body = recorder.body()
    assert body["output_config"] == {"effort": "high"}
    assert body["max_tokens"] == 16000
    assert body["model"] == "claude-sonnet-5-5"
    assert body["stream"] is True


@pytest.mark.parametrize("cut", [1, 3, 6])
def test_anthropic_stream_without_message_stop_is_unknown(cut):
    providers, _ = providers_with(stream_response(ANTHROPIC_STREAM[:cut]))
    with pytest.raises(retry.StreamInterrupted) as error:
        collect(providers, Target("claude-haiku-5-5", "low"))
    assert retry.classify(error.value) == retry.UNKNOWN


def test_anthropic_max_tokens_is_truncated():
    events = [*ANTHROPIC_STREAM]
    events[5] = ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None}, "usage": {"output_tokens": 2048}})
    providers, _ = providers_with(stream_response(events))
    chunks, result = collect(providers, Target("claude-haiku-5-5", "low"))
    assert chunks == ["При", "вет"]
    assert result.truncated is True
    assert result.usage.output_tokens == 2048


def test_anthropic_stream_error_after_start_is_unknown():
    broken = ANTHROPIC_STREAM[:3] + [("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})]
    providers, _ = providers_with(stream_response(broken))
    with pytest.raises(retry.StreamInterrupted) as error:
        collect(providers, Target("claude-haiku-5-5", "low"))
    assert retry.classify(error.value) == retry.UNKNOWN


def test_anthropic_status_before_stream_keeps_its_class():
    providers, _ = providers_with(lambda request: httpx2.Response(529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}))
    with pytest.raises(anthropic.APIStatusError) as error:
        collect(providers, Target("claude-haiku-5-5", "low"))
    assert retry.classify(error.value) == retry.REFUSED


def test_openai_stream_sends_reasoning_effort_and_returns_usage():
    providers, recorder = providers_with(stream_response(OPENAI_STREAM))
    chunks, result = collect(providers, Target("gpt-6-luna", "medium"))
    assert chunks == ["Хо", "рошо"]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (10, 3)
    assert result.truncated is False
    body = recorder.body()
    assert body["reasoning"] == {"effort": "medium"}
    assert body["max_output_tokens"] == 8192
    assert body["instructions"] == "system"


def test_openai_stream_without_completion_is_unknown():
    providers, _ = providers_with(stream_response(OPENAI_STREAM[:1]))
    with pytest.raises(retry.StreamInterrupted):
        collect(providers, Target("gpt-6.1-sol", "high"))


@pytest.mark.parametrize("deltas", [0, 1])
def test_openai_incomplete_response_is_truncated(deltas):
    incomplete = {**OPENAI_STREAM[2][1], "type": "response.incomplete"}
    incomplete["response"] = {**incomplete["response"], "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    providers, _ = providers_with(stream_response([*OPENAI_STREAM[:deltas], ("response.incomplete", incomplete)]))
    chunks, result = collect(providers, Target("gpt-6-luna", "low"))
    assert len(chunks) == deltas
    assert result.truncated is True
    assert result.usage.output_tokens == 3


def test_classifier_uses_structured_output_for_both_providers():
    labels = {"intent": "code", "complexity": "standard", "risk": "none"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "anthropic.test":
            return httpx2.Response(200, json={"id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [{"type": "text", "text": json.dumps(labels)}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 50, "output_tokens": 9}})
        return httpx2.Response(200, json={"id": "resp", "object": "response", "created_at": 1, "status": "completed", "model": "gpt-6-luna", "output": [{"type": "message", "id": "m", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": json.dumps(labels), "annotations": []}]}], "usage": USAGE})

    providers, recorder = providers_with(handler)
    for model in ("claude-haiku-5-5", "gpt-6-luna"):
        result, usage = asyncio.run(providers.classify(model, CLASSIFIER_PROMPT, "{}", 2048))
        assert result.model_dump() == labels
        assert usage is not None
    anthropic_body, openai_body = recorder.body(0), recorder.body(1)
    assert anthropic_body["output_config"]["effort"] == "low"
    assert anthropic_body["output_config"]["format"]["type"] == "json_schema"
    assert openai_body["text"]["format"]["strict"] is True
    assert openai_body["reasoning"] == {"effort": "low"}


def test_classifier_invalid_json_is_invalid_response():
    providers, _ = providers_with(lambda request: httpx2.Response(200, json={"id": "msg", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [{"type": "text", "text": "{\"intent\": \"poem\"}"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}))
    with pytest.raises(retry.InvalidResponse):
        asyncio.run(providers.classify("claude-haiku-5-5", CLASSIFIER_PROMPT, "{}", 2048))


def test_moderation_and_image_parsing():
    image = base64.b64encode(b"png-bytes").decode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/moderations"):
            return httpx2.Response(200, json={"id": "modr", "model": "omni-moderation-latest", "results": [{"flagged": True, "categories": {"violence": True}, "category_scores": {"violence": 0.91, "sexual/minors": 0.01}, "category_applied_input_types": {}}]})
        return httpx2.Response(200, json={"created": 1, "data": [{"b64_json": image}], "usage": {"input_tokens": 7, "output_tokens": 900, "total_tokens": 907, "input_tokens_details": {"image_tokens": 0, "text_tokens": 7}}})

    providers, recorder = providers_with(handler)
    scores = asyncio.run(providers.moderate("текст"))
    assert scores["violence"] == 0.91
    assert scores["sexual/minors"] == 0.01
    data, usage = asyncio.run(providers.generate_image("кот", "low"))
    assert data == b"png-bytes"
    assert usage.input_tokens == 7
    body = recorder.body()
    assert (body["size"], body["quality"], body["output_format"]) == ("1024x1024", "low", "png")
