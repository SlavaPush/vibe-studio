"""Smoke-проверка приватного Supabase Storage: bucket, загрузка, подписанная ссылка, запрет прямого доступа, удаление.

Запуск из каталога api: uv run python scripts/smoke_storage.py
Переменные читаются из ../.env: SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_STORAGE_BUCKET.
Код возврата: 0 — все шаги прошли; 1 — ошибка; 2 — переменные не заданы.
"""

import json
import os
import sys
import time
from pathlib import Path

import httpx

OBJECT_PATH = "p0-check/check.txt"
CONTENT = b"vibe-chat storage smoke"


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


def main() -> int:
    env = load_env(Path(__file__).resolve().parents[2] / ".env")
    url = env.get("SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    secret = env.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SECRET_KEY")
    bucket = env.get("SUPABASE_STORAGE_BUCKET") or os.environ.get("SUPABASE_STORAGE_BUCKET") or "images"
    if not url or not secret:
        report(ok=None, skipped="SUPABASE_URL или SUPABASE_SECRET_KEY не заданы")
        return 2

    storage = f"{url.rstrip('/')}/storage/v1"
    headers = {"apikey": secret, "Authorization": f"Bearer {secret}"}
    ok = True
    with httpx.Client(timeout=30) as client:
        existing = client.get(f"{storage}/bucket/{bucket}", headers=headers)
        if existing.status_code == 200:
            public = existing.json().get("public")
            report(step="bucket", ok=public is False, exists=True, public=public)
            ok = ok and public is False
        else:
            created = client.post(f"{storage}/bucket", headers=headers, json={"id": bucket, "name": bucket, "public": False})
            report(step="bucket", ok=created.status_code == 200, exists=False, status=created.status_code)
            ok = ok and created.status_code == 200

        uploaded = client.post(
            f"{storage}/object/{bucket}/{OBJECT_PATH}",
            headers={**headers, "Content-Type": "text/plain", "x-upsert": "true"},
            content=CONTENT,
        )
        report(step="upload", ok=uploaded.status_code == 200, status=uploaded.status_code)
        ok = ok and uploaded.status_code == 200

        direct = client.get(f"{storage}/object/{bucket}/{OBJECT_PATH}")
        report(step="direct_access_denied", ok=direct.status_code in (400, 401, 403, 404), status=direct.status_code)
        ok = ok and direct.status_code in (400, 401, 403, 404)

        signed = client.post(f"{storage}/object/sign/{bucket}/{OBJECT_PATH}", headers=headers, json={"expiresIn": 60})
        signed_ok = signed.status_code == 200 and "signedURL" in signed.json()
        report(step="sign", ok=signed_ok, status=signed.status_code)
        ok = ok and signed_ok
        if signed_ok:
            started = time.monotonic()
            fetched = client.get(f"{storage}{signed.json()['signedURL']}")
            matches = fetched.status_code == 200 and fetched.content == CONTENT
            report(step="fetch_signed", ok=matches, status=fetched.status_code, seconds=round(time.monotonic() - started, 2))
            ok = ok and matches

        deleted = client.request("DELETE", f"{storage}/object/{bucket}", headers=headers, json={"prefixes": [OBJECT_PATH]})
        report(step="delete", ok=deleted.status_code == 200, status=deleted.status_code)
        ok = ok and deleted.status_code == 200

    report(summary=True, ok=ok, bucket=bucket)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
