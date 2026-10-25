import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import anthropic
import httpx
import openai
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.roles import input_budget, provider_schema
from app.schemas import AgentRole, ModelProfile

if TYPE_CHECKING:
    from app.tasks import TaskStore

MODERATION_MODEL = "omni-moderation-latest"
CLASSIFIER_MAX_TOKENS = 1024
CLOUD_TIMEOUT = 300.0
OLLAMA_TIMEOUT = 600.0
OLLAMA_ERROR_CHARS = 300

ReplyT = TypeVar("ReplyT", bound=BaseModel)

CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "related_to_vibe_chat": {"type": "boolean"},
        "change_class": {
            "type": "string",
            "enum": ["frontend_ui", "backend_api", "out_of_scope", "internal_probe", "other"],
        },
        "reason": {"type": "string"},
    },
    "required": ["related_to_vibe_chat", "change_class", "reason"],
    "additionalProperties": False,
}


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    related_to_vibe_chat: bool
    change_class: Literal["frontend_ui", "backend_api", "out_of_scope", "internal_probe", "other"]
    reason: str = Field(max_length=300)


class ProviderError(Exception):
    pass


class InvalidModelResponse(Exception):
    pass


class TruncatedModelResponse(InvalidModelResponse):
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


@dataclass(frozen=True)
class Target:
    provider: str
    model: str
    effort: Any
    max_output_tokens: int
    context_tokens: int
    max_input_tokens: int | None

    @property
    def name(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass(frozen=True)
class ModelCall:
    task_id: str
    role: AgentRole
    step: str
    target: Target
    system: str
    user: str
    schema: dict[str, Any]
    timeout: float


@dataclass(frozen=True)
class ProviderReply:
    text: str
    input_tokens: int
    output_tokens: int
    complete: bool
    truncated: bool = False
    done_reason: str | None = None
    eval_duration_ns: int | None = None
    prompt_eval_duration_ns: int | None = None
    load_duration_ns: int | None = None


def _nanoseconds(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ollama_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, str) or not error.strip():
        return ""
    return ": " + " ".join(error.split())[:OLLAMA_ERROR_CHARS]


class ModelsGateway:
    def __init__(
        self,
        settings: Settings,
        *,
        anthropic_client: Any | None = None,
        openai_client: Any | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self._anthropic = anthropic_client
        self._openai = openai_client
        self._http = http_client
        self.models = yaml.safe_load(settings.models_policy.read_text(encoding="utf-8"))
        self.ollama_lock = threading.BoundedSemaphore(1)

    def http_client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(trust_env=False)
        return self._http

    def _target(self, name: str, effort: Any, wanted_tokens: int) -> Target:
        capability = self.models["capabilities"][name]
        provider, model = name.split("/", 1)
        if provider != "ollama" and effort not in capability["reasoning_values"]:
            effort = capability["reasoning_default"]
        return Target(
            provider=provider,
            model=model,
            effort=effort,
            max_output_tokens=min(wanted_tokens, capability["max_output_tokens"]),
            context_tokens=capability["context_tokens"],
            max_input_tokens=capability.get("max_input_tokens"),
        )

    def targets(self, profile: ModelProfile, role: AgentRole) -> list[Target]:
        route = self.models["profiles"][profile.value][role.value]
        wanted = route["max_output_tokens"]
        found = [self._target(f"{route['provider']}/{route['model']}", route["effort"], wanted)]
        if route.get("fallback") and route["provider"] != "ollama":
            found.append(self._target(route["fallback"], route["effort"], wanted))
        return found

    def input_budget(self, profile: ModelProfile, role: AgentRole) -> int:
        return min(
            input_budget(target.context_tokens, target.max_output_tokens, target.max_input_tokens)
            for target in self.targets(profile, role)
        )

    def timeout(self, provider: str) -> float:
        if self.settings.model_timeout_seconds is not None:
            return self.settings.model_timeout_seconds
        return OLLAMA_TIMEOUT if provider == "ollama" else CLOUD_TIMEOUT

    def structured(
        self,
        store: "TaskStore",
        task_id: str,
        profile: ModelProfile,
        role: AgentRole,
        step: str,
        system: str,
        user: str,
        reply_type: type[ReplyT],
    ) -> ReplyT:
        schema = provider_schema(reply_type)
        targets = self.targets(profile, role)
        for index, target in enumerate(targets):
            store.reserve_model_call(task_id, role, step, target)
            call = ModelCall(task_id, role, step, target, system, user, schema, self.timeout(target.provider))
            try:
                reply = self.call_provider(call)
            except ProviderError:
                if index == len(targets) - 1:
                    raise
                continue
            store.record_usage(task_id, reply.input_tokens, reply.output_tokens)
            if reply.truncated:
                raise TruncatedModelResponse(
                    f"{target.name}: answer cut at {target.max_output_tokens} output tokens"
                )
            if not reply.complete:
                raise InvalidModelResponse(f"{target.name}: incomplete answer")
            try:
                return reply_type.model_validate(json.loads(reply.text))
            except (ValueError, ValidationError) as error:
                raise InvalidModelResponse(f"{target.name}: answer does not match schema") from error
        raise ProviderError("no model route")

    def call_provider(self, call: ModelCall) -> ProviderReply:
        handler = {
            "anthropic": self.call_anthropic,
            "openai": self.call_openai,
            "ollama": self.call_ollama,
        }.get(call.target.provider)
        if handler is None:
            raise ProviderError(f"unknown provider {call.target.provider}")
        return handler(call)

    def call_anthropic(self, call: ModelCall) -> ProviderReply:
        client = self.anthropic_client()
        try:
            response = client.messages.create(
                model=call.target.model,
                max_tokens=call.target.max_output_tokens,
                system=call.system,
                messages=[{"role": "user", "content": call.user}],
                output_config={
                    "effort": call.target.effort,
                    "format": {"type": "json_schema", "schema": call.schema},
                },
                timeout=call.timeout,
            )
        except anthropic.APIError as error:
            raise ProviderError(f"anthropic failed: {type(error).__name__}") from error
        text = "".join(block.text for block in response.content if block.type == "text")
        return ProviderReply(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            complete=response.stop_reason == "end_turn",
            truncated=response.stop_reason == "max_tokens",
        )

    def call_openai(self, call: ModelCall) -> ProviderReply:
        client = self.openai_client()
        try:
            response = client.responses.create(
                model=call.target.model,
                instructions=call.system,
                input=call.user,
                reasoning={"effort": call.target.effort},
                max_output_tokens=call.target.max_output_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": f"{call.role.value}_{call.step}",
                        "schema": call.schema,
                        "strict": True,
                    }
                },
                timeout=call.timeout,
            )
        except openai.APIError as error:
            raise ProviderError(f"openai failed: {type(error).__name__}") from error
        usage = response.usage
        return ProviderReply(
            text=response.output_text,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            complete=response.status == "completed",
            truncated=getattr(response.incomplete_details, "reason", None) == "max_output_tokens",
        )

    def call_ollama(self, call: ModelCall) -> ProviderReply:
        body = {
            "model": call.target.model,
            "messages": [
                {"role": "system", "content": call.system},
                {"role": "user", "content": call.user},
            ],
            "format": call.schema,
            "think": bool(call.target.effort),
            "stream": False,
            "options": {"num_predict": call.target.max_output_tokens, "num_ctx": call.target.context_tokens},
        }
        with self.ollama_lock:
            try:
                response = self.http_client().post(
                    f"{self.settings.ollama_url.rstrip('/')}/api/chat", json=body, timeout=call.timeout
                )
            except httpx.HTTPError as error:
                raise ProviderError(f"ollama failed: {type(error).__name__}") from error
        if response.status_code != 200:
            raise ProviderError(f"ollama status {response.status_code}{_ollama_error(response)}")
        try:
            data = response.json()
        except ValueError as error:
            raise ProviderError("ollama answer is not JSON") from error
        return ProviderReply(
            text=(data.get("message") or {}).get("content") or "",
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            complete=data.get("done") is True and data.get("done_reason") == "stop",
            truncated=data.get("done_reason") == "length",
            done_reason=data["done_reason"] if isinstance(data.get("done_reason"), str) else None,
            eval_duration_ns=_nanoseconds(data.get("eval_duration")),
            prompt_eval_duration_ns=_nanoseconds(data.get("prompt_eval_duration")),
            load_duration_ns=_nanoseconds(data.get("load_duration")),
        )

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
