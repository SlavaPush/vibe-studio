import hashlib
import hmac
import ipaddress
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

router = APIRouter()


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    csrf_token: str
    expires_at: datetime
    source_hash: str


def client_ip(request: Request) -> str:
    settings = request.app.state.settings
    peer = request.client.host if request.client else "unknown"
    if not settings.trust_proxy_headers:
        return peer
    chain = [item.strip() for value in request.headers.getlist("x-forwarded-for") for item in value.split(",")]
    if len(chain) < settings.proxy_hops:
        return peer
    candidate = chain[-settings.proxy_hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return peer
    return candidate


def source_hash(request: Request) -> str:
    salt = request.app.state.settings.quota_salt.encode()
    return hmac.new(salt, client_ip(request).encode(), hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
        row = await fetch_one(
            conn,
            "SELECT user_id, csrf_token, expires_at FROM sessions WHERE token_hash = %s AND expires_at > now()",
            (token_hash(token),),
        )
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
    if not request.app.state.session_limiter.allow(source, settings.sessions_per_hour):
        raise AppError("rate_limited")
    expected = settings.demo_access_code
    if not expected or not hmac.compare_digest(body.code.encode(), expected.encode()):
        raise AppError("invalid_demo_code")

    user_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_hex(16)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    async with request.app.state.pool.connection() as conn:
        await conn.execute("INSERT INTO users (id, source_hash) VALUES (%s, %s)", (user_id, source))
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
