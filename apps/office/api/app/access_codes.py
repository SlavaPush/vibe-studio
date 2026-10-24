import hashlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.config import Settings
from app.db import Database
from app.errors import ErrorCode, OfficeError

CLAIM_SQL = (
    "UPDATE access_codes SET office_used_at = CURRENT_TIMESTAMP "
    "WHERE code_hash = :hash AND revoked_at IS NULL AND office_used_at IS NULL "
    "AND office_role IN ('participant', 'admin') "
    "RETURNING CAST(id AS TEXT) AS id, label, office_role"
)
ACTIVE_SQL = "SELECT 1 AS active FROM access_codes WHERE CAST(id AS TEXT) = :id AND revoked_at IS NULL"
POOL_SIZE = 2

logger = logging.getLogger("office.access_codes")


def access_code_hash(code: str) -> str:
    normalized = "".join(code.split()).replace("-", "").lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def codes_unavailable() -> OfficeError:
    return OfficeError(
        503,
        ErrorCode.codes_unavailable,
        "Проверка кодов доступа временно недоступна. Повторите позже.",
        retryable=True,
    )


class AccessCodes:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self.check_seconds = settings.codes_check_seconds
        self.clock = clock
        self._checked: dict[str, float] = {}
        self._lock = threading.Lock()
        self.db: Database | None = None
        if settings.codes_database_url:
            try:
                self.db = Database(settings.codes_database_url, max_size=POOL_SIZE)
            except ValueError:
                raise ValueError("OFFICE_CODES_DATABASE_URL: ожидается sqlite:/// или postgresql://") from None

    def _query(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self.db is None:
            logger.error("OFFICE_CODES_DATABASE_URL не задан: вход в офис невозможен")
            raise codes_unavailable()
        try:
            with self.db.connection() as conn:
                return conn.fetchall(sql, params)
        except Exception:
            logger.exception("access codes registry failed")
            raise codes_unavailable() from None

    def claim(self, code: str) -> dict[str, Any] | None:
        rows = self._query(CLAIM_SQL, {"hash": access_code_hash(code)})
        return rows[0] if rows else None

    def active(self, code_id: str) -> bool:
        now = self.clock()
        with self._lock:
            if self._checked.get(code_id, 0.0) > now:
                return True
        if not self._query(ACTIVE_SQL, {"id": code_id}):
            return False
        if self.check_seconds > 0:
            with self._lock:
                self._checked[code_id] = now + self.check_seconds
        return True

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
