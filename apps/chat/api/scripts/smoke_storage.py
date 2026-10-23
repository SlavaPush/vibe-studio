"""Smoke-проверка приватного Supabase Storage: bucket, загрузка, подписанная ссылка, запрет публичного доступа, удаление.

Запуск из каталога api: uv run python scripts/smoke_storage.py
Переменные читаются из ../.env: SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_STORAGE_BUCKET.
Каждый запуск создаёт объект с уникальным путём и удаляет только его.
Код возврата: 0 — все шаги прошли; 1 — ошибка; 2 — переменные не заданы.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

CONTENT = b"vibe-chat storage smoke"
DENIED_STATUSES = {400, 401, 403, 404}


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


def ensure_private_bucket(client: httpx.Client, storage: str, headers: dict, bucket: str) -> bool:
    existing = client.get(f"{storage}/bucket/{bucket}", headers=headers)
    if existing.status_code == 200:
        public = existing.json().get("public")
        report(step="bucket", ok=public is False, exists=True, public=public)
        return public is False
    created = client.post(f"{storage}/bucket", headers=headers, json={"id": bucket, "name": bucket, "public": False})
    report(step="bucket", ok=created.status_code == 200, exists=False, status=created.status_code)
    return created.status_code == 200


def check_object(client: httpx.Client, storage: str, headers: dict, bucket: str, path: str) -> bool:
    uploaded = client.post(
        f"{storage}/object/{bucket}/{path}",
        headers={**headers, "Content-Type": "text/plain"},
        content=CONTENT,
    )
    report(step="upload", ok=uploaded.status_code == 200, status=uploaded.status_code)
    if uploaded.status_code != 200:
        return False

    public = client.get(f"{storage}/object/public/{bucket}/{path}")
    public_denied = public.status_code in DENIED_STATUSES
    report(step="public_access_denied", ok=public_denied, status=public.status_code)

    anonymous = client.get(f"{storage}/object/{bucket}/{path}")
    anonymous_denied = anonymous.status_code in DENIED_STATUSES
    report(step="anonymous_access_denied", ok=anonymous_denied, status=anonymous.status_code)

    signed = client.post(f"{storage}/object/sign/{bucket}/{path}", headers=headers, json={"expiresIn": 60})
    signed_url = signed.json().get("signedURL") if signed.status_code == 200 else None
    report(step="sign", ok=bool(signed_url), status=signed.status_code)
    fetched_ok = False
    if signed_url:
        started = time.monotonic()
        fetched = client.get(f"{storage}{signed_url}")
        fetched_ok = fetched.status_code == 200 and fetched.content == CONTENT
        report(step="fetch_signed", ok=fetched_ok, status=fetched.status_code, seconds=round(time.monotonic() - started, 2))

    return public_denied and anonymous_denied and bool(signed_url) and fetched_ok


def delete_object(client: httpx.Client, storage: str, headers: dict, bucket: str, path: str) -> bool:
    try:
        deleted = client.request("DELETE", f"{storage}/object/{bucket}", headers=headers, json={"prefixes": [path]})
    except httpx.HTTPError as error:
        report(step="delete", ok=False, error=type(error).__name__)
        return False
    report(step="delete", ok=deleted.status_code == 200, status=deleted.status_code)
    return deleted.status_code == 200


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
    path = f"smoke/{uuid.uuid4()}.txt"
    ok = False
    uploaded = False
    with httpx.Client(timeout=30) as client:
        try:
            if ensure_private_bucket(client, storage, headers, bucket):
                uploaded = True
                ok = check_object(client, storage, headers, bucket, path)
        except (httpx.HTTPError, ValueError) as error:
            report(step="error", ok=False, error=type(error).__name__)
            ok = False
        finally:
            if uploaded:
                ok = delete_object(client, storage, headers, bucket, path) and ok

    report(summary=True, ok=ok, bucket=bucket)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
