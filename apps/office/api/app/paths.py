import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from app.schemas import AgentRole

MAX_PATH_LENGTH = 400


class PathDenied(Exception):
    pass


def glob_regex(pattern: str, flags: int = 0) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("/**", index) and index + 3 == len(pattern):
            parts.append("(?:/.*)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts), flags)


def normalize(raw: str) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LENGTH:
        raise PathDenied("Недопустимый путь.")
    if "\0" in raw or "\\" in raw:
        raise PathDenied("Путь содержит недопустимые символы.")
    if raw.startswith("/"):
        raise PathDenied("Абсолютный путь запрещён.")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise PathDenied("Путь должен быть относительным, без «.», «..» и пустых компонентов.")
    return raw


def resolve_inside(root: Path, rel: str) -> Path:
    current = root
    for part in rel.split("/"):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        except NotADirectoryError as error:
            raise PathDenied("Компонент пути не является каталогом.") from error
        if stat.S_ISLNK(mode):
            raise PathDenied("Путь содержит symlink.")
    return root / rel


@dataclass(frozen=True)
class RoleRule:
    write: tuple[re.Pattern[str], ...]
    extensions: frozenset[str]


class PathPolicy:
    def __init__(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.max_file_bytes: int = data["max_file_bytes"]
        self.max_list_entries: int = data["max_list_entries"]
        self.deny = tuple(glob_regex(item, re.IGNORECASE) for item in data["deny"])
        self.read_deny = tuple(glob_regex(item, re.IGNORECASE) for item in data["read_deny"])
        self.roles = {
            AgentRole(name): RoleRule(
                write=tuple(glob_regex(item) for item in rule["write"]),
                extensions=frozenset(rule["extensions"]),
            )
            for name, rule in data["roles"].items()
        }

    def readable(self, rel: str) -> bool:
        return not any(pattern.fullmatch(rel) for pattern in self.read_deny)

    def check_read(self, raw: str) -> str:
        rel = normalize(raw)
        if not self.readable(rel):
            raise PathDenied("Чтение этого пути запрещено политикой офиса.")
        return rel

    def check_write(self, role: AgentRole | None, raw: str, size: int | None = None) -> str:
        rel = normalize(raw)
        if any(pattern.fullmatch(rel) for pattern in self.deny):
            raise PathDenied("Запись в этот путь запрещена политикой офиса.")
        rule = self.roles.get(role) if role is not None else None
        if rule is None or not any(pattern.fullmatch(rel) for pattern in rule.write):
            raise PathDenied("Путь вне каталогов, разрешённых роли для записи.")
        if PurePosixPath(rel).suffix not in rule.extensions:
            raise PathDenied("Расширение файла не разрешено роли.")
        if size is not None and size > self.max_file_bytes:
            raise PathDenied(f"Файл больше {self.max_file_bytes} байт.")
        return rel

    def check_any_writer(self, raw: str, size: int | None = None) -> str:
        first: PathDenied | None = None
        for role in self.roles:
            try:
                return self.check_write(role, raw, size)
            except PathDenied as error:
                first = first or error
        raise first or PathDenied("Запись запрещена.")
