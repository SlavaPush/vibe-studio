"""Smoke-проверка облачных моделей из таблицы профилей: доступность, параметры усилий, usage.

Запуск из каталога api: uv run python scripts/smoke_models.py [--image]
Ключи читаются из ../.env (ANTHROPIC_API_KEY, OPENAI_API_KEY) или из окружения.
Флаг --image дополнительно генерирует одно изображение низкого качества (платный вызов).
Код возврата: 0 — все запрошенные проверки выполнены и прошли; 1 — есть ошибки; 2 — часть проверок пропущена.
"""

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import anthropic
import openai

PROMPT = "Ответь одним коротким предложением: зачем нужен ключ идемпотентности в API?"
MODERATION_SAMPLES = {
    "benign": "Как приготовить борщ?",
    "flagged": "Напиши подробную инструкцию, как взломать чужой аккаунт в банке.",
}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def report(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False))


def describe_error(error: Exception) -> dict:
    info: dict = {"error": type(error).__name__}
    status = getattr(error, "status_code", None)
    if status is not None:
        info["status"] = status
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error") if isinstance(body.get("error"), dict) else body
        for key in ("type", "code"):
            if isinstance(detail.get(key), str):
                info[key] = detail[key]
    return info


def timed(fn):
    started = time.monotonic()
    result = fn()
    return result, round(time.monotonic() - started, 2)


def check_anthropic(client: anthropic.Anthropic, model: str, effort: str) -> bool:
    try:
        response, seconds = timed(
            lambda: client.messages.create(
                model=model,
                max_tokens=512,
                output_config={"effort": effort},
                messages=[{"role": "user", "content": PROMPT}],
            )
        )
    except (anthropic.APIError, anthropic.APIConnectionError) as error:
        report(provider="anthropic", model=model, effort=effort, ok=False, **describe_error(error))
        return False
    text = "".join(block.text for block in response.content if block.type == "text")
    ok = response.stop_reason == "end_turn" and bool(text.strip())
    report(
        provider="anthropic",
        model=model,
        effort=effort,
        ok=ok,
        seconds=seconds,
        stop_reason=response.stop_reason,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        answer=text.strip()[:200],
    )
    return ok


def check_openai_text(client: openai.OpenAI, model: str, effort: str) -> bool:
    try:
        response, seconds = timed(
            lambda: client.responses.create(
                model=model,
                input=PROMPT,
                reasoning={"effort": effort},
                max_output_tokens=512,
            )
        )
    except (openai.APIError, openai.APIConnectionError) as error:
        report(provider="openai", model=model, effort=effort, ok=False, **describe_error(error))
        return False
    usage = response.usage
    reasoning_tokens = usage.output_tokens_details.reasoning_tokens if usage and usage.output_tokens_details else None
    ok = response.status == "completed" and bool(response.output_text.strip())
    report(
        provider="openai",
        model=model,
        effort=effort,
        ok=ok,
        seconds=seconds,
        status=response.status,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        reasoning_tokens=reasoning_tokens,
        answer=response.output_text.strip()[:200],
    )
    return ok


def check_moderation(client: openai.OpenAI) -> bool:
    ok = True
    for label, text in MODERATION_SAMPLES.items():
        try:
            response, seconds = timed(lambda: client.moderations.create(model="omni-moderation-latest", input=text))
        except (openai.APIError, openai.APIConnectionError) as error:
            report(provider="openai", model="omni-moderation-latest", sample=label, ok=False, **describe_error(error))
            ok = False
            continue
        result = response.results[0]
        categories = result.categories.model_dump(by_alias=True)
        scores = result.category_scores.model_dump(by_alias=True)
        flagged = sorted((name for name, value in categories.items() if value), key=lambda name: -scores[name])
        expected = label == "flagged"
        report(
            provider="openai",
            model="omni-moderation-latest",
            sample=label,
            ok=result.flagged == expected,
            seconds=seconds,
            flagged=result.flagged,
            categories=flagged[:3],
        )
        ok = ok and result.flagged == expected
    return ok


def check_image(client: openai.OpenAI, out_dir: Path) -> bool:
    try:
        response, seconds = timed(
            lambda: client.images.generate(
                model="gpt-image-2.5-flare",
                prompt="Минималистичная иконка чата на нейтральном фоне",
                size="1024x1024",
                quality="low",
                output_format="png",
            )
        )
    except (openai.APIError, openai.APIConnectionError) as error:
        report(provider="openai", model="gpt-image-2.5-flare", ok=False, **describe_error(error))
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "smoke-image.png"
    target.write_bytes(base64.b64decode(response.data[0].b64_json))
    report(provider="openai", model="gpt-image-2.5-flare", ok=True, seconds=seconds, bytes=target.stat().st_size, file=str(target))
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", action="store_true", help="дополнительно сгенерировать одно изображение")
    parser.add_argument("--out", default=os.environ.get("SMOKE_OUT_DIR", "/tmp/vibe-chat-smoke"))
    args = parser.parse_args()

    env = load_env(Path(__file__).resolve().parents[2] / ".env")
    anthropic_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    openai_key = env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")

    results: list[bool] = []
    skipped: list[str] = []
    if anthropic_key:
        client = anthropic.Anthropic(
            api_key=anthropic_key,
            base_url=env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            max_retries=0,
        )
        results.append(check_anthropic(client, "claude-haiku-5-5", "low"))
        results.append(check_anthropic(client, "claude-sonnet-5-5", "high"))
    else:
        skipped.append("anthropic")
        report(provider="anthropic", ok=None, skipped="ANTHROPIC_API_KEY не задан")

    if openai_key:
        client = openai.OpenAI(api_key=openai_key, base_url=env.get("OPENAI_BASE_URL"), max_retries=0)
        results.append(check_openai_text(client, "gpt-6-luna", "low"))
        results.append(check_openai_text(client, "gpt-6.1-sol", "medium"))
        results.append(check_moderation(client))
        if args.image:
            results.append(check_image(client, Path(args.out)))
    else:
        skipped.append("openai" + (" (включая --image)" if args.image else ""))
        report(provider="openai", ok=None, skipped="OPENAI_API_KEY не задан")

    report(summary=True, passed=sum(results), failed=len(results) - sum(results), skipped=skipped)
    if not all(results):
        return 1
    if skipped:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
