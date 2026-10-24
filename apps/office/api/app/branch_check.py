import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.paths import PathDenied, PathPolicy

GIT_TIMEOUT = 60
DENIED_MODES = {
    "100755": "Исполняемый файл запрещён.",
    "120000": "Symlink запрещён.",
    "160000": "Submodule запрещён.",
}
DELETED_MODE = "000000"
DISABLE_MARKERS = re.compile(r"eslint-disable|oxlint-disable|@ts-ignore|@ts-nocheck|@ts-expect-error")


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class RawChange:
    new_mode: str
    new_sha: str
    path: str


@dataclass(frozen=True)
class ChangedFile:
    path: str
    mode: str
    size: int | None
    binary: bool
    added: tuple[str, ...]


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


def check_change(policy: PathPolicy, path: str, mode: str, size: int | None) -> None:
    if mode == DELETED_MODE:
        policy.check_any_writer(path)
        return
    if mode in DENIED_MODES:
        raise PathDenied(DENIED_MODES[mode])
    if mode != "100644":
        raise PathDenied("Недопустимый тип файла.")
    policy.check_any_writer(path, size)


def violations(policy: PathPolicy, files: list[ChangedFile]) -> list[str]:
    found = []
    for item in files:
        try:
            check_change(policy, item.path, item.mode, item.size)
        except PathDenied as error:
            found.append(f"{item.path}: {error}")
            continue
        if item.mode == DELETED_MODE:
            continue
        if item.binary:
            found.append(f"{item.path}: двоичное содержимое запрещено.")
        markers = sorted({match.group(0) for line in item.added for match in DISABLE_MARKERS.finditer(line)})
        if markers:
            found.append(f"{item.path}: комментарий отключения проверок ({', '.join(markers)}).")
    return found


def added_lines(patch: bytes) -> tuple[str, ...]:
    lines = []
    in_hunk = False
    for line in patch.decode("utf-8", "replace").split("\n"):
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            lines.append(line[1:])
    return tuple(lines)


def collect(repo: Path, base: str, head: str) -> list[ChangedFile]:
    raw = run_git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, head, cwd=repo)
    numstat = run_git("diff", "--numstat", "-z", "--no-renames", base, head, cwd=repo)
    binary = {
        entry.split("\t", 2)[2]
        for entry in numstat.decode("utf-8", "surrogateescape").split("\0")
        if entry.startswith("-\t-\t")
    }
    literal = {"GIT_LITERAL_PATHSPECS": "1"}
    files = []
    for change in parse_raw(raw):
        size = None
        added: tuple[str, ...] = ()
        if change.new_mode != DELETED_MODE:
            if change.new_mode == "100644":
                size = int(run_git("cat-file", "-s", change.new_sha, cwd=repo).decode().strip())
            patch = run_git(
                "diff", "-U0", "--text", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                base, head, "--", change.path, cwd=repo, env=literal,
            )
            added = added_lines(patch)
        files.append(ChangedFile(change.path, change.new_mode, size, change.path in binary, added))
    return files


def check_range(policy: PathPolicy, repo: Path, base: str, head: str) -> list[str]:
    return violations(policy, collect(repo, base, head))
