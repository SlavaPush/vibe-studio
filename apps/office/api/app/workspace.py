import io
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings
from app.errors import ErrorCode, OfficeError, not_found
from app.paths import PathDenied, PathPolicy
from app.schemas import SLOT_STATUSES, Task, TaskEventType
from app.tasks import CLOSED_STATUSES, SIGNAL_SLOT_ACQUIRED, SIGNAL_TASK_CLOSED, TaskStore

logger = logging.getLogger("office.workspace")

GIT_TIMEOUT = 60
NETWORK_TIMEOUT = 300
DIFF_LIMIT = 1024 * 1024
QA_DIR = "web/e2e/office"
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "back-office",
    "GIT_AUTHOR_EMAIL": "office@localhost",
    "GIT_COMMITTER_NAME": "back-office",
    "GIT_COMMITTER_EMAIL": "office@localhost",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": os.devnull,
}
DENIED_MODES = {
    "100755": "Исполняемый файл запрещён.",
    "120000": "Symlink запрещён.",
    "160000": "Submodule запрещён.",
}


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class RawChange:
    new_mode: str
    new_sha: str
    path: str


def branch_name(task_id: str) -> str:
    return f"office/{task_id}"


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env.update(extra or {})
    return env


def run_git(
    *args: str, cwd: Path, env: dict[str, str] | None = None, timeout: float = GIT_TIMEOUT
) -> bytes:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise WorkspaceError(f"git {args[0]}: превышено время ожидания.") from error
    except OSError as error:
        raise WorkspaceError(f"git {args[0]}: не удалось запустить git.") from error
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.decode("utf-8", "replace").splitlines() if line.strip()]
        detail = lines[-1][:200] if lines else ""
        raise WorkspaceError(f"git {args[0]} завершился с кодом {proc.returncode}. {detail}".strip())
    return proc.stdout


def parse_raw(output: bytes) -> list[RawChange]:
    fields = output.decode("utf-8", "surrogateescape").split("\0")
    changes = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        if not meta:
            continue
        _, new_mode, _, new_sha, _ = meta.lstrip(":").split(" ")
        changes.append(RawChange(new_mode=new_mode, new_sha=new_sha, path=path))
    return changes


