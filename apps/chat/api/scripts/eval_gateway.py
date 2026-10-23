"""Проверка шлюза входа на эталонных запросах с реальными модерацией и классификатором (без основной модели).

Запуск из каталога api: uv run python scripts/eval_gateway.py
Ключи читаются из ../.env (ANTHROPIC_API_KEY, OPENAI_API_KEY). Каждый случай — платный вызов классификатора.
Код возврата: 0 — решение (allow/refuse) совпало во всех случаях; 1 — есть расхождение или ошибка.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import policy, retry
from app.config import load_settings
from app.providers import Providers
from app.routing import CLASSIFIER_MODELS, CLASSIFIER_OUTPUT_TOKENS

CASES = Path(__file__).resolve().parents[1] / "tests" / "data" / "gateway_cases.json"


def report(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False), flush=True)


class CheckUnavailable(Exception):
    pass


async def classify(providers: Providers, text: str):
    data = policy.classifier_input(text, [])
    invalid_left = 1
    for model in CLASSIFIER_MODELS:
        while True:
            try:
                labels, _ = await providers.classify(model, policy.CLASSIFIER_PROMPT, data, CLASSIFIER_OUTPUT_TOKENS)
                return labels, model
            except Exception as error:
                kind = retry.classify(error)
                if kind == retry.INVALID and invalid_left:
                    invalid_left -= 1
                    continue
                if kind in (retry.UNKNOWN, retry.INVALID):
                    raise CheckUnavailable(f"{model}: {kind} ({type(error).__name__})") from error
                break
    raise CheckUnavailable("classifier models refused the request")


async def evaluate(providers: Providers, case: dict, limit: asyncio.Semaphore) -> dict:
    async with limit:
        try:
            scores, (labels, model) = await asyncio.gather(
                providers.moderate(case["text"]), classify(providers, case["text"])
            )
        except Exception as error:
            return {"id": case["id"], "ok": False, "error": str(error) if isinstance(error, CheckUnavailable) else type(error).__name__}
    flagged = policy.moderation_flagged(scores)
    decision = "refuse" if flagged or labels.risk != "none" else "allow"
    expected = case["labels"]
    return {
        "id": case["id"],
        "ok": decision == case["decision"],
        "decision": decision,
        "expected_decision": case["decision"],
        "moderation_flagged": flagged,
        "classifier_model": model,
        "intent": labels.intent,
        "complexity": labels.complexity,
        "risk": labels.risk,
        "intent_match": labels.intent == expected["intent"],
        "complexity_match": labels.complexity == expected["complexity"],
        "top_categories": sorted(scores, key=lambda name: -scores[name])[:2],
    }


async def main() -> int:
    settings = load_settings()
    if not settings.openai_api_key or not settings.anthropic_api_key:
        report(ok=None, skipped="ANTHROPIC_API_KEY или OPENAI_API_KEY не заданы")
        return 1
    providers = Providers(settings)
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    limit = asyncio.Semaphore(3)
    results = await asyncio.gather(*(evaluate(providers, case, limit) for case in cases))
    for result in results:
        report(**result)
    checked = [result for result in results if "intent_match" in result]
    report(
        summary=True,
        cases=len(results),
        decision_mismatches=[result["id"] for result in results if not result["ok"]],
        intent_match=round(sum(r["intent_match"] for r in checked) / max(len(checked), 1), 2),
        complexity_match=round(sum(r["complexity_match"] for r in checked) / max(len(checked), 1), 2),
    )
    return 0 if all(result["ok"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
