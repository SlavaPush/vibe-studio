#!/usr/bin/env bash
set -euo pipefail

HOST="office-vps"
BACKUP=""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
ENV_FILE="$ROOT/apps/office/.env"
KEY_FILE="$HOME/.config/vibe-office/vibe-office.private-key.pem"

usage() {
  cat <<'EOF'
Использование: bash apps/office/infra/restore.sh [--host <ssh-хост>] [--backup <копия>/office]

Переносит секреты и, если указана копия, состояние офиса с компьютера владельца
на VPS. Запускать на компьютере владельца из корня монорепозитория после install.sh
на VPS, пока сервис office-api на VPS не запущен.

Параметры:
  --host <ssh-хост>        хост SSH из ~/.ssh/config (по умолчанию office-vps)
  --backup <копия>/office  каталог office/ из копии backup.sh; без параметра
                           офис запустится с чистым состоянием (базы создаст API),
                           поэтому на VPS не должно быть баз, workspace и artifacts
  -h, --help               показать эту справку

Что делает скрипт:
  1. Проверяет, что на VPS есть /home/office/office и сервис office-api не активен;
     без --backup — что в /home/office/office нет баз, workspace и artifacts
     (иначе завершается с ошибкой, существующие данные не удаляются).
  2. Копирует apps/office/.env и ~/.config/vibe-office/vibe-office.private-key.pem
     с этого компьютера (файлы из копии не используются).
  3. С --backup копирует состояние из копии в /home/office/office: файлы на VPS,
     которых нет в копии, удаляются; vps.env, .env, *.pem и office-api.service
     не копируются и не удаляются (vps.env с новым доменом создаёт install.sh).
  4. Закрывает права (600) на .env, vps.env и *.pem; с --backup проверяет, что
     у задач needs_attention есть ветка office/<id> в workspace/vibe-studio.
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
    --backup)
      [ "$#" -ge 2 ] || fail "для --backup не указано значение"
      BACKUP="${2%/}"
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
[ -s "$ENV_FILE" ] || fail "нет файла $ENV_FILE"
[ -s "$KEY_FILE" ] || fail "нет файла $KEY_FILE"
if [ -n "$BACKUP" ]; then
  [ -d "$BACKUP" ] || fail "нет каталога копии $BACKUP"
  if [ ! -f "$BACKUP/office.sqlite" ]; then
    if [ -f "$BACKUP/office/office.sqlite" ]; then
      fail "в $BACKUP нет office.sqlite; указать каталог $BACKUP/office"
    fi
    fail "в $BACKUP нет office.sqlite: это не каталог office/ из копии backup.sh"
  fi
fi

echo "Проверка VPS $HOST"
STATE="$(ssh "$HOST" bash -s <<'EOF'
test -d /home/office/office || { echo missing; exit 0; }
systemctl is-active office-api || true
EOF
)" || fail "не удалось подключиться к $HOST по SSH"
case "$STATE" in
  missing)
    fail "на $HOST нет каталога /home/office/office: сначала выполнить install.sh на VPS"
    ;;
  inactive|failed)
    echo "Сервис office-api: $STATE"
    ;;
  *)
    fail "сервис office-api на $HOST в состоянии ${STATE:-неизвестно}; остановить его: ssh $HOST, затем sudo systemctl stop office-api"
    ;;
esac

if [ -z "$BACKUP" ]; then
  EXISTING="$(ssh "$HOST" bash -s <<'EOF'
cd /home/office/office
for name in office.sqlite* office.checkpoints.sqlite* workspace artifacts; do
  if [ -e "$name" ]; then
    printf '%s ' "$name"
  fi
done
EOF
)" || fail "не удалось проверить состояние на $HOST"
  if [ -n "$EXISTING" ]; then
    fail "без --backup нужен пустой каталог состояния, а в /home/office/office на $HOST уже есть: ${EXISTING% }. Указать --backup или удалить эти файлы вручную"
  fi
fi

echo "Копирование .env и ключа GitHub App"
scp -q "$ENV_FILE" "$HOST:/home/office/office/.env"
scp -q "$KEY_FILE" "$HOST:/home/office/office/vibe-office.private-key.pem"

RESTORED=0
if [ -n "$BACKUP" ]; then
  echo "Копирование состояния из $BACKUP"
  rsync -a --delete \
    --exclude vps.env --exclude office-api.service --exclude .env --exclude '*.pem' --exclude '*.bak-*' \
    "$BACKUP/" "$HOST:/home/office/office/"
  RESTORED=1
fi

DOMAIN="$(ssh "$HOST" bash -s -- "$RESTORED" <<'EOF'
set -euo pipefail
cd /home/office/office
for file in .env vps.env *.pem; do
  if [ -e "$file" ]; then
    chmod 600 "$file"
  fi
done
if [ "$1" = "1" ]; then
  python3 - <<'PY' >&2
import sqlite3
import subprocess

connection = sqlite3.connect("/home/office/office/office.sqlite")
try:
    ids = [row[0] for row in connection.execute("SELECT id FROM tasks WHERE status = 'needs_attention' ORDER BY id")]
finally:
    connection.close()
for task_id in ids:
    branch = f"office/{task_id}"
    found = subprocess.run(
        ["git", "-C", "/home/office/office/workspace/vibe-studio", "branch", "--list", branch],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if found:
        print(f"Задача {task_id} (needs_attention): ветка {branch} есть")
    else:
        print(f"Предупреждение: у задачи {task_id} (needs_attention) нет ветки {branch}; повтор начнёт задачу заново")
if not ids:
    print("Задач needs_attention нет")
PY
fi
if [ -f vps.env ]; then
  sed -n 's/^OFFICE_ALLOWED_HOSTS=//p' vps.env | head -n 1 | cut -d, -f1
fi
EOF
)" || fail "не удалось закрыть права или проверить ветки на $HOST"

echo
echo "Файлы перенесены на $HOST."
echo "Дальнейшие шаги:"
echo "  1. Остановить прежний офис (sudo systemctl stop office-api на старом VPS или локальный office-run):"
echo "     два офиса на один репозиторий одновременно не запускать."
echo "  2. На $HOST запустить офис: sudo systemctl start office-api"
if [ -n "$DOMAIN" ]; then
  echo "  3. Проверить: curl https://$DOMAIN/api/health"
else
  echo "  3. Проверить: curl https://<домен>/api/health (в vps.env на $HOST нет OFFICE_ALLOWED_HOSTS: выполнить install.sh)"
fi
