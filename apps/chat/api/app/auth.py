import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response

from app.db import fetch_one
from app.errors import AppError
from app.schemas import DemoSessionRequest, Session

COOKIE = "vc_session"
SESSION_DAYS = 7
FORWARDED_HEADERS = ("x-forwarded-for", "x-real-ip", "true-client-ip", "cf-connecting-ip", "forwarded")

SESSION_SQL = """
SELECT s.user_id, s.csrf_token, s.expires_at
FROM sessions s
JOIN users u ON u.id = s.user_id
JOIN access_codes c ON c.id = u.access_code_id
WHERE s.token_hash = %s AND s.expires_at > now() AND c.revoked_at IS NULL
"""

log = logging.getLogger("uvicorn.error")

router = APIRouter()


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    csrf_token: str
    expires_at: datetime
    source_hash: str


def forwarded_chain(request: Request) -> list[str]:
    return [item.strip() for value in request.headers.getlist("x-forwarded-for") for item in value.split(",")]


def client_ip(request: Request) -> str:
    settings = request.app.state.settings
    peer = request.client.host if request.client else "unknown"
    if not settings.trust_proxy_headers:
        return peer
    source = settings.client_ip_source
    chain = forwarded_chain(request)
    if source == "xff_from_end":
        candidate = chain[-settings.proxy_hops] if len(chain) >= settings.proxy_hops else ""
    elif source == "xff_first":
        candidate = chain[0] if chain else ""
    else:
        candidate = request.headers.get(source, "").strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return peer
    return candidate


def keyed_hash(request: Request, value: str) -> str:
    salt = request.app.state.settings.quota_salt.encode()
    return hmac.new(salt, value.encode(), hashlib.sha256).hexdigest()


def source_hash(request: Request) -> str:
    return keyed_hash(request, client_ip(request))


def log_forwarded_chain(request: Request, source: str) -> None:
    chain = forwarded_chain(request)
    present = [name for name in FORWARDED_HEADERS if name in request.headers]
    singles = {name: keyed_hash(request, request.headers[name])[:8] for name in present if name != "x-forwarded-for"}
    log.info(
        "forwarded chain: count=%d xff=%s headers=%s values=%s source=%s",
        len(chain),
        [keyed_hash(request, item)[:8] for item in chain],
        present,
        singles,
        source[:8],
    )


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def access_code_hash(code: str) -> str:
    normalized = "".join(code.split()).replace("-", "").lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


async def active_session(conn, token: str) -> dict | None:
    return await fetch_one(conn, SESSION_SQL, (token_hash(token),))


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin not in request.app.state.settings.allowed_origins:
        raise AppError("csrf_failed")


class SessionRateLimiter:
    def __init__(self) -> None:
        self.created: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, source: str, limit: int) -> bool:
        now = time.monotonic()
        window = self.created[source]
        while window and now - window[0] > 3600:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


async def current_user(request: Request) -> CurrentUser:
    token = request.cookies.get(COOKIE)
    if not token:
        raise AppError("unauthorized")
    async with request.app.state.pool.connection() as conn:
        row = await active_session(conn, token)
    if row is None:
        raise AppError("unauthorized")
    return CurrentUser(str(row["user_id"]), row["csrf_token"], row["expires_at"], source_hash(request))


async def mutating_user(request: Request, user: CurrentUser = Depends(current_user)) -> CurrentUser:
    check_origin(request)
    header = request.headers.get("x-csrf-token", "")
    if not hmac.compare_digest(header.encode(), user.csrf_token.encode()):
        raise AppError("csrf_failed")
    return user


@router.post("/api/demo-session", status_code=201)
async def create_session(body: DemoSessionRequest, request: Request, response: Response) -> Session:
    check_origin(request)
    settings = request.app.state.settings
    source = source_hash(request)
    if settings.log_forwarded_chain:
        log_forwarded_chain(request, source)
    if not request.app.state.session_limiter.allow(source, settings.sessions_per_hour):
        raise AppError("rate_limited")

    user_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_hex(16)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    async with request.app.state.pool.connection() as conn:
        code = await fetch_one(
            conn,
            """
            UPDATE access_codes SET used_at = now()
            WHERE code_hash = %s AND revoked_at IS NULL AND used_at IS NULL
            RETURNING id
            """,
            (access_code_hash(body.code),),
        )
        if code is None:
            raise AppError("invalid_demo_code")
        await conn.execute(
            "INSERT INTO users (id, source_hash, access_code_id) VALUES (%s, %s, %s)", (user_id, source, code["id"])
        )
        await conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at) VALUES (%s, %s, %s, %s)",
            (token_hash(token), user_id, csrf, expires),
        )
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return Session(user_id=user_id, expires_at=expires, csrf_token=csrf)


@router.get("/api/me")
async def me(user: CurrentUser = Depends(current_user)) -> Session:
    return Session(user_id=user.user_id, expires_at=user.expires_at, csrf_token=user.csrf_token)
