import base64
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
from contextlib import contextmanager, suppress
from enum import StrEnum
from pathlib import Path

from app.branch_check import (
    GIT_TIMEOUT,
    RawChange,
    WorkspaceError,
    check_change,
    check_range,
    git_env,
    parse_raw,
    run_git,
)
from app.config import APP_DIR, Settings
from app.errors import ErrorCode, OfficeError, not_found
from app.paths import PathDenied, PathPolicy
from app.schemas import SLOT_STATUSES, WORKTREE_STATUSES, AgentRole, Task, TaskEventType, TaskStatus
from app.tasks import CLOSED_STATUSES, SIGNAL_SLOT_ACQUIRED, SIGNAL_TASK_CLOSED, TaskStore

logger = logging.getLogger("office.workspace")

NETWORK_TIMEOUT = 300
DIFF_LIMIT = 1024 * 1024
CLONE_NAME = "vibe-studio"
QA_DIR = f"{APP_DIR}/web/e2e/office"
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "back-office",
    "GIT_AUTHOR_EMAIL": "office@localhost",
    "GIT_COMMITTER_NAME": "back-office",
    "GIT_COMMITTER_EMAIL": "office@localhost",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.hooksPath",
    "GIT_CONFIG_VALUE_0": os.devnull,
}
PUSH_HEADER_KEY = "http.https://github.com/.extraheader"


class SparseUnsafe(WorkspaceError):
    pass


class PushResult(StrEnum):
    ok = "ok"
    rejected = "rejected"
    error = "error"


def branch_name(task_id: str) -> str:
    return f"office/{task_id}"


def push_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    env = git_env(
        {
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": PUSH_HEADER_KEY,
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": os.devnull,
            "GIT_CONFIG_KEY_2": "credential.helper",
            "GIT_CONFIG_VALUE_2": "",
            "GIT_CONFIG_KEY_3": "core.askPass",
            "GIT_CONFIG_VALUE_3": "",
        }
    )
    for name in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_PARAMETERS"):
        env.pop(name, None)
    return env


