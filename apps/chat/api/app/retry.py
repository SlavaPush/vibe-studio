import random

REFUSED = "refused"
REJECTED = "rejected"
UNKNOWN = "unknown"
INVALID = "invalid"

REFUSED_STATUSES = {429, 529}
REJECTED_STATUSES = {400, 401, 403, 404, 413, 422}
CONNECT_ERRORS = {"ConnectError", "ConnectTimeout"}
MAX_RETRIES = 2
BASE_DELAY = 1.0
MAX_DELAY = 20.0


class InvalidResponse(Exception):
    pass


class StreamInterrupted(Exception):
    pass


def http_status(error: BaseException) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def classify(error: BaseException) -> str:
    if isinstance(error, InvalidResponse):
        return INVALID
    if isinstance(error, StreamInterrupted):
        return UNKNOWN
    status = http_status(error)
    if status in REFUSED_STATUSES:
        return REFUSED
    if status in REJECTED_STATUSES:
        return REJECTED
    if status is not None:
        return UNKNOWN
    cause = error.__cause__
    while cause is not None:
        if type(cause).__name__ in CONNECT_ERRORS:
            return REFUSED
        cause = cause.__cause__
    return UNKNOWN


def retry_after(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def retry_delay(error: BaseException, retry: int) -> float:
    base = retry_after(error)
    if base is None:
        base = BASE_DELAY * 2**retry
    return min(base, MAX_DELAY) + random.uniform(0, BASE_DELAY / 2)
