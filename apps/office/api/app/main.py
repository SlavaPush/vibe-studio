import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.access_codes import AccessCodes
from app.artifacts import artifact_response
from app.config import Settings
from app.db import Database
from app.decisions import decide_release, decide_task
from app.errors import ErrorCode, OfficeError, error_response, install_error_handlers, not_found
from app.events import EventNotifier, parse_last_event_id, stream_events
from app.github import github_client
from app.intake import UNAVAILABLE, IntakeFilter
from app.models_gateway import ModelsGateway
from app.release import ReleaseWorker
from app.render import render_client
from app.required_checks import load_required_checks
from app.runner import Checkpoints, Runner
from app.sandbox import Sandbox
from app.schemas import (
    SLOTS_TOTAL,
    CheckReport,
    Decision,
    OfficeSessionRequest,
    Participant,
    ReleaseCandidate,
    ReleaseDecisionRequest,
    ReleaseQueueState,
    Slots,
    Task,
    TaskCreate,
    TaskDecisionRequest,
)
from app.sessions import COOKIE_NAME, SessionStore, unauthorized
from app.tasks import IntakeOutcome, TaskStore
from app.workspace import Workspace

logger = logging.getLogger("office")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
ASSET_CACHE = "public, max-age=31536000, immutable"


class OriginGuard:
    def __init__(self, app: ASGIApp, hosts: tuple[str, ...], origins: tuple[str, ...]) -> None:
        self.app = app
        self.hosts = {host.lower() for host in hosts}
        self.origins = {origin.lower() for origin in origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            host = headers.get("host", "").lower()
            origin = headers.get("origin", "").lower()
            if host not in self.hosts or (
                scope["method"] in UNSAFE_METHODS and origin not in self.origins
            ):
                response = error_response(
                    403, ErrorCode.forbidden_origin, "Запрос с недопустимого адреса отклонён."
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class AssetFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = ASSET_CACHE
        return response


def install_web(app: FastAPI, dist: Path) -> None:
    index = (dist / "index.html").read_text(encoding="utf-8")
    root_files = {path.name for path in dist.iterdir() if path.is_file() and not path.is_symlink()} - {"index.html"}
    app.mount("/assets", AssetFiles(directory=dist / "assets"), name="assets")

    @app.exception_handler(404)
    async def web_page(request: Request, _: Exception) -> Response:
        path = request.url.path
        if request.method not in ("GET", "HEAD") or path == "/api" or path.startswith(("/api/", "/assets/")):
            return error_response(404, ErrorCode.not_found, "Объект не найден.")
        name = path.removeprefix("/")
        if name in root_files:
            return FileResponse(dist / name)
        return HTMLResponse(index, headers={"Cache-Control": "no-cache"})


def create_app(
    settings: Settings | None = None,
    *,
    gateway: ModelsGateway | None = None,
    workspace: bool = True,
    runner: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if workspace and not settings.target_repo_url:
        raise ValueError("TARGET_REPO_URL не задан: офису нужен репозиторий для рабочих копий задач.")
    db = Database(settings.database_url)
    db.init_schema()
    notifier = EventNotifier()
    store = TaskStore(db, notifier.notify, settings.pr_review_calls, settings.rework_limit)
    sessions = SessionStore(db, settings, AccessCodes(settings))
    models = gateway or ModelsGateway(settings)
    intake = IntakeFilter(models, settings)
    store.clear_reservations()
    store.promote_queue()
    office_workspace = Workspace(settings, store) if workspace else None
    sandbox = Sandbox(settings, store, office_workspace) if office_workspace else None
    office_runner = None
    if runner and office_workspace is not None and sandbox is not None:
        office_runner = Runner(settings, store, office_workspace, sandbox, models, Checkpoints(settings.database_url))
    office_release = None
    if office_runner is not None and office_workspace is not None and sandbox is not None:
        if settings.github_app_configured:
            github = github_client(
                settings.github_app_id,
                settings.github_app_installation_id,
                settings.github_app_private_key_path,
                settings.github_repo,
                base_url=settings.github_api_url,
            )
            render = None
            if settings.render_configured:
                render = render_client(
                    settings.render_api_key, settings.render_service_id, base_url=settings.render_api_url
                )
            else:
                logger.warning(
                    "Render не настроен (RENDER_API_KEY, RENDER_SERVICE_ID): кандидаты выпуска ждут в очереди."
                )
            office_release = ReleaseWorker(
                settings, store, office_workspace, github, load_required_checks(settings.checkout_root),
                gateway=models, suite=sandbox, render=render,
            )
            office_runner.materials.github = github
            office_runner.materials.render = render
        else:
            logger.warning(
                "GitHub App не настроен (GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY_PATH): "
                "PR задач не создаются, задачи ждут в awaiting_integration."
            )
    if office_workspace is not None:
        office_workspace.recover()
    if office_runner is not None:
        office_runner.recover()
    if office_release is not None and office_workspace is not None:
        office_workspace.submit(office_release.start)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if office_release is not None:
            office_release.shutdown()
        if office_runner is not None:
            office_runner.shutdown()
        if office_workspace is not None:
            office_workspace.shutdown()
        sessions.codes.close()
        db.close()

    app = FastAPI(title="back-office API", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.sessions = sessions
    app.state.notifier = notifier
    app.state.workspace = office_workspace
    app.state.sandbox = sandbox
    app.state.runner = office_runner
    app.state.release = office_release
    install_error_handlers(app)
    app.add_middleware(OriginGuard, hosts=settings.allowed_hosts, origins=settings.allowed_origins)

    def current_participant(request: Request) -> Participant:
        participant = sessions.participant(request.cookies.get(COOKIE_NAME))
        if participant is None:
            raise unauthorized()
        return participant

    CurrentParticipant = Annotated[Participant, Depends(current_participant)]

    def session_check(request: Request) -> Callable[[], bool]:
        token = request.cookies.get(COOKIE_NAME)

        def active() -> bool:
            try:
                return sessions.participant(token) is not None
            except OfficeError:
                return False

        return active

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/office-session")
    def create_session(body: OfficeSessionRequest, request: Request, response: Response) -> Participant:
        client = request.client.host if request.client else "unknown"
        participant, token = sessions.login(body, client, request.cookies.get(COOKIE_NAME))
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.session_ttl_hours * 3600,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return participant

    @app.delete("/api/office-session", status_code=204)
    def delete_session(request: Request) -> Response:
        sessions.logout(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.get("/api/me")
    def me(participant: CurrentParticipant) -> Participant:
        return participant

    @app.get("/api/tasks")
    def list_tasks(_: CurrentParticipant) -> list[Task]:
        return store.list_tasks()

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskCreate, participant: CurrentParticipant, background: BackgroundTasks) -> Task:
        reservation = store.reserve_intake(participant.id, settings.tasks_per_hour)
        try:
            try:
                outcome = intake.run(body.text)
            except Exception:
                logger.exception("intake filter crashed")
                outcome = IntakeOutcome(accepted=False, reason=UNAVAILABLE)
            task = store.create_task(participant.id, body.text, outcome, reservation)
        except Exception:
            store.drop_reservation(reservation)
            raise
        try:
            intake.attach(outcome, store, task.id)
        except Exception:
            logger.exception("intake usage is not recorded")
        if outcome.accepted:
            background.add_task(store.promote_queue)
        return task

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, _: CurrentParticipant) -> Task:
        return store.get_task(task_id)

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        request: Request,
        _: CurrentParticipant,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        await asyncio.to_thread(store.get_task, task_id)
        after = parse_last_event_id(last_event_id) or 0
        stream = stream_events(
            store,
            notifier,
            task_id=task_id,
            after=after,
            ping_seconds=settings.sse_ping_seconds,
            poll_seconds=settings.sse_poll_seconds,
            session_active=session_check(request),
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/events")
    async def all_events(
        request: Request,
        _: CurrentParticipant,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        after = parse_last_event_id(last_event_id)
        if after is None:
            after = await asyncio.to_thread(store.last_event_id)
        stream = stream_events(
            store,
            notifier,
            task_id=None,
            after=after,
            ping_seconds=settings.sse_ping_seconds,
            poll_seconds=settings.sse_poll_seconds,
            session_active=session_check(request),
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/tasks/{task_id}/diff")
    def task_diff(task_id: str, _: CurrentParticipant) -> Response:
        task = store.get_task(task_id)
        if office_workspace is None:
            raise not_found("Рабочая копия задачи не найдена.")
        return Response(office_workspace.diff(task), media_type="text/plain; charset=utf-8")

    @app.get("/api/tasks/{task_id}/checks")
    def task_checks(task_id: str, _: CurrentParticipant) -> CheckReport:
        return store.latest_check_report(store.get_task(task_id))

    @app.get("/api/tasks/{task_id}/artifacts/{name:path}")
    def task_artifact(task_id: str, name: str, _: CurrentParticipant) -> FileResponse:
        store.get_task(task_id)
        return artifact_response(settings.artifacts_dir, task_id, name)

    @app.post("/api/tasks/{task_id}/decisions")
    def task_decision(task_id: str, body: TaskDecisionRequest, participant: CurrentParticipant) -> Decision:
        return decide_task(store, participant, task_id, body)

    @app.get("/api/releases")
    def list_releases(_: CurrentParticipant) -> list[ReleaseCandidate]:
        return store.list_releases()

    @app.post("/api/releases/{release_id}/decisions")
    def release_decision(
        release_id: str, body: ReleaseDecisionRequest, participant: CurrentParticipant
    ) -> Decision:
        if office_release is not None:
            return office_release.decide(participant, release_id, body)
        return decide_release(store, participant, release_id, body)

    @app.get("/api/release-queue")
    def release_queue(_: CurrentParticipant) -> ReleaseQueueState:
        with db.connection() as conn:
            state = store.queue_state(conn)
            blocked = store.load_candidate(conn, state["release_blocked_by"]) if state["release_blocked_by"] else None
        return ReleaseQueueState(
            configured=office_release is not None and office_release.queue.render is not None,
            active_release_id=state["active_release_id"],
            release_blocked_by=state["release_blocked_by"],
            blocked_reason=blocked.blocked_reason if blocked else None,
            published_sha=state["published_sha"],
        )

    @app.get("/api/slots")
    def slots(_: CurrentParticipant) -> Slots:
        return Slots(used=store.slots_used(), total=SLOTS_TOTAL)

    if settings.web_dist_dir is not None:
        install_web(app, settings.web_dist_dir)

    return app
