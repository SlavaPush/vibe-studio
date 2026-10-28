#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/SlavaPush/vibe-studio.git"
OFFICE_USER="office"
CLONE_DIR="/home/office/vibe-studio"
STATE_DIR="/home/office/office"
UV_BIN="/home/office/.local/bin/uv"
NODE_MAJOR="24"

DOMAIN=""
REF="main"

usage() {
  cat <<'EOF'
Использование: bash install.sh --domain <домен> [--ref <ветка или commit>]

Устанавливает офис на новый VPS с Ubuntu. Запускать под пользователем office,
у которого есть sudo (пароль запрашивается) и ключ SSH в ~/.ssh/authorized_keys.
DNS домена должен указывать на этот сервер.

Параметры:
  --domain <домен>  домен офиса (обязательный): вписывается в vps.env и /etc/caddy/Caddyfile
  --ref <ref>       ветка, тег или commit репозитория (по умолчанию main)
  -h, --help        показать эту справку

Что делает скрипт (выполненные шаги пропускаются; ssh, Caddy и конфигурация
systemd перезагружаются при каждом запуске):
  1. Проверяет пользователя office, Ubuntu и sudo.
  2. Ставит git, curl, ca-certificates, rsync, ufw, Docker CE и Caddy
     из официальных репозиториев apt, добавляет office в группу docker.
  3. Открывает в ufw OpenSSH, 80/tcp и 443/tcp; запрещает вход root и вход
     по паролю в SSH, если ~/.ssh/authorized_keys непустой.
  4. Ставит uv в /home/office/.local/bin и Node 24 из репозитория NodeSource.
  5. Клонирует репозиторий в /home/office/vibe-studio (или обновляет до --ref),
     выполняет uv sync --locked и сборку панели.
  6. Собирает образы back-office-sandbox:p4 и office-python-sandbox:p5
     (повторная сборка берёт слои из кэша) и загружает postgres:16-alpine.
  7. Создаёт /home/office/office (права 700) и vps.env из infra/vps.env.example.
     Если vps.env уже есть, в нём заменяются только строки OFFICE_ALLOWED_HOSTS
     и OFFICE_ALLOWED_ORIGINS, остальные строки не меняются.
     Записывает /etc/caddy/Caddyfile с переданным доменом и перезагружает Caddy.
     Устанавливает unit office-api и включает автозапуск без запуска сервиса.

Сервис office-api скрипт не запускает. Секреты (.env и ключ GitHub App) копирует
restore.sh с компьютера владельца.
EOF
}

log() {
  printf '\n==> %s\n' "$*"
}

fail() {
  printf 'Ошибка: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain)
      [ "$#" -ge 2 ] || fail "для --domain не указано значение"
      DOMAIN="$2"
      shift 2
      ;;
    --ref)
      [ "$#" -ge 2 ] || fail "для --ref не указано значение"
      REF="$2"
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

[ -n "$DOMAIN" ] || fail "не указан --domain (справка: --help)"
printf '%s' "$DOMAIN" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$' \
  || fail "домен $DOMAIN не похож на имя хоста (пример: office.example.com)"
[ -n "$REF" ] || fail "пустое значение --ref"

apt_install() {
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  else
    sudo docker "$@"
  fi
}

log "Проверки"
[ "$(id -un)" = "$OFFICE_USER" ] || fail "скрипт запускается под пользователем $OFFICE_USER, сейчас $(id -un)"
[ "$HOME" = "/home/$OFFICE_USER" ] || fail "домашний каталог должен быть /home/$OFFICE_USER, сейчас $HOME"
[ -r /etc/os-release ] || fail "нет /etc/os-release"
os_release_value() {
  sed -n "s/^$1=//p" /etc/os-release | head -n 1 | tr -d "\"'"
}
OS_ID="$(os_release_value ID)"
CODENAME="$(os_release_value UBUNTU_CODENAME)"
[ -n "$CODENAME" ] || CODENAME="$(os_release_value VERSION_CODENAME)"
[ "$OS_ID" = "ubuntu" ] || fail "поддерживается только Ubuntu, сейчас ${OS_ID:-неизвестная система}"
[ -n "$CODENAME" ] || fail "в /etc/os-release нет кодового имени выпуска Ubuntu"
sudo -v || fail "sudo недоступен"
ARCH="$(dpkg --print-architecture)"

