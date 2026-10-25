import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.paths import PathDenied, PathPolicy
from app.schemas import ChangeClass

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
    old_sha: str = ""


@dataclass(frozen=True)
class ChangedFile:
    path: str
    mode: str
    size: int | None
    binary: bool
    added: tuple[str, ...]
    old: bytes | None = None
    new: bytes | None = None


PACKAGE_JSON = "apps/chat/web/package.json"
PYPROJECT = "apps/chat/api/pyproject.toml"
SCHEMA_SQL = "apps/chat/api/app/schema.sql"
INSPECTED = frozenset({PACKAGE_JSON, PYPROJECT, SCHEMA_SQL})
NULL_SHA = "0" * 40
MANIFEST_VIOLATION = "изменение манифеста вне разделов зависимостей"
SCHEMA_VIOLATION = "неаддитивное изменение схемы"
NPM_SECTIONS = ("dependencies", "devDependencies")
NPM_VERSION = re.compile(r"[0-9A-Za-z.^~<>=|*+\- ]+")
IDENT = r"[a-z_][a-z0-9_]*"
COLUMN_TYPES = r"(?:text|integer|bigint|boolean|timestamptz|jsonb|uuid|numeric)"
LITERAL = r"(?:-?\d+(?:\.\d+)?|'(?:[^']|'')*'|true|false|now ?\(\))"
ADD_COLUMN = re.compile(
    rf"alter table {IDENT} add column if not exists {IDENT} {COLUMN_TYPES}(?: default {LITERAL})?", re.IGNORECASE
)
ADD_INDEX = re.compile(
    rf"create index if not exists {IDENT} on {IDENT} \({IDENT}(?: ?, ?{IDENT})*\)", re.IGNORECASE
)
NEW_TABLE = re.compile(rf"create table if not exists ({IDENT}) ?\((.*)\)", re.IGNORECASE | re.DOTALL)
BASE_TABLE = re.compile(rf"create table (?:if not exists )?({IDENT})", re.IGNORECASE)
REFERENCES = re.compile(r"\breferences\b", re.IGNORECASE)
CASCADE = re.compile(rf"\breferences {IDENT}(?: ?\({IDENT}(?: ?, ?{IDENT})*\))? on delete cascade\b", re.IGNORECASE)


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
        _, new_mode, old_sha, new_sha, _ = meta.lstrip(":").split(" ")
        changes.append(RawChange(new_mode=new_mode, new_sha=new_sha, path=path, old_sha=old_sha))
    return changes


def check_change(
    policy: PathPolicy, path: str, mode: str, size: int | None, change_class: ChangeClass | None = None
) -> None:
    if mode == DELETED_MODE:
        policy.check_any_writer(path, None, change_class)
        return
    if mode in DENIED_MODES:
        raise PathDenied(DENIED_MODES[mode])
    if mode != "100644":
        raise PathDenied("Недопустимый тип файла.")
    policy.check_any_writer(path, size, change_class)


def npm_violation(old: bytes | None, new: bytes | None) -> bool:
    if new is None:
        return True
    try:
        before = json.loads(old) if old else {}
        after = json.loads(new)
    except ValueError:
        return True
    if not isinstance(before, dict) or not isinstance(after, dict):
        return True
    if {k: v for k, v in before.items() if k not in NPM_SECTIONS} != {
        k: v for k, v in after.items() if k not in NPM_SECTIONS
    }:
        return True
    for section in NPM_SECTIONS:
        values = after.get(section, {})
        known = before.get(section) or {}
        if not isinstance(values, dict) or not isinstance(known, dict):
            return True
        for name, value in values.items():
            if known.get(name) == value:
                continue
            if not isinstance(value, str) or not NPM_VERSION.fullmatch(value) or value.lower().startswith("git"):
                return True
    return False


