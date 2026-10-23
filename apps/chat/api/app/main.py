import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import auth, conversations, events, runs
from app.config import Settings, load_settings
from app.db import apply_schema, open_pool
from app.errors import install_error_handlers
from app.executor import Executor
from app.models_catalog import MODEL_CATALOG
from app.providers import Providers
from app.schemas import Health, ModelCatalog
from app.storage import Storage


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


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="vibe-chat API", lifespan=lifespan)
    app.state.settings = settings or load_settings()
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(conversations.router)
    app.include_router(runs.router)
    app.include_router(events.router)

    @app.get("/api/version")
    def version(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"version": os.getenv("APP_VERSION", "dev")}

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

    return app


app = create_app()
