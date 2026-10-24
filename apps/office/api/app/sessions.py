import hashlib
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta
from typing import TypeVar

from app.access_codes import AccessCodes
from app.config import Settings
from app.db import Database, from_text, to_text, utcnow
from app.errors import ErrorCode, OfficeError, rate_limited
from app.schemas import OfficeSessionRequest, Participant, Role
from app.tasks import new_id

COOKIE_NAME = "office_session"
LOGIN_WINDOW_SECONDS = 60
T = TypeVar("T")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def unauthorized() -> OfficeError:
    return OfficeError(401, ErrorCode.unauthorized, "Сеанс не найден или истёк. Войдите по коду офиса.")


class LoginLimiter:
    def __init__(self, limit: int, window: float = LOGIN_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, client: str, now: float) -> deque[float]:
        failures = self._failures.setdefault(client, deque())
        while failures and failures[0] <= now - self.window:
            failures.popleft()
        return failures

    def attempt(self, client: str, verify: Callable[[], T | None]) -> T | None:
        now = time.monotonic()
        with self._lock:
            failures = self._recent(client, now)
            if len(failures) >= self.limit:
                wait = math.ceil(failures[0] + self.window - now)
                raise rate_limited("Слишком много попыток входа. Повторите через минуту.", wait)
            result = verify()
            if result is None:
                failures.append(now)
            return result


class SessionStore:
    def __init__(self, db: Database, settings: Settings, codes: AccessCodes) -> None:
        self.db = db
        self.settings = settings
        self.codes = codes
        self.limiter = LoginLimiter(settings.login_failures_per_minute)

    def login(
        self, request: OfficeSessionRequest, client: str, current_token: str | None
    ) -> tuple[Participant, str]:
        code = self.limiter.attempt(client, lambda: self.codes.claim(request.access_code))
        if code is None:
            raise OfficeError(401, ErrorCode.invalid_access_code, "Неверный код доступа офиса.")
        role = Role(code["office_role"])
        name = code["label"]
        participant_id = new_id("prt")
        token = secrets.token_urlsafe(32)
        now = utcnow()
        expires_at = now + timedelta(hours=self.settings.session_ttl_hours)
        with self.db.transaction() as conn:
            if current_token:
                conn.execute("DELETE FROM sessions WHERE token_hash = :hash", {"hash": token_hash(current_token)})
            conn.execute(
                "INSERT INTO participants (id, display_name, role, created_at, access_code_id) "
                "VALUES (:id, :name, :role, :now, :code)",
                {"id": participant_id, "name": name, "role": role.value, "now": to_text(now), "code": code["id"]},
            )
            conn.execute(
                "INSERT INTO sessions (token_hash, participant_id, created_at, expires_at) "
                "VALUES (:hash, :participant, :now, :expires)",
                {
                    "hash": token_hash(token),
                    "participant": participant_id,
                    "now": to_text(now),
                    "expires": to_text(expires_at),
                },
            )
        participant = Participant(id=participant_id, display_name=name, role=role, expires_at=expires_at)
        return participant, token

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = :hash", {"hash": token_hash(token)})

    def participant(self, token: str | None) -> Participant | None:
        if not token:
            return None
        with self.db.connection() as conn:
            row = conn.fetchone(
                "SELECT p.id, p.display_name, p.role, p.access_code_id, s.expires_at FROM sessions s "
                "JOIN participants p ON p.id = s.participant_id "
                "WHERE s.token_hash = :hash AND s.expires_at > :now",
                {"hash": token_hash(token), "now": to_text(utcnow())},
            )
        if row is None or row["access_code_id"] is None or not self.codes.active(row["access_code_id"]):
            return None
        return Participant(
            id=row["id"],
            display_name=row["display_name"],
            role=row["role"],
            expires_at=from_text(row["expires_at"]),
        )
