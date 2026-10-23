"""Каталог профилей моделей для `GET /api/models`."""

from app.schemas import ModelCatalog, ModelProfile

MODEL_CATALOG = ModelCatalog(
    items=[
        ModelProfile(
            id="claude-haiku-5-5",
            provider="anthropic",
            label="Claude Haiku 5.5",
            kind="text",
            selectable=True,
        ),
        ModelProfile(
            id="claude-sonnet-5-5",
            provider="anthropic",
            label="Claude Sonnet 5.5",
            kind="text",
            selectable=True,
        ),
        ModelProfile(
            id="gpt-6-luna", provider="openai", label="GPT-6 Luna", kind="text", selectable=True
        ),
        ModelProfile(
            id="gpt-6.1-sol", provider="openai", label="GPT-6.1 Sol", kind="text", selectable=True
        ),
        ModelProfile(
            id="gpt-image-2.5-flare",
            provider="openai",
            label="GPT Image 2.5 Flare",
            kind="image",
            selectable=False,
        ),
    ]
)