class Workspace:
    def __init__(self, settings: Settings, store: TaskStore) -> None:
        self.store = store
        self.url = settings.target_repo_url
        self.root = settings.workspace_dir.resolve()
        self.clone = self.root / "vibe-chat"
        self.trees = self.root / "wt"
        self.paths = PathPolicy(settings.paths_policy)
        self.lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace")
        self._pending = 0
        self._idle = threading.Condition()
        store.signal_handlers.append(self.on_signal)

    def submit(self, job: Callable[..., None], *args: str) -> None:
        with self._idle:
            self._pending += 1

        def run() -> None:
            try:
                job(*args)
            except Exception:
                logger.exception("workspace job failed: %s %s", job.__name__, args)
            finally:
                with self._idle:
                    self._pending -= 1
                    self._idle.notify_all()

        self._executor.submit(run)

    def wait_idle(self, timeout: float = 120) -> None:
        with self._idle:
            if not self._idle.wait_for(lambda: self._pending == 0, timeout):
                raise TimeoutError("workspace jobs are still running")

    def shutdown(self) -> None:
        if self.on_signal in self.store.signal_handlers:
            self.store.signal_handlers.remove(self.on_signal)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def on_signal(self, name: str, task_id: str) -> None:
        if name == SIGNAL_SLOT_ACQUIRED:
            self.submit(self.ensure_worktree, task_id)
        elif name == SIGNAL_TASK_CLOSED:
            self.submit(self.remove_worktree, task_id)

    def recover(self) -> None:
        self.submit(self._recover)

    def _recover(self) -> None:
        tasks = {task.id: task for task in self.store.list_tasks()}
        if self.trees.is_dir():
            for entry in sorted(self.trees.iterdir()):
                task = tasks.get(entry.name)
                if task is not None and task.status in CLOSED_STATUSES:
                    try:
                        self.remove_worktree(task.id)
                    except WorkspaceError:
                        logger.exception("cannot remove worktree of %s", task.id)
        for task in sorted(tasks.values(), key=lambda item: (item.created_at, item.id)):
            if task.status in SLOT_STATUSES:
                self.ensure_worktree(task.id)

    def git(self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None,
            timeout: float = GIT_TIMEOUT) -> str:
        return run_git(*args, cwd=cwd or self.clone, env=env, timeout=timeout).decode("utf-8").strip()

    def worktree_path(self, task_id: str) -> Path:
        return self.trees / task_id

    def is_task_worktree(self, path: Path, task_id: str) -> bool:
        if path.is_symlink() or not path.is_dir():
            return False
        try:
            top = self.git("rev-parse", "--show-toplevel", cwd=path)
            common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
            head = self.git("symbolic-ref", "-q", "HEAD", cwd=path)
        except WorkspaceError:
            return False
        return (
            Path(top).resolve() == path.resolve()
            and Path(common).resolve() == (self.clone / ".git").resolve()
            and head == f"refs/heads/{branch_name(task_id)}"
        )

    def _ensure_clone(self) -> None:
        if (self.clone / ".git").is_dir():
            return
        if os.path.lexists(self.clone):
            raise WorkspaceError("Каталог клона существует, но не является репозиторием git.")
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self.root, prefix=".clone-"))
        try:
            run_git("clone", "--no-checkout", "--quiet", self.url, str(staging / "repo"),
                    cwd=self.root, timeout=NETWORK_TIMEOUT)
            os.rename(staging / "repo", self.clone)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _branch_exists(self, branch: str) -> bool:
        try:
            self.git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        except WorkspaceError:
            return False
        return True

    def _prepare(self, task: Task) -> tuple[bool, str]:
        branch = branch_name(task.id)
        path = self.worktree_path(task.id)
        self._ensure_clone()
        self.git("worktree", "prune")
        if os.path.lexists(path):
            if not self.is_task_worktree(path, task.id):
                raise WorkspaceError("Каталог рабочей копии занят и не является worktree задачи.")
            created = False
        elif self._branch_exists(branch):
            self.trees.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "--quiet", str(path), branch)
            created = True
        else:
            self.git("fetch", "--quiet", "origin", timeout=NETWORK_TIMEOUT)
            base = self.git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
            self.trees.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "--quiet", "-b", branch, str(path), base)
            return True, base
        if task.base_sha:
            return created, task.base_sha
        return created, self.git("merge-base", branch, "refs/remotes/origin/main")

    def ensure_worktree(self, task_id: str) -> None:
        try:
            task = self.store.get_task(task_id)
        except OfficeError:
            return
        if task.status not in SLOT_STATUSES:
            return
        try:
            with self.lock:
                created, base_sha = self._prepare(task)
        except WorkspaceError as error:
            self.store.fail_task(task_id, tool="git_worktree", arguments=branch_name(task_id), reason=str(error))
            return
        except OSError as error:
            reason = f"Файловая операция рабочей области не выполнена: {error.strerror or type(error).__name__}."
            self.store.fail_task(task_id, tool="git_worktree", arguments=branch_name(task_id), reason=reason)
            return
        if not self.store.record_worktree(task_id, branch_name(task_id), base_sha, created):
            self.remove_worktree(task_id)

    def remove_worktree(self, task_id: str) -> None:
        path = self.worktree_path(task_id)
        with self.lock:
            if not (self.clone / ".git").is_dir():
                return
            if os.path.lexists(path):
                if not self.is_task_worktree(path, task_id):
                    logger.warning("not removing foreign directory %s", path)
                    return
                self.git("worktree", "remove", "--force", str(path))
            self.git("worktree", "prune")

    def task_worktree(self, task_id: str) -> Path:
        path = self.worktree_path(task_id)
        if not self.is_task_worktree(path, task_id):
            raise WorkspaceError("Рабочая копия задачи не найдена.")
        return path

    def _index_copy(self, path: Path, directory: Path) -> tuple[Path, Path]:
        real = path / self.git("rev-parse", "--git-path", "index", cwd=path)
        copy = directory / "index"
        if real.exists():
            shutil.copyfile(real, copy)
        return real, copy

    def _check_change(self, path: Path, change: RawChange, env: dict[str, str]) -> None:
        if change.new_mode == "000000":
            self.paths.check_any_writer(change.path)
            return
        if change.new_mode in DENIED_MODES:
            raise PathDenied(DENIED_MODES[change.new_mode])
        if change.new_mode != "100644":
            raise PathDenied("Недопустимый тип файла.")
        size = int(self.git("cat-file", "-s", change.new_sha, cwd=path, env=env))
        self.paths.check_any_writer(change.path, size)

    def commit(self, task_id: str, message: str) -> str | None:
        task = self.store.get_task(task_id)
        if not task.base_sha:
            raise WorkspaceError("base_sha задачи не записан.")
        with self.lock, tempfile.TemporaryDirectory(dir=self.root) as tmp:
            path = self.task_worktree(task_id)
            real, index = self._index_copy(path, Path(tmp))
            env = {"GIT_INDEX_FILE": str(index)}
            self.git("add", "-A", cwd=path, env=env)
            raw = run_git("diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", "HEAD", cwd=path, env=env)
            changes = parse_raw(raw)
            if not changes:
                return None
            for change in changes:
                try:
                    self._check_change(path, change, env)
                except PathDenied as error:
                    self.store.add_event(
                        task_id,
                        TaskEventType.tool_called,
                        {"arguments": change.path[:200], "result": "denied", "reason": str(error)},
                        tool="git_commit",
                    )
                    raise
            self.git("commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", message,
                     cwd=path, env=env | COMMIT_ENV)
            os.replace(index, real)
            sha = self.git("rev-parse", "HEAD", cwd=path)
            names = run_git("diff", "--name-only", "-z", task.base_sha, sha, cwd=path)
        changed = [name for name in names.decode("utf-8").split("\0") if name]
        self.store.record_commit(task_id, sha, changed)
        return sha

    def diff(self, task: Task) -> bytes:
        path = self.worktree_path(task.id)
        if not task.base_sha or not self.is_task_worktree(path, task.id):
            raise not_found("Рабочая копия задачи не найдена.")
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            _, index = self._index_copy(path, Path(tmp))
            env = {"GIT_INDEX_FILE": str(index)}
            self.git("add", "-A", cwd=path, env=env)
            output = run_git(
                "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", task.base_sha,
                cwd=path, env=env,
            )
        if len(output) > DIFF_LIMIT:
            raise OfficeError(
                413, ErrorCode.diff_too_large, "Diff больше 1 МиБ. Список файлов доступен в карточке задачи."
            )
        return output

    def resolve(self, sha: str) -> str:
        return self.git("rev-parse", "--verify", f"{sha}^{{commit}}")

    def tracked_files(self, path: Path) -> list[str]:
        output = run_git("ls-files", "-z", cwd=path)
        return [name for name in output.decode("utf-8").split("\0") if name]

    @contextmanager
    def checkout(self, sha: str) -> Iterator[tuple[Path, str]]:
        with self.lock:
            full = self.resolve(sha)
            holder = Path(tempfile.mkdtemp(dir=self.root, prefix=".suite-"))
            path = holder / "app"
            try:
                self.git("worktree", "add", "--quiet", "--detach", str(path), full)
            except WorkspaceError:
                shutil.rmtree(holder, ignore_errors=True)
                raise
        try:
            yield path, full
        finally:
            with self.lock:
                try:
                    self.git("worktree", "remove", "--force", str(path))
                finally:
                    shutil.rmtree(holder, ignore_errors=True)
                    self.git("worktree", "prune")

    def export_qa(self, sha: str, destination: Path) -> list[str]:
        listed = run_git("ls-tree", "-r", "--name-only", "-z", sha, "--", QA_DIR, cwd=self.clone)
        files = [name for name in listed.decode("utf-8").split("\0") if name]
        if not files:
            return []
        archive = run_git("archive", "--format=tar", sha, "--", QA_DIR, cwd=self.clone)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(destination, filter="data")
        return files
