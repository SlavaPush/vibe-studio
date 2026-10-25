"""Smoke-проверка шлюза ролей: по одному вызову со схемой ответа QA на Anthropic и OpenAI.

Запуск из каталога api: PYTHONPATH=. uv run --env-file ../.env python scripts/smoke_roles.py [провайдер ...]
Аргументы ограничивают проверку названными провайдерами (anthropic, openai); без аргументов проверяются оба.
Код возврата: 0 — все ответы завершены и проходят схему; 1 — иначе. Ключи не выводятся.
"""

import json
import sys
import time

from pydantic import ValidationError

from app.config import Settings
from app.models_gateway import ModelCall, ModelsGateway, ProviderError
from app.roles import QA, QaTurn, Entry, assemble, provider_schema
from app.schemas import AgentRole

TASK = "Добавить под каждым ответом ассистента кнопку копирования текста ответа."
FIXED = [
    Entry("Текст задачи участника", TASK, required=True),
    Entry("Требования", "Кнопка копирования видна под ответом ассистента.\n\nКритерии приёмки:\n1. Кнопка видна.", required=True),
    Entry("Файл теста QA", "apps/chat/web/e2e/office/tsk_smoke.pw.ts", required=True),
    Entry("Файлы apps/chat/web/src", "apps/chat/web/src/App.tsx\napps/chat/web/src/components/chat/MessageActions.tsx"),
]


def main() -> int:
    settings = Settings.from_env()
    gateway = ModelsGateway(settings)
    targets = gateway.targets(AgentRole.qa)
    wanted = set(sys.argv[1:])
    unknown = wanted - {target.provider for target in targets}
    if unknown:
        print(f"unknown provider: {sorted(unknown)}", file=sys.stderr)
        return 1
    if wanted:
        targets = [target for target in targets if target.provider in wanted]
    ok = True
    for target in targets:
        budget = gateway.input_budget(AgentRole.qa)
        call = ModelCall(
            task_id="smoke",
            role=AgentRole.qa,
            step="smoke",
            target=target,
            system=QA,
            user=assemble(QA, FIXED, [], budget),
            schema=provider_schema(QaTurn),
            timeout=gateway.timeout(target.provider),
        )
        started = time.monotonic()
        result = {"model": target.name, "effort": target.effort}
        try:
            reply = gateway.call_provider(call)
        except ProviderError as error:
            result |= {"ok": False, "error": str(error)}
            ok = False
            print(json.dumps(result, ensure_ascii=False))
            continue
        try:
            parsed = QaTurn.model_validate(json.loads(reply.text))
            valid = True
            detail = {"actions": [action.kind for action in parsed.actions], "done": parsed.done}
        except (ValueError, ValidationError) as error:
            valid = False
            detail = {"error": type(error).__name__, "head": reply.text[:200], "tail": reply.text[-200:]}
        result |= {
            "ok": reply.complete and valid,
            "complete": reply.complete,
            "truncated": reply.truncated,
            "valid": valid,
            "seconds": round(time.monotonic() - started, 1),
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
        } | detail
        ok = ok and result["ok"]
        print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
