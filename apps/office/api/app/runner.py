import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings
from app.graph import GRAPH_STATUSES, Stopped, TaskGraph
from app.models_gateway import InvalidModelResponse, ModelsGateway, ProviderError, TruncatedModelResponse
from app.roles import ContextTooLarge
from app.sandbox import Sandbox
from app.schemas import SLOTS_TOTAL, TaskStatus
from app.tasks import (
    SIGNAL_DECISION_RECORDED,
    SIGNAL_SLOT_ACQUIRED,
    SIGNAL_SLOT_RELEASED,
    SIGNAL_TASK_CLOSED,
    BudgetExceeded,
    TaskStore,
)
from app.workspace import Workspace

logger = logging.getLogger("office.runner")

STOP_TIMEOUT = 120.0
SHUTDOWN_TIMEOUT = 30.0


def checkpoint_path(database_url: str) -> Path:
    path = Path(database_url.removeprefix("sqlite:///"))
    return path.with_name(f"{path.stem}.checkpoints.sqlite")


class Checkpoints:
    def __init__(self, database_url: str) -> None:
        self._sqlite: sqlite3.Connection | None = None
        self._pool: ConnectionPool | None = None
        if database_url.startswith("sqlite:///"):
            self._sqlite = sqlite3.connect(checkpoint_path(database_url), check_same_thread=False)
            self.saver: Any = SqliteSaver(self._sqlite)
        else:
            self._pool = ConnectionPool(
                database_url,
                min_size=1,
                max_size=4,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=True,
            )
            self.saver = PostgresSaver(self._pool)
        self.saver.setup()

    def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
        if self._pool is not None:
            self._pool.close()


@dataclass
class Run:
    cancel: threading.Event = field(default_factory=threading.Event)
    active: bool = True
    again: bool = False


def failure_reason(error: Exception) -> tuple[TaskStatus, str]:
    if isinstance(error, BudgetExceeded):
        return TaskStatus.needs_attention, "Исчерпан предел обращений к моделям для задачи."
    if isinstance(error, TruncatedModelResponse):
        return TaskStatus.failed, "Ответ модели обрезан пределом выходных токенов; действия из него не выполнялись."
    if isinstance(error, InvalidModelResponse):
        return TaskStatus.failed, "Ответ модели не соответствует схеме роли; действия из него не выполнялись."
    if isinstance(error, ContextTooLarge):
        return TaskStatus.failed, f"Контекст не помещается в окно модели: {error}"
    if isinstance(error, ProviderError):
        return TaskStatus.failed, "Модель недоступна: основной и резервный провайдеры вернули ошибку."
    return TaskStatus.failed, f"Ошибка выполнения графа: {type(error).__name__}."


class Runner:
    def __init__(
        self,
        settings: Settings,
        store: TaskStore,
        workspace: Workspace,
        sandbox: Sandbox,
        gateway: ModelsGateway,
        checkpoints: Checkpoints,
    ) -> None:
        self.settings = settings
        self.store = store
        self.workspace = workspace
        self.sandbox = sandbox
        self.gateway = gateway
        self.checkpoints = checkpoints
        self.fetch_options: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.runs: dict[str, Run] = {}
        self.stopping = False
        self.executor = ThreadPoolExecutor(max_workers=SLOTS_TOTAL, thread_name_prefix="graph")
        store.signal_handlers.append(self.on_signal)
        workspace.wait_stopped = self.wait_stopped

    def on_signal(self, name: str, task_id: str) -> None:
        if name == SIGNAL_SLOT_ACQUIRED:
            self.launch(task_id)
        elif name == SIGNAL_DECISION_RECORDED:
            if self.store.get_task(task_id).status == TaskStatus.developing:
                self.launch(task_id)
        elif name in (SIGNAL_SLOT_RELEASED, SIGNAL_TASK_CLOSED):
            self.stop(task_id)

    def launch(self, task_id: str) -> None:
        if self.store.get_task(task_id).status not in GRAPH_STATUSES:
            return
        with self.lock:
            if self.stopping:
                return
            run = self.runs.get(task_id)
            if run is not None and run.active:
                run.again = True
                return
            run = Run()
            self.runs[task_id] = run
        self.sandbox.reset_task(task_id)
        self.executor.submit(self._execute, task_id, run)

    def stop(self, task_id: str) -> None:
        with self.lock:
            run = self.runs.get(task_id)
            if run is not None:
                run.cancel.set()
        self.sandbox.cancel_task(task_id)

    def active(self, task_id: str) -> bool:
        with self.lock:
            run = self.runs.get(task_id)
            return run is not None and run.active

    def wait_stopped(self, task_id: str, timeout: float = STOP_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while self.active(task_id) or self.sandbox.has_containers(task_id):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def recover(self) -> None:
        self.sandbox.remove_stale()
        tasks = sorted(self.store.list_tasks(), key=lambda task: (task.created_at, task.id))
        for task in tasks:
            if task.status in GRAPH_STATUSES:
                self.launch(task.id)

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        with self.lock:
            self.stopping = True
            for run in self.runs.values():
                run.cancel.set()
        if self.on_signal in self.store.signal_handlers:
            self.store.signal_handlers.remove(self.on_signal)
        self.workspace.wait_stopped = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + timeout
        while any(self.active(task_id) for task_id in list(self.runs)):
            if time.monotonic() >= deadline:
                logger.warning("graph threads are still running at shutdown")
                return
            time.sleep(0.05)
        self.checkpoints.close()

    def _execute(self, task_id: str, run: Run) -> None:
        try:
            self._drive(task_id, run)
        except Exception:
            logger.exception("graph run failed: %s", task_id)
        finally:
            with self.lock:
                run.active = False
                again = run.again and not self.stopping and self.runs.get(task_id) is run
            if again:
                self.launch(task_id)

    def _drive(self, task_id: str, run: Run) -> None:
        while not run.cancel.is_set():
            task = self.store.get_task(task_id)
            if task.status not in GRAPH_STATUSES:
                return
            attempt = self.store.attempt(task_id)
            graph = TaskGraph(self, task_id, attempt, run.cancel).compile(self.checkpoints.saver)
            config = {"configurable": {"thread_id": f"{task_id}:{attempt}"}}
            snapshot = graph.get_state(config)
            if snapshot.interrupts:
                decision = self.store.spec_approval(task_id, snapshot.values.get("expected_version"))
                if decision is None:
                    return
                command: Any = Command(resume=decision)
            elif snapshot.created_at is None:
                command = {"task_id": task_id, "attempt": attempt}
            elif snapshot.next:
                command = None
            else:
                return
            self.workspace.ensure_worktree(task_id)
            try:
                graph.invoke(command, config, durability="sync")
            except Stopped:
                return
            except Exception as error:
                if run.cancel.is_set():
                    return
                logger.warning("graph of %s stopped with %s", task_id, type(error).__name__, exc_info=error)
                target, reason = failure_reason(error)
                self.store.halt(task_id, target, statuses=GRAPH_STATUSES, role=None, step="graph", reason=reason)
                return
