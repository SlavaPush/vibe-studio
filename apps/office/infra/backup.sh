#!/usr/bin/env bash
set -euo pipefail

HOST="office-vps"
DEST=""

usage() {
  cat <<'EOF'
Использование: bash backup.sh [--host <ssh-хост>] [--dest <каталог>]

Копирует состояние офиса с VPS на компьютер владельца. Запускать на компьютере
владельца, когда сервис office-api на VPS остановлен (sudo systemctl stop office-api):
копировать SQLite работающего сервиса нельзя.

Параметры:
  --host <ssh-хост>  хост SSH из ~/.ssh/config (по умолчанию office-vps)
  --dest <каталог>   каталог копии (по умолчанию ~/backups/vibe-office/<ГГГГ-ММ-ДД>);
                     хранить вне репозитория: в копии есть .env и ключ GitHub App
  -h, --help         показать эту справку

Результат:
  <каталог>/office/        /home/office/office целиком, кроме *.bak-*: .env, vps.env,
                           ключ GitHub App, office.sqlite, office.checkpoints.sqlite
                           (с -wal и -shm), artifacts/, workspace/
  <каталог>/versions.txt   commit клона, образы Docker, ветки office/* в workspace

Повторный запуск с тем же каталогом приводит копию в точное соответствие серверу
(файлы, которых нет на сервере, удаляются из <каталог>/office/).
EOF
}

fail() {
  printf 'Ошибка: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --host)
      [ "$#" -ge 2 ] || fail "для --host не указано значение"
      HOST="$2"
      shift 2
      ;;
    --dest)
      [ "$#" -ge 2 ] || fail "для --dest не указано значение"
      DEST="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "неизвестный параметр $1 (справка: --help)"
      ;;
  esac
done

[ -n "$HOST" ] || fail "пустое значение --host"
if [ -z "$DEST" ]; then
  DEST="$HOME/backups/vibe-office/$(date +%F)"
fi

echo "Проверка сервиса office-api на $HOST"
STATE="$(ssh "$HOST" bash -s <<'EOF'
test -d /home/office/office || { echo missing; exit 0; }
systemctl is-active office-api || true
EOF
)" || fail "не удалось подключиться к $HOST по SSH"
case "$STATE" in
  missing)
    fail "на $HOST нет каталога /home/office/office"
    ;;
  inactive|failed)
    echo "Сервис office-api: $STATE"
    ;;
  *)
    fail "сервис office-api на $HOST в состоянии ${STATE:-неизвестно}; остановить его: ssh $HOST, затем sudo systemctl stop office-api"
    ;;
esac

mkdir -p "$DEST/office"
chmod 700 "$DEST"
VERSIONS="$DEST/versions.txt"

{
  echo "Хост: $HOST"
  echo "Дата копии: $(date '+%Y-%m-%d %H:%M:%S %z')"
  ssh "$HOST" bash -s <<'EOF'
echo "Commit клона /home/office/vibe-studio: $(git -C /home/office/vibe-studio rev-parse HEAD 2>/dev/null || echo 'нет клона')"
echo "Образы Docker (имя, id, дата создания, размер):"
for image in back-office-sandbox:p4 office-python-sandbox:p5 postgres:16-alpine; do
  line="$(docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}} {{.Size}}' "$image" 2>/dev/null || true)"
  echo "  ${line:-$image нет}"
done
EOF
} > "$VERSIONS"

echo "Копирование $HOST:/home/office/office/ в $DEST/office/"
rsync -a --delete --exclude '*.bak-*' "$HOST:/home/office/office/" "$DEST/office/"
chmod 700 "$DEST/office"

echo "Проверка целостности SQLite"
CHECK_DIR="$(mktemp -d)"
trap 'rm -rf "$CHECK_DIR"' EXIT
for db in office.sqlite office.checkpoints.sqlite; do
  path="$DEST/office/$db"
  [ -f "$path" ] || fail "в копии нет $db"
  rm -f "$CHECK_DIR"/*
  cp "$path" "$CHECK_DIR/$db"
  if [ -f "$path-wal" ]; then
    cp "$path-wal" "$CHECK_DIR/$db-wal"
  fi
  result="$(python3 -c '
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
try:
    print(connection.execute("PRAGMA integrity_check").fetchone()[0])
finally:
    connection.close()
' "$CHECK_DIR/$db")" || fail "не удалось проверить $db"
  [ "$result" = "ok" ] || fail "PRAGMA integrity_check для $db вернул: $result"
  echo "  $db: ok"
done

CLONE="$DEST/office/workspace/vibe-studio"
if [ -d "$CLONE/.git" ]; then
  BRANCHES="$(git -C "$CLONE" branch --list 'office/*')"
else
  BRANCHES="  (клона workspace/vibe-studio в копии нет)"
fi
{
  echo "Ветки office/* в workspace/vibe-studio:"
  echo "${BRANCHES:-  (нет)}"
} >> "$VERSIONS"

echo
echo "Копия: $DEST"
echo "Размер: $(du -sh "$DEST" | cut -f1)"
echo "Ветки office/* в workspace/vibe-studio:"
echo "${BRANCHES:-  (нет)}"
echo "Версии: $VERSIONS"
