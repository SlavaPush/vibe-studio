import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.schemas import ApiError, ErrorCode

log = logging.getLogger("vibe_chat")

MESSAGES: dict[str, str] = {
    "unauthorized": "Сеанс не найден или истёк. Введите код доступа.",
    "invalid_demo_code": "Неверный код доступа.",
    "csrf_failed": "Запрос отклонён проверкой безопасности. Обновите страницу.",
    "not_found": "Объект не найден.",
    "validation_error": "Запрос не соответствует формату.",
    "idempotency_conflict": "Ключ повторной отправки уже использован с другим запросом.",
    "run_not_active": "Задание уже завершено.",
    "rate_limited": "Слишком частые запросы. Повторите позже.",
    "quota_exceeded": "Лимит запросов демоверсии исчерпан.",
    "provider_unavailable": "Модели временно недоступны. Повторите запрос позже.",
    "provider_outcome_unknown": "Исход запроса к модели неизвестен. Ответ может быть неполным; отправьте запрос заново вручную.",
    "response_truncated": "Ответ модели обрезан на пределе длины. Уточните запрос или попросите продолжить.",
    "storage_unavailable": "Хранилище файлов временно недоступно.",
    "run_cancelled": "Запрос отменён.",
    "service_unavailable": "Сервис временно недоступен. Повторите позже.",
    "internal_error": "Внутренняя ошибка сервера.",
}
RETRYABLE = {"rate_limited", "provider_unavailable", "storage_unavailable", "service_unavailable"}
STATUS = {
    "unauthorized": 401,
    "invalid_demo_code": 401,
    "csrf_failed": 403,
    "not_found": 404,
    "validation_error": 422,
    "idempotency_conflict": 409,
    "run_not_active": 409,
    "rate_limited": 429,
    "quota_exceeded": 429,
    "storage_unavailable": 503,
    "service_unavailable": 503,
    "internal_error": 500,
}


def operation_id() -> str:
    return f"op_{uuid.uuid4().hex}"


def api_error(code: ErrorCode, message: str | None = None) -> ApiError:
    return ApiError(
        code=code,
        message=message or MESSAGES[code],
        retryable=code in RETRYABLE,
        operation_id=operation_id(),
    )


class AppError(Exception):
    def __init__(self, code: ErrorCode, message: str | None = None):
        super().__init__(code)
        self.code = code
        self.message = message


def _response(error: ApiError, status: int) -> JSONResponse:
    return JSONResponse(error.model_dump(), status_code=status)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _response(api_error(exc.code, exc.message), STATUS[exc.code])

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _response(api_error("validation_error"), 422)

    @app.exception_handler(HTTPException)
    async def handle_http(request: Request, exc: HTTPException) -> JSONResponse:
        code: ErrorCode = "not_found" if exc.status_code == 404 else "validation_error"
        return _response(api_error(code), exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        error = api_error("internal_error")
        log.error("operation %s failed: %s", error.operation_id, type(exc).__name__)
        return _response(error, 500)
