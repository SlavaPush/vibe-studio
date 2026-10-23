import json
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings

MODERATION_MODEL = "omni-moderation-latest"
CLASSIFIER_MAX_TOKENS = 1024

CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "related_to_vibe_chat": {"type": "boolean"},
        "change_class": {
            "type": "string",
            "enum": ["frontend_ui", "out_of_scope", "internal_probe", "other"],
        },
        "reason": {"type": "string"},
    },
    "required": ["related_to_vibe_chat", "change_class", "reason"],
    "additionalProperties": False,
}


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    related_to_vibe_chat: bool
    change_class: Literal["frontend_ui", "out_of_scope", "internal_probe", "other"]
    reason: str = Field(max_length=300)


class ProviderError(Exception):
    pass


class InvalidModelResponse(Exception):
    pass


@dataclass(frozen=True)
class ClassifierReply:
    classification: Classification
    input_tokens: int
    output_tokens: int


def parse_classification(text: str) -> Classification:
    try:
        return Classification.model_validate(json.loads(text))
    except (ValueError, ValidationError) as error:
        raise InvalidModelResponse("classifier answer does not match schema") from error


class ModelsGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        anthropic_client: Any | None = None,
        openai_client: Any | None = None,
    ) -> None:
        self.settings = settings
        self._anthropic = anthropic_client
        self._openai = openai_client

    def anthropic_client(self) -> Any:
        if self._anthropic is None:
            if not self.settings.anthropic_api_key:
                raise ProviderError("ANTHROPIC_API_KEY is not set")
            self._anthropic = anthropic.Anthropic(
                api_key=self.settings.anthropic_api_key,
                base_url=self.settings.anthropic_base_url,
                max_retries=0,
                timeout=self.settings.intake_timeout_seconds,
            )
        return self._anthropic

    def openai_client(self) -> Any:
        if self._openai is None:
            if not self.settings.openai_api_key:
                raise ProviderError("OPENAI_API_KEY is not set")
            self._openai = openai.OpenAI(
                api_key=self.settings.openai_api_key,
                max_retries=0,
                timeout=self.settings.intake_timeout_seconds,
            )
        return self._openai

    def moderate(self, text: str, timeout: float) -> bool:
        client = self.openai_client()
        try:
            response = client.moderations.create(model=MODERATION_MODEL, input=text, timeout=timeout)
        except openai.APIError as error:
            raise ProviderError(f"moderation failed: {type(error).__name__}") from error
        return bool(response.results[0].flagged)

    def classify_anthropic(
        self, model: str, effort: str, system: str, user: str, timeout: float
    ) -> ClassifierReply:
        client = self.anthropic_client()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=CLASSIFIER_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": CLASSIFICATION_SCHEMA},
                },
                timeout=timeout,
            )
        except anthropic.APIError as error:
            raise ProviderError(f"anthropic failed: {type(error).__name__}") from error
        if response.stop_reason != "end_turn":
            raise InvalidModelResponse(f"anthropic stop_reason {response.stop_reason}")
        text = "".join(block.text for block in response.content if block.type == "text")
        return ClassifierReply(
            classification=parse_classification(text),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def classify_openai(
        self, model: str, effort: str, system: str, user: str, timeout: float
    ) -> ClassifierReply:
        client = self.openai_client()
        try:
            response = client.responses.create(
                model=model,
                instructions=system,
                input=user,
                reasoning={"effort": effort},
                max_output_tokens=CLASSIFIER_MAX_TOKENS,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "intake_classification",
                        "schema": CLASSIFICATION_SCHEMA,
                        "strict": True,
                    }
                },
                timeout=timeout,
            )
        except openai.APIError as error:
            raise ProviderError(f"openai failed: {type(error).__name__}") from error
        if response.status != "completed":
            raise InvalidModelResponse(f"openai status {response.status}")
        usage = response.usage
        return ClassifierReply(
            classification=parse_classification(response.output_text),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
        )