def python_violation(old: bytes | None, new: bytes | None) -> bool:
    if new is None:
        return True
    try:
        before = tomllib.loads(old.decode("utf-8")) if old else {}
        after = tomllib.loads(new.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return True

    def rest(data: dict) -> dict:
        project = {k: v for k, v in (data.get("project") or {}).items() if k != "dependencies"}
        return {k: v for k, v in data.items() if k not in ("project", "dependency-groups")} | {"project": project}

    if rest(before) != rest(after):
        return True
    known = set((before.get("project") or {}).get("dependencies") or [])
    for groups in (before.get("dependency-groups") or {}).values():
        known.update(item for item in groups if isinstance(item, str))
    entries = (after.get("project") or {}).get("dependencies", [])
    groups = after.get("dependency-groups", {})
    if not isinstance(entries, list) or not isinstance(groups, dict) or not all(isinstance(g, list) for g in groups.values()):
        return True
    for item in [*entries, *(entry for group in groups.values() for entry in group)]:
        if isinstance(item, dict) and set(item) == {"include-group"} and isinstance(item["include-group"], str):
            continue
        if not isinstance(item, str):
            return True
        if item not in known and "@" in item:
            return True
    return False


def split_sql(text: str) -> list[str] | None:
    statements = []
    current: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "$" or text.startswith("/*", index):
            return None
        if text.startswith("--", index):
            end = text.find("\n", index)
            index = len(text) if end == -1 else end
            continue
        if char == "'":
            end = index + 1
            while True:
                end = text.find("'", end)
                if end == -1:
                    return None
                if text.startswith("''", end):
                    end += 2
                    continue
                break
            current.append(text[index:end + 1])
            index = end + 1
            continue
        if char == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    statements.append("".join(current))
    return statements


def normalized(statement: str) -> str:
    text = re.sub(r"\s+", " ", statement).strip()
    text = re.sub(r" ?\( ?", " (", text)
    text = re.sub(r" ?\)", ")", text)
    return re.sub(r" ?, ?", ", ", text)


def balanced(body: str) -> bool:
    depth = 0
    quoted = False
    for char in body:
        if char == "'":
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not quoted


def schema_violation(old: bytes | None, new: bytes | None) -> bool:
    try:
        base = (old or b"").decode("utf-8")
        head = new.decode("utf-8") if new is not None else None
    except UnicodeDecodeError:
        return True
    if head is None or not head.startswith(base):
        return True
    base_parts = split_sql(base)
    tail_parts = split_sql(head[len(base):])
    if base_parts is None or tail_parts is None or base_parts[-1].strip():
        return True
    tables = {match.group(1).lower() for part in base_parts for match in BASE_TABLE.finditer(normalized(part))}
    for part in tail_parts:
        statement = normalized(part)
        if not statement or ADD_COLUMN.fullmatch(statement) or ADD_INDEX.fullmatch(statement):
            continue
        match = NEW_TABLE.fullmatch(statement)
        if match is None or match.group(1).lower() in tables or not balanced(match.group(2)):
            return True
        if len(REFERENCES.findall(match.group(2))) != len(CASCADE.findall(match.group(2))):
            return True
        tables.add(match.group(1).lower())
    return False


CONTENT_CHECKS = {
    PACKAGE_JSON: (npm_violation, MANIFEST_VIOLATION),
    PYPROJECT: (python_violation, MANIFEST_VIOLATION),
    SCHEMA_SQL: (schema_violation, SCHEMA_VIOLATION),
}


def violations(policy: PathPolicy, files: list[ChangedFile], change_class: ChangeClass | None = None) -> list[str]:
    found = []
    for item in files:
        try:
            check_change(policy, item.path, item.mode, item.size, change_class)
        except PathDenied as error:
            found.append(f"{item.path}: {error}")
            continue
        content = CONTENT_CHECKS.get(item.path)
        if content is not None and content[0](item.old, None if item.mode == DELETED_MODE else item.new):
            found.append(f"{item.path}: {content[1]}.")
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
        old = new = None
        if change.path in INSPECTED:
            if change.old_sha and change.old_sha != NULL_SHA:
                old = run_git("cat-file", "blob", change.old_sha, cwd=repo)
            if change.new_mode == "100644":
                new = run_git("cat-file", "blob", change.new_sha, cwd=repo)
        files.append(ChangedFile(change.path, change.new_mode, size, change.path in binary, added, old, new))
    return files


def check_range(
    policy: PathPolicy, repo: Path, base: str, head: str, change_class: ChangeClass | None = None
) -> list[str]:
    return violations(policy, collect(repo, base, head), change_class)
