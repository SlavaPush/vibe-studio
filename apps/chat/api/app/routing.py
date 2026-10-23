from dataclasses import dataclass

IMAGE_MODEL = "gpt-image-2.5-flare"
PROVIDERS = {
    "claude-haiku-5-5": "anthropic",
    "claude-sonnet-5-5": "anthropic",
    "gpt-6-luna": "openai",
    "gpt-6.1-sol": "openai",
    IMAGE_MODEL: "openai",
    "omni-moderation-latest": "openai",
}
MAX_OUTPUT_TOKENS = {
    "claude-haiku-5-5": 128_000,
    "claude-sonnet-5-5": 128_000,
    "gpt-6-luna": 128_000,
    "gpt-6.1-sol": 128_000,
}
FALLBACK = {
    "claude-haiku-5-5": "gpt-6-luna",
    "gpt-6-luna": "claude-haiku-5-5",
    "claude-sonnet-5-5": "gpt-6.1-sol",
    "gpt-6.1-sol": "claude-sonnet-5-5",
}
OUTPUT_TOKENS = {"low": 2048, "medium": 8192, "high": 16000}
CLASSIFIER_MODELS = ("claude-haiku-5-5", "gpt-6-luna")
CLASSIFIER_OUTPUT_TOKENS = 2048
TO_IMAGE_REASON = "выбранная модель не генерирует изображения"
TO_TEXT_REASON = "модель изображений не отвечает на текстовые запросы"
FALLBACK_REASON = "основной провайдер временно недоступен"


@dataclass(frozen=True)
class Target:
    model: str
    effort: str

    @property
    def provider(self) -> str:
        return PROVIDERS[self.model]

    @property
    def is_image(self) -> bool:
        return self.model == IMAGE_MODEL

    @property
    def max_output_tokens(self) -> int:
        return 0 if self.is_image else min(OUTPUT_TOKENS[self.effort], MAX_OUTPUT_TOKENS[self.model])


@dataclass(frozen=True)
class Route:
    targets: tuple[Target, ...]
    switch_reason: str | None
    switch_text: str | None


def effort_for(intent: str, complexity: str) -> str:
    if intent == "code" or complexity == "complex":
        return "high"
    return "low" if complexity == "trivial" else "medium"


def auto_model(intent: str, complexity: str) -> str:
    if intent == "chat" and complexity == "trivial":
        return "claude-haiku-5-5"
    return "claude-sonnet-5-5"


def switch_text(model: str, reason: str) -> str:
    return f"Запрос выполнен моделью {model}, потому что {reason}."


def route(requested_model: str, intent: str, complexity: str) -> Route:
    if intent == "image":
        targets = (Target(IMAGE_MODEL, "unsupported"),)
        if requested_model in ("auto", "image"):
            return Route(targets, None, None)
        return Route(targets, "intent_mismatch", switch_text(IMAGE_MODEL, TO_IMAGE_REASON))
    effort = effort_for(intent, complexity)
    model = auto_model(intent, complexity) if requested_model in ("auto", "image") else requested_model
    targets = (Target(model, effort), Target(FALLBACK[model], effort))
    if requested_model == "image":
        return Route(targets, "intent_mismatch", switch_text(model, TO_TEXT_REASON))
    return Route(targets, None, None)
