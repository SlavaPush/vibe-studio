import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.artifacts import artifact_response
from app.config import Settings
from app.db import Database
from app.decisions import decide_release, decide_task
from app.errors import ErrorCode, error_response, install_error_handlers, not_found
from app.events import EventNotifier, parse_last_event_id, stream_events
from app.intake import UNAVAILABLE, IntakeFilter
from app.models_gateway import ModelsGateway
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
    store = TaskStore(db, notifier.notify)
    sessions = SessionStore(db, settings)
    models = gateway or ModelsGateway(settings)
    intake = IntakeFilter(models, settings)
    store.clear_reservations()
    store.promote_queue()
    office_workspace = Workspace(settings, store) if workspace else None
    sandbox = Sandbox(settings, store, office_workspace) if office_workspace else None
    office_runner = None
    if runner and office_workspace is not None and sandbox is not None:
        office_runner = Runner(settings, store, office_workspace, sandbox, models, Checkpoints(settings.database_url))
    if office_workspace is not None:
        office_workspace.recover()
    if office_runner is not None:
        office_runner.recover()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if office_runner is not None:
            office_runner.shutdown()
        if office_workspace is not None:
            office_workspace.shutdown()
        db.close()

    app = FastAPI(title="back-office API", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.sessions = sessions
    app.state.notifier = notifier
    app.state.workspace = office_workspace
    app.state.sandbox = sandbox
    app.state.runner = office_runner
    install_error_handlers(app)
    app.add_middleware(OriginGuard, hosts=settings.allowed_hosts, origins=settings.allowed_origins)

    def current_participant(request: Request) -> Participant:
        participant = sessions.participant(request.cookies.get(COOKIE_NAME))
        if participant is None:
            raise unauthorized()
        return participant

    CurrentParticipant = Annotated[Participant, Depends(current_participant)]

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
                outcome = intake.run(body.text, body.profile)
            except Exception:
                logger.exception("intake filter crashed")
                outcome = IntakeOutcome(accepted=False, reason=UNAVAILABLE)
            task = store.create_task(participant.id, body.text, body.profile, outcome, reservation)
        except Exception:
            store.drop_reservation(reservation)
            raise
        if outcome.accepted:
            background.add_task(store.promote_queue)
        return task

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, _: CurrentParticipant) -> Task:
        return store.get_task(task_id)

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
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
        )
        return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)

    @app.get("/api/events")
    async def all_events(
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
        return decide_release(store, participant, release_id, body)

    @app.get("/api/slots")
    def slots(_: CurrentParticipant) -> Slots:
        return Slots(used=store.slots_used(), total=SLOTS_TOTAL)

    return app
