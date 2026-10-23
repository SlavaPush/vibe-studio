import html
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders

from app import auth, conversations, events, runs
from app.config import Settings, app_version, load_settings
from app.db import apply_schema, open_pool
from app.errors import api_error, install_error_handlers
from app.executor import Executor
from app.models_catalog import MODEL_CATALOG
from app.providers import Providers
from app.schemas import Health, ModelCatalog
from app.storage import Storage

ASSET_CACHE = "public, max-age=31536000, immutable"
VERSION_META = 'name="app-version" content="dev"'


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state
    state.pool = await open_pool(state.settings.database_url)
    await apply_schema(state.pool)
    if getattr(state, "providers", None) is None:
        state.providers = Providers(state.settings)
    owns_storage = getattr(state, "storage", None) is None
    if owns_storage:
        state.storage = Storage(state.settings)
    state.bus = events.EventBus()
    state.session_limiter = auth.SessionRateLimiter()
    state.executor = Executor(state.pool, state.settings, state.providers, state.storage, state.bus)
    await state.executor.start()
    try:
        yield
    finally:
        await state.executor.stop()
        if owns_storage:
            await state.storage.close()
        await state.pool.close()


class AssetFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = ASSET_CACHE
        return response


class SecurityHeaders:
    def __init__(self, app, headers: dict[str, str]) -> None:
        self.app = app
        self.headers = headers

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).update(self.headers)
            await send(message)

        await self.app(scope, receive, send_with_headers)


def security_headers(settings: Settings) -> dict[str, str]:
    storage = urlsplit(settings.supabase_url)
    storage_origin = f" {storage.scheme}://{storage.netloc}" if storage.scheme and storage.netloc else ""
    policy = "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            f"img-src 'self' data: blob:{storage_origin}",
            "connect-src 'self'",
            "font-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )
    return {
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }


def install_web(app: FastAPI, dist: Path) -> None:
    index = (dist / "index.html").read_text(encoding="utf-8")
    root_files = {path.name for path in dist.iterdir() if path.is_file() and not path.is_symlink()} - {"index.html"}
    app.mount("/assets", AssetFiles(directory=dist / "assets"), name="assets")

    @app.exception_handler(404)
    async def web_page(request: Request, exc: Exception) -> Response:
        path = request.url.path
        if request.method not in ("GET", "HEAD") or path == "/api" or path.startswith(("/api/", "/assets/")):
            return JSONResponse(api_error("not_found").model_dump(), status_code=404)
        name = path.removeprefix("/")
        if name in root_files:
            return FileResponse(dist / name)
        page = index.replace(VERSION_META, f'name="app-version" content="{html.escape(app_version())}"')
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="vibe-chat API", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    app.add_middleware(SecurityHeaders, headers=security_headers(app.state.settings))
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(runs.router)
    app.include_router(events.router)

    @app.get("/api/version")
    def version(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"version": app_version()}

    @app.get("/api/models")
    def models() -> ModelCatalog:
        return MODEL_CATALOG

    @app.get("/healthz")
    async def healthz(request: Request) -> Health:
        state = request.app.state
        executor = getattr(state, "executor", None)
        try:
            async with state.pool.connection() as conn:
                await conn.execute("SELECT 1 FROM quota_locks LIMIT 1")
            ready = executor is not None and executor.running
        except Exception:
            ready = False
        if not ready:
            return JSONResponse(Health(status="unavailable").model_dump(), status_code=503)
        return Health(status="ok")

    if app.state.settings.web_dist_dir:
        install_web(app, Path(app.state.settings.web_dist_dir))

    return app


app = create_app()