class Workspace:
    def __init__(self, settings: Settings, store: TaskStore) -> None:
        self.store = store
        self.url = settings.target_repo_url
        self.root = settings.workspace_dir.resolve()
        self.clone = self.root / CLONE_NAME
        self.trees = self.root / "wt"
        self.paths = PathPolicy(settings.paths_policy)
        self.lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workspace")
        self._pending = 0
        self._idle = threading.Condition()
        self.wait_stopped: Callable[[str], bool] | None = None
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
            self.submit(self.remove_closed, task_id)

    def remove_closed(self, task_id: str) -> None:
        if self.wait_stopped is not None and not self.wait_stopped(task_id):
            self.store.add_event(
                task_id,
                TaskEventType.tool_called,
                {
                    "arguments": branch_name(task_id),
                    "result": "error",
                    "reason": "Удаление рабочей копии отложено до следующего запуска: выполнение задачи не остановилось.",
                },
                tool="git_worktree",
            )
            return
        self.remove_worktree(task_id)

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
            if task.status in WORKTREE_STATUSES:
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

    def _add_worktree(self, path: Path, *args: str) -> None:
        self.git("worktree", "add", "--quiet", "--no-checkout", *args)
        try:
            self.git("sparse-checkout", "set", "--cone", APP_DIR, cwd=path)
            self.git("checkout", "--quiet", cwd=path)
        except WorkspaceError:
            self.git("worktree", "remove", "--force", str(path))
            raise

    def _is_sparse(self, path: Path) -> bool:
        try:
            return self.git("sparse-checkout", "list", cwd=path) == APP_DIR
        except WorkspaceError:
            return False

    def _outside_app(self, path: Path) -> bool:
        top, child = APP_DIR.split("/")
        for entry in os.scandir(path):
            if entry.name == ".git" or not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name != top or any(item.name != child for item in os.scandir(entry.path)):
                return True
        return False

    def _ensure_sparse(self, path: Path) -> None:
        if self._is_sparse(path) and not self._outside_app(path):
            return
        status = run_git(
            "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all", "--ignored=matching", cwd=path
        )
        for entry in status.decode("utf-8", "surrogateescape").split("\0"):
            rel = entry[3:]
            if rel and "/" in rel and not rel.startswith(f"{APP_DIR}/"):
                raise SparseUnsafe(
                    "Рабочая копия задачи содержит изменённые, неотслеживаемые или игнорируемые файлы вне каталога "
                    "приложения; sparse checkout не применён, рабочая копия оставлена без изменений."
                )
        self.git("sparse-checkout", "set", "--cone", APP_DIR, cwd=path)
        if self._outside_app(path):
            raise SparseUnsafe("После применения sparse checkout в рабочей копии остались файлы вне каталога приложения.")

    def _prepare(self, task: Task) -> tuple[bool, str]:
        branch = branch_name(task.id)
        path = self.worktree_path(task.id)
        self._ensure_clone()
        self.git("worktree", "prune")
        if os.path.lexists(path):
            if not self.is_task_worktree(path, task.id):
                raise WorkspaceError("Каталог рабочей копии занят и не является worktree задачи.")
            self._ensure_sparse(path)
            created = False
        elif self._branch_exists(branch):
            self.trees.mkdir(parents=True, exist_ok=True)
            self._add_worktree(path, str(path), branch)
            created = True
        else:
            self.git("fetch", "--quiet", "origin", timeout=NETWORK_TIMEOUT)
            base = self.git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
            self.trees.mkdir(parents=True, exist_ok=True)
            self._add_worktree(path, "-b", branch, str(path), base)
            return True, base
        if task.base_sha:
            return created, task.base_sha
        return created, self.git("merge-base", branch, "refs/remotes/origin/main")

    def ensure_worktree(self, task_id: str) -> None:
        try:
            task = self.store.get_task(task_id)
        except OfficeError:
            return
        if task.status not in WORKTREE_STATUSES:
            return
        try:
            with self.lock:
                created, base_sha = self._prepare(task)
        except SparseUnsafe as error:
            self.store.add_event(
                task_id,
                TaskEventType.tool_called,
                {"arguments": branch_name(task_id), "result": "error", "reason": str(error)},
                tool="git_worktree",
            )
            self.store.halt(
                task_id, TaskStatus.needs_attention, statuses=SLOT_STATUSES, role=None, step="git_worktree",
                reason=str(error),
            )
            return
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
        size = None
        if change.new_mode == "100644":
            size = int(self.git("cat-file", "-s", change.new_sha, cwd=path, env=env))
        check_change(self.paths, change.path, change.new_mode, size)

    def commit(self, task_id: str, message: str, role: AgentRole | None = None) -> str | None:
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
                        role=role,
                        tool="git_commit",
                    )
                    raise
            self.git("commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", message,
                     cwd=path, env=env | COMMIT_ENV)
            os.replace(index, real)
            sha = self.git("rev-parse", "HEAD", cwd=path)
            names = run_git("diff", "--name-only", "-z", task.base_sha, sha, cwd=path)
        changed = [name for name in names.decode("utf-8").split("\0") if name]
        self.store.record_commit(task_id, sha, changed, role)
        return sha

    def verify_branch(self, task_id: str, base_sha: str | None = None) -> list[str]:
        base = base_sha or self.store.get_task(task_id).base_sha
        if not base:
            raise WorkspaceError("base_sha задачи не записан.")
        with self.lock:
            path = self.task_worktree(task_id)
            return check_range(self.paths, path, base, "HEAD")

    def first_parent(self, base: str, head: str) -> list[str]:
        with self.lock:
            self._ensure_clone()
            self.git("fetch", "--quiet", "origin", "main", timeout=NETWORK_TIMEOUT)
            output = self.git("rev-list", "--first-parent", "--parents", f"{base}..{head}")
        return [line.split() for line in output.splitlines() if line]

    def revert_branch(self, tree: str, commits: list[list[str]]) -> tuple[str, str]:
        path = self.worktree_path(tree)
        branch = branch_name(tree)
        with self.lock:
            self._ensure_clone()
            if os.path.lexists(path):
                self.git("worktree", "remove", "--force", str(path))
            self.git("worktree", "prune")
            self.git("fetch", "--quiet", "origin", timeout=NETWORK_TIMEOUT)
            base = self.git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
            self.trees.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "--quiet", "-B", branch, str(path), base)
            for sha, *parents in commits:
                mainline = ["-m", "1"] if len(parents) > 1 else []
                try:
                    self.git("revert", "--no-edit", *mainline, sha, cwd=path, env=COMMIT_ENV)
                except WorkspaceError as error:
                    with suppress(WorkspaceError):
                        self.git("revert", "--abort", cwd=path, env=COMMIT_ENV)
                    raise WorkspaceError(f"git revert {sha[:7]} не выполнен: конфликт с более поздними изменениями main.") from error
            return base, self.git("rev-parse", "HEAD", cwd=path)

    def changed_names(self, tree: str, base: str, head: str) -> list[str]:
        output = run_git("diff", "--name-only", "-z", base, head, cwd=self.task_worktree(tree))
        return [name for name in output.decode("utf-8").split("\0") if name]

    def head(self, task_id: str) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.task_worktree(task_id))

    def is_ancestor(self, ancestor: str, sha: str) -> bool:
        try:
            self.git("merge-base", "--is-ancestor", ancestor, sha)
        except WorkspaceError:
            return False
        return True

    def push(self, task_id: str, url: str, token: str, sha: str) -> PushResult:
        path = self.task_worktree(task_id)
        argv = ["git", "push", "--porcelain", url, f"{sha}:refs/heads/{branch_name(task_id)}"]
        proc = None
        try:
            proc = subprocess.run(
                argv,
                cwd=path,
                env=push_env(token),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=NETWORK_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        if proc is None:
            return PushResult.error
        if proc.returncode == 0:
            return PushResult.ok
        if b"rejected]" in proc.stdout:
            return PushResult.rejected
        return PushResult.error

    def trailer(self, task_id: str, sha: str, key: str) -> str:
        path = self.task_worktree(task_id)
        return self.git("log", "-1", f"--format=%(trailers:key={key},valueonly)", sha, cwd=path)

    def diff_range(self, task_id: str, base: str, sha: str, limit: int) -> str:
        output = run_git(
            "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, sha, cwd=self.task_worktree(task_id)
        )
        text = output[:limit].decode("utf-8", "replace")
        if len(output) > limit:
            text += f"\n[обрезано: diff, {len(output)} байтов]\n"
        return text

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
        output = run_git("ls-files", "-z", "--", APP_DIR, cwd=path)
        return [name for name in output.decode("utf-8").split("\0") if name]

    @contextmanager
    def checkout(self, sha: str) -> Iterator[tuple[Path, str]]:
        with self.lock:
            full = self.resolve(sha)
            holder = Path(tempfile.mkdtemp(dir=self.root, prefix=".suite-"))
            path = holder / "app"
            try:
                self._add_worktree(path, "--detach", str(path), full)
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

    def qa_files(self, sha: str) -> list[str]:
        listed = run_git("ls-tree", "-r", "--name-only", "-z", sha, "--", QA_DIR, cwd=self.clone)
        return [name for name in listed.decode("utf-8").split("\0") if name]

    def export_qa(self, sha: str, destination: Path) -> list[str]:
        files = self.qa_files(sha)
        if not files:
            return []
        archive = run_git("archive", "--format=tar", sha, "--", QA_DIR, cwd=self.clone)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(destination, filter="data")
        return files
