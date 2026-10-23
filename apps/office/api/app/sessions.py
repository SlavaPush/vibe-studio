import hashlib
import hmac
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import timedelta

from app.config import Settings
from app.db import Database, from_text, to_text, utcnow
from app.errors import ErrorCode, OfficeError, rate_limited
from app.schemas import OfficeSessionRequest, Participant, Role
from app.tasks import new_id

COOKIE_NAME = "office_session"
LOGIN_WINDOW_SECONDS = 60


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

    def attempt(self, client: str, verify: Callable[[], Role | None]) -> Role | None:
        now = time.monotonic()
        with self._lock:
            failures = self._recent(client, now)
            if len(failures) >= self.limit:
                wait = math.ceil(failures[0] + self.window - now)
                raise rate_limited("Слишком много попыток входа. Повторите через минуту.", wait)
            role = verify()
            if role is None:
                failures.append(now)
            return role


def _matches(code: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(code.encode("utf-8"), expected.encode("utf-8"))


class SessionStore:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.limiter = LoginLimiter(settings.login_failures_per_minute)

    def role_for(self, code: str) -> Role | None:
        if _matches(code, self.settings.admin_code):
            return Role.admin
        if _matches(code, self.settings.access_code):
            return Role.participant
        return None

    def login(
        self, request: OfficeSessionRequest, client: str, current_token: str | None
    ) -> tuple[Participant, str]:
        role = self.limiter.attempt(client, lambda: self.role_for(request.access_code))
        if role is None:
            raise OfficeError(401, ErrorCode.invalid_access_code, "Неверный код доступа офиса.")
        name = request.display_name.strip()
        if not name:
            raise OfficeError(422, ErrorCode.validation_error, "Укажите отображаемое имя.")

        current = self.participant(current_token) if current_token else None
        token = secrets.token_urlsafe(32)
        now = utcnow()
        expires_at = now + timedelta(hours=self.settings.session_ttl_hours)
        with self.db.transaction() as conn:
            if current is not None and current.role == role:
                participant_id = current.id
                conn.execute("DELETE FROM sessions WHERE token_hash = :hash", {"hash": token_hash(current_token)})
                conn.execute(
                    "UPDATE participants SET display_name = :name WHERE id = :id",
                    {"name": name, "id": participant_id},
                )
            else:
                participant_id = new_id("prt")
                conn.execute(
                    "INSERT INTO participants (id, display_name, role, created_at) VALUES (:id, :name, :role, :now)",
                    {"id": participant_id, "name": name, "role": role.value, "now": to_text(now)},
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

    def participant(self, token: str | None) -> Participant | None:
        if not token:
            return None
        with self.db.connection() as conn:
            row = conn.fetchone(
                "SELECT p.id, p.display_name, p.role, s.expires_at FROM sessions s "
                "JOIN participants p ON p.id = s.participant_id "
                "WHERE s.token_hash = :hash AND s.expires_at > :now",
                {"hash": token_hash(token), "now": to_text(utcnow())},
            )
        if row is None:
            return None
        return Participant(
            id=row["id"],
            display_name=row["display_name"],
            role=row["role"],
            expires_at=from_text(row["expires_at"]),
        )
