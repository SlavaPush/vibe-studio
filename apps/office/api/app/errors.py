import logging
from enum import StrEnum
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas import ApiError

logger = logging.getLogger("office")


class ErrorCode(StrEnum):
    invalid_artifact_name = "invalid_artifact_name"
    unauthorized = "unauthorized"
    invalid_access_code = "invalid_access_code"
    forbidden_not_owner = "forbidden_not_owner"
    forbidden_role = "forbidden_role"
    forbidden_origin = "forbidden_origin"
    not_found = "not_found"
    version_mismatch = "version_mismatch"
    decision_not_allowed = "decision_not_allowed"
    idempotency_conflict = "idempotency_conflict"
    diff_too_large = "diff_too_large"
    validation_error = "validation_error"
    rate_limited = "rate_limited"
    internal_error = "internal_error"


class OfficeError(Exception):
    def __init__(
        self,
        status: int,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.headers = headers or {}


def operation_id() -> str:
    return f"op_{uuid4().hex[:8]}"


def error_response(
    status: int,
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    headers: dict[str, str] | None = None,
    op_id: str | None = None,
) -> JSONResponse:
    body = ApiError(
        code=code.value,
        message=message,
        retryable=retryable,
        operation_id=op_id or operation_id(),
    )
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


def not_found(message: str = "Объект не найден.") -> OfficeError:
    return OfficeError(404, ErrorCode.not_found, message)


def rate_limited(message: str, retry_after: int) -> OfficeError:
    return OfficeError(
        429,
        ErrorCode.rate_limited,
        message,
        retryable=True,
        headers={"Retry-After": str(max(1, retry_after))},
    )


TASK_TEXT_FIELDS = {("body", "text")}


def validation_message(error: RequestValidationError) -> str:
    for item in error.errors():
        if tuple(item.get("loc", ()))[:2] in TASK_TEXT_FIELDS and item.get("type") in {
            "string_too_short",
            "string_too_long",
        }:
            return "Текст задачи должен содержать от 20 до 4000 символов."
    return "Тело запроса не соответствует схеме."


HTTP_CODES = {
    401: ErrorCode.unauthorized,
    404: ErrorCode.not_found,
    405: ErrorCode.not_found,
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OfficeError)
    async def office_error(_: Request, exc: OfficeError) -> JSONResponse:
        return error_response(
            exc.status, exc.code, exc.message, retryable=exc.retryable, headers=exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, ErrorCode.validation_error, validation_message(exc))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code >= 500:
            return error_response(exc.status_code, ErrorCode.internal_error, "Внутренняя ошибка.")
        code = HTTP_CODES.get(exc.status_code, ErrorCode.validation_error)
        message = "Объект не найден." if code is ErrorCode.not_found else "Запрос отклонён."
        return error_response(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception) -> JSONResponse:
        op_id = operation_id()
        logger.exception("internal error %s", op_id, exc_info=exc)
        return error_response(
            500,
            ErrorCode.internal_error,
            "Внутренняя ошибка. Проверьте состояние объекта перед повтором.",
            op_id=op_id,
        )
