"""Smoke-проверка локальной модели Ollama: завершённый ответ с рассуждением и без, время и токены.

Запуск из каталога api: uv run python scripts/smoke_ollama.py [модель]
Код возврата: 0 — оба ответа завершены и непусты; 1 — ошибка или незавершённый ответ.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PROMPT = "Ответь одним предложением: что делает git worktree?"


def chat(model: str, think: bool) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "think": think,
        "options": {"num_predict": 1024 if think else 200},
    }
    request = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = json.load(response)
    payload["wall_seconds"] = round(time.monotonic() - started, 1)
    return payload


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3:8b"
    ok = True
    for think in (False, True):
        try:
            result = chat(model, think)
        except (urllib.error.URLError, TimeoutError) as error:
            print(json.dumps({"model": model, "think": think, "ok": False, "error": type(error).__name__}, ensure_ascii=False))
            ok = False
            continue
        message = result.get("message", {})
        answer = (message.get("content") or "").strip()
        completed = result.get("done") is True and result.get("done_reason") == "stop" and bool(answer)
        ok = ok and completed
        print(
            json.dumps(
                {
                    "model": model,
                    "think": think,
                    "ok": completed,
                    "done_reason": result.get("done_reason"),
                    "wall_seconds": result["wall_seconds"],
                    "load_seconds": round(result.get("load_duration", 0) / 1e9, 1),
                    "eval_tokens": result.get("eval_count"),
                    "eval_tokens_per_second": round(
                        result.get("eval_count", 0) / max(result.get("eval_duration", 1) / 1e9, 1e-9), 1
                    ),
                    "thinking_chars": len(message.get("thinking") or ""),
                    "answer": answer[:300],
                },
                ensure_ascii=False,
            )
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
