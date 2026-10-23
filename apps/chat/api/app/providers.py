import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic
import openai

from app.config import Settings
from app.policy import CLASSIFIER_SCHEMA, MODERATION_MODEL
from app.retry import InvalidResponse, StreamInterrupted
from app.routing import IMAGE_MODEL, Target
from app.schemas import Complexity, Intent, Risk, Schema

OnDelta = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class TextResult:
    usage: Usage | None
    truncated: bool


class Classification(Schema):
    intent: Intent
    complexity: Complexity
    risk: Risk


def parse_classification(text: str) -> Classification:
    try:
        return Classification.model_validate_json(text)
    except ValueError as error:
        raise InvalidResponse("classifier returned invalid JSON") from error


class Providers:
    def __init__(self, settings: Settings) -> None:
        self.anthropic = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key or "missing",
            base_url=settings.anthropic_base_url,
            max_retries=0,
        )
        self.openai = openai.AsyncOpenAI(
            api_key=settings.openai_api_key or "missing",
            base_url=settings.openai_base_url,
            max_retries=0,
        )

    async def moderate(self, text: str) -> dict[str, float]:
        response = await self.openai.moderations.create(model=MODERATION_MODEL, input=text)
        return response.results[0].category_scores.model_dump(by_alias=True)

    async def classify(self, model: str, system: str, data: str, max_tokens: int) -> tuple[Classification, Usage | None]:
        if model.startswith("claude-"):
            response = await self.anthropic.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": data}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": CLASSIFIER_SCHEMA}},
            )
            text = "".join(block.text for block in response.content if block.type == "text")
            usage = Usage(response.usage.input_tokens, response.usage.output_tokens)
            return parse_classification(text), usage
        response = await self.openai.responses.create(
            model=model,
            instructions=system,
            input=data,
            reasoning={"effort": "low"},
            max_output_tokens=max_tokens,
            text={"format": {"type": "json_schema", "name": "classification", "schema": CLASSIFIER_SCHEMA, "strict": True}},
        )
        usage = Usage(response.usage.input_tokens, response.usage.output_tokens) if response.usage else None
        return parse_classification(response.output_text), usage

    async def stream_text(self, target: Target, system: str, messages: list[dict], on_delta: OnDelta) -> TextResult:
        if target.provider == "anthropic":
            async with self.anthropic.messages.stream(
                model=target.model,
                max_tokens=target.max_output_tokens,
                system=system,
                messages=messages,
                output_config={"effort": target.effort},
            ) as stream:
                stopped = False
                try:
                    async for event in stream:
                        if event.type == "content_block_delta" and event.delta.type == "text_delta":
                            await on_delta(event.delta.text)
                        elif event.type == "message_stop":
                            stopped = True
                    if not stopped:
                        raise StreamInterrupted("stream ended without message_stop")
                    final = await stream.get_final_message()
                except (anthropic.APIError, ValueError) as error:
                    raise StreamInterrupted(type(error).__name__) from error
            usage = Usage(final.usage.input_tokens, final.usage.output_tokens)
            return TextResult(usage, final.stop_reason == "max_tokens")

        stream = await self.openai.responses.create(
            model=target.model,
            instructions=system,
            input=messages,
            reasoning={"effort": target.effort},
            max_output_tokens=target.max_output_tokens,
            stream=True,
        )
        try:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    await on_delta(event.delta)
                elif event.type in ("response.completed", "response.incomplete"):
                    usage = event.response.usage
                    return TextResult(
                        Usage(usage.input_tokens, usage.output_tokens) if usage else None,
                        event.type == "response.incomplete",
                    )
                elif event.type in ("response.failed", "error"):
                    raise StreamInterrupted(event.type)
        except (openai.APIError, ValueError) as error:
            raise StreamInterrupted(type(error).__name__) from error
        raise StreamInterrupted("stream ended without completion")

    async def generate_image(self, prompt: str, quality: str) -> tuple[bytes, Usage | None]:
        response = await self.openai.images.generate(
            model=IMAGE_MODEL,
            prompt=prompt,
            size="1024x1024",
            quality=quality,
            output_format="png",
        )
        usage = Usage(response.usage.input_tokens, response.usage.output_tokens) if response.usage else None
        return base64.b64decode(response.data[0].b64_json), usage