log "Пакеты"
sudo apt-get update
apt_install git curl ca-certificates rsync ufw gnupg

if [ -f /etc/apt/sources.list.d/docker.list ] && [ -f /etc/apt/keyrings/docker.asc ]; then
  echo "Репозиторий Docker уже подключён"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' "$ARCH" "$CODENAME" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
fi

if [ -f /etc/apt/sources.list.d/caddy-stable.list ] && [ -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
  echo "Репозиторий Caddy уже подключён"
else
  apt_install debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
fi

sudo apt-get update
apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin caddy
sudo systemctl enable --now docker caddy

if id -nG "$OFFICE_USER" | tr ' ' '\n' | grep -qx docker; then
  echo "Пользователь $OFFICE_USER уже в группе docker"
else
  sudo usermod -aG docker "$OFFICE_USER"
  echo "Пользователь $OFFICE_USER добавлен в группу docker; в этом запуске docker вызывается через sudo"
fi

log "Сеть и SSH"
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

SSHD_HARDENING="/etc/ssh/sshd_config.d/00-hardening.conf"
SSHD_CONTENT="$(printf 'PermitRootLogin no\nPasswordAuthentication no')"
if [ -s "$HOME/.ssh/authorized_keys" ]; then
  if [ -f "$SSHD_HARDENING" ] && [ "$(sudo cat "$SSHD_HARDENING")" = "$SSHD_CONTENT" ]; then
    echo "$SSHD_HARDENING уже записан"
  else
    printf '%s\n' "$SSHD_CONTENT" | sudo tee "$SSHD_HARDENING" >/dev/null
  fi
  sudo sshd -t
  sudo systemctl restart ssh
  echo "Вход root и вход по паролю в SSH запрещены"
else
  echo "Предупреждение: ~/.ssh/authorized_keys пустой, вход по паролю в SSH оставлен" >&2
fi

log "uv и Node"
if [ -x "$UV_BIN" ]; then
  echo "uv уже установлен: $("$UV_BIN" --version)"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh
  [ -x "$UV_BIN" ] || fail "uv не найден в $UV_BIN после установки"
fi

if [ -x /usr/bin/node ] && [ "$(/usr/bin/node -p 'process.versions.node.split(".")[0]')" = "$NODE_MAJOR" ]; then
  echo "Node $NODE_MAJOR уже установлен: $(/usr/bin/node --version)"
else
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash -
  apt_install nodejs
fi

log "Клон и сборка"
if [ -d "$CLONE_DIR/.git" ]; then
  git -C "$CLONE_DIR" fetch --prune --tags origin
else
  git clone "$REPO_URL" "$CLONE_DIR"
fi
if git -C "$CLONE_DIR" show-ref --verify --quiet "refs/remotes/origin/$REF"; then
  git -C "$CLONE_DIR" checkout "$REF"
  git -C "$CLONE_DIR" pull --ff-only origin "$REF"
else
  git -C "$CLONE_DIR" checkout --detach "$REF"
fi
echo "Клон на commit $(git -C "$CLONE_DIR" rev-parse HEAD)"

(cd "$CLONE_DIR/apps/office/api" && "$UV_BIN" sync --locked)
(cd "$CLONE_DIR/apps/office/web" && npm ci && npm run build)

log "Образы Docker"
(cd "$CLONE_DIR" && docker_cmd build -t back-office-sandbox:p4 apps/office/docker/sandbox)
(cd "$CLONE_DIR" && docker_cmd build -t office-python-sandbox:p5 apps/office/docker/sandbox-python)
if docker_cmd image inspect postgres:16-alpine >/dev/null 2>&1; then
  echo "Образ postgres:16-alpine уже загружен"
else
  docker_cmd pull postgres:16-alpine
fi

log "Каталог состояния и vps.env"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
VPS_ENV="$STATE_DIR/vps.env"
VPS_ENV_TMP="$(mktemp "$STATE_DIR/.vps.env.XXXXXX")"
if [ -f "$VPS_ENV" ]; then
  VPS_ENV_SOURCE="$VPS_ENV"
else
  VPS_ENV_SOURCE="$CLONE_DIR/apps/office/infra/vps.env.example"
fi
sed -e "s|^OFFICE_ALLOWED_HOSTS=.*|OFFICE_ALLOWED_HOSTS=$DOMAIN|" \
  -e "s|^OFFICE_ALLOWED_ORIGINS=.*|OFFICE_ALLOWED_ORIGINS=https://$DOMAIN|" \
  "$VPS_ENV_SOURCE" > "$VPS_ENV_TMP"
grep -q '^OFFICE_ALLOWED_HOSTS=' "$VPS_ENV_TMP" || printf 'OFFICE_ALLOWED_HOSTS=%s\n' "$DOMAIN" >> "$VPS_ENV_TMP"
grep -q '^OFFICE_ALLOWED_ORIGINS=' "$VPS_ENV_TMP" || printf 'OFFICE_ALLOWED_ORIGINS=https://%s\n' "$DOMAIN" >> "$VPS_ENV_TMP"
chmod 600 "$VPS_ENV_TMP"
mv "$VPS_ENV_TMP" "$VPS_ENV"
echo "vps.env: OFFICE_ALLOWED_HOSTS=$DOMAIN, OFFICE_ALLOWED_ORIGINS=https://$DOMAIN"

log "Caddy"
CADDY_TMP="$(mktemp)"
sed -e "1s|^[^[:space:]]* {\$|$DOMAIN {|" "$CLONE_DIR/apps/office/infra/Caddyfile" > "$CADDY_TMP"
[ "$(head -n 1 "$CADDY_TMP")" = "$DOMAIN {" ] || fail "не удалось подставить домен в infra/Caddyfile"
if [ -f /etc/caddy/Caddyfile ] && cmp -s "$CADDY_TMP" /etc/caddy/Caddyfile; then
  echo "/etc/caddy/Caddyfile уже содержит домен $DOMAIN"
else
  sudo install -m 0644 "$CADDY_TMP" /etc/caddy/Caddyfile
  echo "/etc/caddy/Caddyfile записан"
fi
rm -f "$CADDY_TMP"
sudo systemctl reload-or-restart caddy
echo "Caddy перезагружен"

log "Unit office-api"
UNIT_SRC="$CLONE_DIR/apps/office/infra/office-api.service"
UNIT_DST="/etc/systemd/system/office-api.service"
if [ -f "$UNIT_DST" ] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  echo "Unit office-api уже установлен"
else
  sudo install -m 0644 "$UNIT_SRC" "$UNIT_DST"
  echo "Unit office-api установлен"
fi
sudo systemctl daemon-reload
sudo systemctl enable office-api

log "Установка завершена, сервис office-api не запущен"
MISSING=""
[ -s "$STATE_DIR/.env" ] || MISSING="$MISSING $STATE_DIR/.env"
[ -s "$STATE_DIR/vibe-office.private-key.pem" ] || MISSING="$MISSING $STATE_DIR/vibe-office.private-key.pem"
if [ -n "$MISSING" ]; then
  echo "Не хватает файлов:$MISSING"
  echo "Скопировать их с компьютера владельца (из корня монорепозитория): bash apps/office/infra/restore.sh --host <ssh-хост этого сервера> [--backup <копия>/office]"
fi
cat <<EOF
Дальнейшие шаги:
  1. Остановить прежний офис (sudo systemctl stop office-api на старом VPS или локальный office-run):
     два офиса на один репозиторий одновременно не запускать.
  2. Запустить офис: sudo systemctl start office-api
  3. Проверить: curl https://$DOMAIN/api/health
Новый сеанс SSH нужен, чтобы docker работал без sudo в командах владельца.
EOF
