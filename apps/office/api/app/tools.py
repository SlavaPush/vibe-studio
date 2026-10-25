import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.paths import PathDenied, resolve_inside
from app.schemas import AgentRole, TaskEventType
from app.tasks import TaskStore
from app.workspace import Workspace


class ToolError(Exception):
    pass


@dataclass(frozen=True)
class ToolOutcome:
    result: str
    output: str


class WorkspaceTools:
    def __init__(self, store: TaskStore, workspace: Workspace, task_id: str, role: AgentRole) -> None:
        self.store = store
        self.workspace = workspace
        self.task_id = task_id
        self.role = role
        self.policy = workspace.paths
        self.change_class = store.get_task(task_id).change_class

    def _root(self) -> Path:
        root = self.workspace.worktree_path(self.task_id)
        if root.is_symlink() or not root.is_dir():
            raise ToolError("Рабочая копия задачи не найдена.")
        return root

    def _call(self, tool: str, arguments: str, action: Callable[[], str]) -> ToolOutcome:
        try:
            outcome = ToolOutcome("ok", action())
        except PathDenied as error:
            outcome = ToolOutcome("denied", str(error))
        except ToolError as error:
            outcome = ToolOutcome("error", str(error))
        except OSError:
            outcome = ToolOutcome("error", "Файловая операция не выполнена.")
        payload = {"arguments": arguments, "result": outcome.result}
        if outcome.result != "ok":
            payload["reason"] = outcome.output
        self.store.add_event(self.task_id, TaskEventType.tool_called, payload, role=self.role, tool=tool)
        return outcome

    def list_dir(self, path: str = ".") -> ToolOutcome:
        def action() -> str:
            root = self._root()
            rel = "" if path == "." else self.policy.check_read(path)
            target = resolve_inside(root, rel) if rel else root
            if not target.is_dir():
                raise ToolError("Каталог не найден.")
            lines = []
            for entry in sorted(os.scandir(target), key=lambda item: item.name):
                child = f"{rel}/{entry.name}" if rel else entry.name
                if not self.policy.readable(child):
                    continue
                if entry.is_symlink():
                    lines.append(f"{entry.name} (symlink)")
                else:
                    lines.append(entry.name + ("/" if entry.is_dir() else ""))
            limit = self.policy.max_list_entries
            if len(lines) > limit:
                lines = lines[:limit] + [f"... ещё {len(lines) - limit} записей"]
            return "\n".join(lines)

        return self._call("list_dir", path[:200], action)

    def read_file(self, path: str) -> ToolOutcome:
        def action() -> str:
            target = resolve_inside(self._root(), self.policy.check_read(path))
            try:
                info = os.lstat(target)
            except FileNotFoundError as error:
                raise ToolError("Файл не найден.") from error
            if not stat.S_ISREG(info.st_mode):
                raise PathDenied("Читать можно только обычный файл.")
            if info.st_size > self.policy.max_file_bytes:
                raise PathDenied(f"Файл больше {self.policy.max_file_bytes} байт.")
            try:
                return target.read_bytes().decode("utf-8")
            except UnicodeDecodeError as error:
                raise PathDenied("Файл не является текстом UTF-8.") from error

        return self._call("read_file", path[:200], action)

    def write_file(self, path: str, content: str) -> ToolOutcome:
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError:
            data = None

        def action() -> str:
            if data is None or b"\0" in data:
                raise PathDenied("Содержимое должно быть текстом UTF-8.")
            target = resolve_inside(
                self._root(), self.policy.check_write(self.role, path, len(data), self.change_class)
            )
            if os.path.lexists(target) and not stat.S_ISREG(os.lstat(target).st_mode):
                raise PathDenied("Записывать можно только в обычный файл.")
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".office-")
            try:
                with os.fdopen(handle, "wb") as file:
                    file.write(data)
                os.chmod(temporary, 0o644)
                os.replace(temporary, target)
            except BaseException:
                if os.path.lexists(temporary):
                    os.unlink(temporary)
                raise
            return f"Записано {len(data)} байт."

        size = "?" if data is None else str(len(data))
        return self._call("write_file", f"{path[:200]} ({size} байт)", action)

    def delete_file(self, path: str) -> ToolOutcome:
        def action() -> str:
            target = resolve_inside(self._root(), self.policy.check_write(self.role, path, None, self.change_class))
            try:
                info = os.lstat(target)
            except FileNotFoundError as error:
                raise ToolError("Файл не найден.") from error
            if not stat.S_ISREG(info.st_mode):
                raise PathDenied("Удалять можно только обычный файл.")
            os.unlink(target)
            return "Файл удалён."

        return self._call("delete_file", path[:200], action)
