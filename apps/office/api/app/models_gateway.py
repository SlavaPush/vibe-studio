import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import anthropic
import openai
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import Settings
from app.roles import input_budget, provider_schema
from app.schemas import AgentRole

if TYPE_CHECKING:
    from app.tasks import TaskStore

logger = logging.getLogger("office.models")

MODERATION_MODEL = "omni-moderation-latest"
CLASSIFIER_MAX_TOKENS = 1024
CLOUD_TIMEOUT = 300.0
INTAKE_ROLE = "intake_filter"
ROUTED_ROLES = (*(role.value for role in AgentRole), INTAKE_ROLE)

UsageSink = Callable[[int, int, float], None]

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
        "title": {"type": "string"},
    },
    "required": ["related_to_vibe_chat", "change_class", "reason", "title"],
    "additionalProperties": False,
}


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    related_to_vibe_chat: bool
    change_class: Literal["frontend_ui", "backend_api", "out_of_scope", "internal_probe", "other"]
    reason: str = Field(max_length=300)
    title: str


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


class UsageMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sink: UsageSink | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0

    def add(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        with self._lock:
            if self._sink is not None:
                self._sink(input_tokens, output_tokens, cost_usd)
                return
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            self.cost_usd += cost_usd

    def snapshot(self) -> tuple[int, int, float]:
        with self._lock:
            return self.input_tokens, self.output_tokens, self.cost_usd

    def attach(self, sink: UsageSink, recorded: tuple[int, int, float]) -> None:
        with self._lock:
            self._sink = sink
            rest = (
                self.input_tokens - recorded[0],
                self.output_tokens - recorded[1],
                self.cost_usd - recorded[2],
            )
            if rest[0] or rest[1]:
                sink(*rest)


def check_policy(models: dict[str, Any]) -> None:
    capabilities = models.get("capabilities") or {}
    roles = models.get("roles") or {}
    for role in ROUTED_ROLES:
        route = roles.get(role)
        if route is None:
            raise ValueError(f"models.yaml: no route for role {role}")
        for name in (f"{route['provider']}/{route['model']}", route.get("fallback")):
            if name and name not in capabilities:
                raise ValueError(f"models.yaml: {name} of role {role} has no capabilities")


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
        self.models = yaml.safe_load(settings.models_policy.read_text(encoding="utf-8"))
        check_policy(self.models)

    def cost(self, name: str, input_tokens: int, output_tokens: int) -> float:
        price = self.models["capabilities"].get(name, {}).get("price_usd_per_mtok")
        if not price:
            logger.warning("model %s has no price, cost is not counted", name)
            return 0.0
        return (max(0, input_tokens) * price["input"] + max(0, output_tokens) * price["output"]) / 1_000_000

    def _target(self, name: str, effort: Any, wanted_tokens: int) -> Target:
        capability = self.models["capabilities"][name]
        provider, model = name.split("/", 1)
        if effort not in capability["reasoning_values"]:
            effort = capability["reasoning_default"]
        return Target(
            provider=provider,
            model=model,
            effort=effort,
            max_output_tokens=min(wanted_tokens, capability["max_output_tokens"]),
            context_tokens=capability["context_tokens"],
            max_input_tokens=capability.get("max_input_tokens"),
        )

    def targets(self, role: AgentRole) -> list[Target]:
        route = self.models["roles"][role.value]
        wanted = route["max_output_tokens"]
        found = [self._target(f"{route['provider']}/{route['model']}", route["effort"], wanted)]
        if route.get("fallback"):
            found.append(self._target(route["fallback"], route["effort"], wanted))
        return found

    def input_budget(self, role: AgentRole) -> int:
        return min(
            input_budget(target.context_tokens, target.max_output_tokens, target.max_input_tokens)
            for target in self.targets(role)
        )

    def timeout(self, provider: str) -> float:
        if self.settings.model_timeout_seconds is not None:
            return self.settings.model_timeout_seconds
        return CLOUD_TIMEOUT

    def structured(
        self,
        store: "TaskStore",
        task_id: str,
        role: AgentRole,
        step: str,
        system: str,
        user: str,
        reply_type: type[ReplyT],
    ) -> ReplyT:
        schema = provider_schema(reply_type)
        targets = self.targets(role)
        for index, target in enumerate(targets):
            store.reserve_model_call(task_id, role, step, target)
            call = ModelCall(task_id, role, step, target, system, user, schema, self.timeout(target.provider))
            try:
                reply = self.call_provider(call)
            except ProviderError:
                if index == len(targets) - 1:
                    raise
                continue
            store.record_usage(
                task_id,
                reply.input_tokens,
                reply.output_tokens,
                self.cost(target.name, reply.input_tokens, reply.output_tokens),
            )
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

    def _meter(self, meter: UsageMeter | None, name: str, input_tokens: int, output_tokens: int) -> None:
        if meter is not None:
            meter.add(input_tokens, output_tokens, self.cost(name, input_tokens, output_tokens))

    def classify_anthropic(
        self, model: str, effort: str, system: str, user: str, timeout: float, meter: UsageMeter | None = None
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
        self._meter(meter, f"anthropic/{model}", response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason != "end_turn":
            raise InvalidModelResponse(f"anthropic stop_reason {response.stop_reason}")
        text = "".join(block.text for block in response.content if block.type == "text")
        return ClassifierReply(
            classification=parse_classification(text),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def classify_openai(
        self, model: str, effort: str, system: str, user: str, timeout: float, meter: UsageMeter | None = None
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
        usage = response.usage
        self._meter(meter, f"openai/{model}", usage.input_tokens if usage else 0, usage.output_tokens if usage else 0)
        if response.status != "completed":
            raise InvalidModelResponse(f"openai status {response.status}")
        return ClassifierReply(
            classification=parse_classification(response.output_text),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
        )
