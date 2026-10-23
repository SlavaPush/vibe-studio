"""Smoke-проверка локальной модели Ollama: ответ с рассуждением и без, время и токены.

Запуск: python3 -I scripts/smoke_ollama.py [модель]
"""

import json
import sys
import time
import urllib.request

OLLAMA_URL = "http://127.0.0.1:11434"
PROMPT = "Ответь одним предложением: что делает git worktree?"


def chat(model: str, think: bool) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "think": think,
        "options": {"num_predict": 200},
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
    for think in (False, True):
        result = chat(model, think)
        message = result["message"]
        print(
            json.dumps(
                {
                    "model": model,
                    "think": think,
                    "wall_seconds": result["wall_seconds"],
                    "load_seconds": round(result.get("load_duration", 0) / 1e9, 1),
                    "eval_tokens": result.get("eval_count"),
                    "eval_tokens_per_second": round(
                        result.get("eval_count", 0) / max(result.get("eval_duration", 1) / 1e9, 1e-9), 1
                    ),
                    "thinking_chars": len(message.get("thinking") or ""),
                    "answer": message.get("content", "").strip()[:300],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
