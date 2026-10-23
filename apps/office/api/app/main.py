from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import fixtures
from app.schemas import (
    SLOTS_TOTAL,
    ApiError,
    Participant,
    ReleaseCandidate,
    Slots,
    Task,
    count_slots,
)

app = FastAPI(title="back-office API")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me")
def me() -> Participant:
    return fixtures.ADMIN


@app.get("/api/tasks")
def list_tasks() -> list[Task]:
    return fixtures.TASKS


@app.get("/api/tasks/{task_id}", response_model=Task, responses={404: {"model": ApiError}})
def get_task(task_id: str) -> Task | JSONResponse:
    for task in fixtures.TASKS:
        if task.id == task_id:
            return task
    error = ApiError(
        code="not_found",
        message="Задача не найдена.",
        retryable=False,
        operation_id=f"op_{uuid4().hex[:8]}",
    )
    return JSONResponse(status_code=404, content=error.model_dump())


@app.get("/api/slots")
def slots() -> Slots:
    return Slots(used=count_slots(fixtures.TASKS), total=SLOTS_TOTAL)


@app.get("/api/releases")
def list_releases() -> list[ReleaseCandidate]:
    return fixtures.RELEASES
