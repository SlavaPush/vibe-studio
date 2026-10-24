import re

import httpx

SHORT_SHA = re.compile(r"[0-9a-f]{7,40}")
APP_VERSION = re.compile(r'<meta\s+name="app-version"\s+content="([0-9a-f]{7,40})"\s*/?>')
NO_CACHE = {"Cache-Control": "no-cache"}
SERVICE_TIMEOUT = 60.0
MIN_TIMEOUT = 1.0


class BaseUnavailable(Exception):
    pass


class ServiceCheck:
    def __init__(self, http: httpx.Client, url: str) -> None:
        self.http = http
        self.url = url.rstrip("/")

    def _get(self, path: str, timeout: float | None) -> httpx.Response:
        limit = httpx.USE_CLIENT_DEFAULT if timeout is None else max(timeout, MIN_TIMEOUT)
        return self.http.get(f"{self.url}{path}", headers=NO_CACHE, timeout=limit)

    def version(self, timeout: float | None = None) -> str:
        try:
            response = self._get("/api/version", timeout)
        except httpx.HTTPError:
            raise BaseUnavailable("версия сервиса недоступна: ответ не получен.") from None
        if response.status_code != 200:
            raise BaseUnavailable(f"версия сервиса недоступна: HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            data = None
        version = data.get("version") if isinstance(data, dict) else None
        if not isinstance(version, str) or not SHORT_SHA.fullmatch(version):
            raise BaseUnavailable("ответ версии сервиса не содержит SHA commit.")
        return version

    def health(self, timeout: float | None = None) -> bool:
        try:
            response = self._get("/healthz", timeout)
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return False
        return isinstance(data, dict) and data.get("status") == "ok"

    def page_version(self, timeout: float | None = None) -> str | None:
        try:
            response = self._get("/", timeout)
        except httpx.HTTPError:
            return None
        found = APP_VERSION.search(response.text) if response.status_code == 200 else None
        return found.group(1) if found else None
