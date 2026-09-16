#!/usr/bin/env bash
# Установщик sabz-notifier на VPS. Идемпотентный: можно гонять повторно.
set -euo pipefail

APP="sabz-notifier"
DIR="/opt/${APP}"
SVC_USER="sabz"
UNIT="/etc/systemd/system/${APP}.service"
CLI="/usr/local/bin/${APP}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TOKEN=""; OWNER=""; MODE="light"; INTERVAL="25"
DO_SWAP=1; DO_START=1; ASSUME_YES=0; FORCE=0; UNINSTALL=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; D=$'\e[2m'; N=$'\e[0m'; BD=$'\e[1m'
else R=""; G=""; Y=""; B=""; D=""; N=""; BD=""; fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s%s%s\n' "$B" "$N" "$BD" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
${BD}sabz-notifier${N} — уведомления о новых сообщениях Avito и VK в Telegram.

  sudo ./install.sh [ключи]

${BD}Ключи${N}
  --token TOKEN     токен бота от @BotFather
  --owner NAME      твой telegram-username без @ (только он управляет ботом)
  --mode light      без браузера, ~25 МБ RAM  ${D}(по умолчанию)${N}
  --mode browser    свой браузер, вход один раз  ${D}(нужно >= 2 ГБ RAM)${N}
  --interval SEC    период опроса, по умолчанию ${INTERVAL}
  --no-swap         не создавать swap на машине с малой памятью
  --no-start        установить, но не запускать
  --force           игнорировать предупреждения о ресурсах
  -y, --yes         не задавать вопросов
  --uninstall       удалить бота (данные можно сохранить)
  -h, --help        эта справка

${BD}Пример${N}
  sudo ./install.sh --token 123:AA... --owner sabzrr --mode light -y
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2;;
    --owner) OWNER="${2:-}"; shift 2;;
    --mode) MODE="${2:-}"; shift 2;;
    --interval) INTERVAL="${2:-}"; shift 2;;
    --no-swap) DO_SWAP=0; shift;;
    --no-start) DO_START=0; shift;;
    --force) FORCE=1; shift;;
    -y|--yes) ASSUME_YES=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "неизвестный ключ: $1 (--help)";;
  esac
done

[ "$(id -u)" = "0" ] || die "нужен root: sudo ./install.sh"

# запуск команды от имени служебного пользователя
if command -v runuser >/dev/null 2>&1; then AS_USER=(runuser -u "$SVC_USER" --)
else AS_USER=(sudo -u "$SVC_USER"); fi

# ------------------------------------------------------------------ удаление
if [ "$UNINSTALL" = "1" ]; then
  step "Удаляю ${APP}"
  systemctl disable --now "${APP}.service" 2>/dev/null || true
  rm -f "$UNIT" "$CLI"; systemctl daemon-reload || true
  ok "служба и команда удалены"
  if [ -d "$DIR" ]; then
    keep="y"
    if [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then
      read -r -p "  Сохранить данные в ${DIR}/data (куки, история)? [Y/n] " a || true
      case "${a:-y}" in [Nn]*) keep="n";; esac
    fi
    if [ "$keep" = "n" ]; then rm -rf "$DIR"; ok "каталог ${DIR} удалён"
    else rm -f "$DIR"/bot.py "$DIR"/.env; ok "данные сохранены в ${DIR}/data"; fi
  fi
  say ""; say "${G}Готово.${N} Пользователь ${SVC_USER} и swap не тронуты."
  exit 0
fi

say ""
say "${BD}  sabz-notifier${N} ${D}— уведомления Avito и VK в Telegram${N}"
say ""

# ------------------------------------------------------------------- вводные
[ -f "$SRC/bot.py" ] || die "рядом нет bot.py — запускай install.sh из папки репозитория"

if [ -z "$TOKEN" ] && [ -f "$DIR/.env" ]; then
  TOKEN="$(grep -E '^TG_TOKEN=' "$DIR/.env" | cut -d= -f2- || true)"
  [ -n "$TOKEN" ] && ok "токен взят из установленного ранее .env"
fi
if [ -z "$TOKEN" ]; then
  [ -t 0 ] || die "нужен --token (неинтерактивный запуск)"
  read -r -p "  Токен бота от @BotFather: " TOKEN
fi
[[ "$TOKEN" == *:* ]] || die "токен не похож на токен (ожидается 123456:AA...)"

if [ -z "$OWNER" ] && [ -f "$DIR/.env" ]; then
  OWNER="$(grep -E '^TG_OWNER=' "$DIR/.env" | cut -d= -f2- || true)"
fi
if [ -z "$OWNER" ]; then
  [ -t 0 ] || die "нужен --owner (неинтерактивный запуск)"
  read -r -p "  Твой telegram-username без @: " OWNER
fi
OWNER="${OWNER#@}"

case "$MODE" in light|browser) ;; *) die "--mode должен быть light или browser";; esac

RAM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
if [ "$MODE" = "browser" ] && [ "$RAM_MB" -lt 1800 ] && [ "$FORCE" != "1" ]; then
  warn "на машине ${RAM_MB} МБ RAM — браузерный режим требует ~2 ГБ."
  warn "поставлю лёгкий режим. Нужен всё равно браузерный — добавь --force."
  MODE="light"
fi

say ""
say "  режим:     ${BD}${MODE}${N}   владелец: ${BD}@${OWNER}${N}   опрос: ${BD}${INTERVAL}с${N}"
say "  каталог:   ${DIR}"
say "  память:    ${RAM_MB} МБ"
say ""
if [ "$ASSUME_YES" != "1" ] && [ -t 0 ]; then
  read -r -p "  Продолжаем? [Y/n] " a || true
  case "${a:-y}" in [Nn]*) say "отменено"; exit 0;; esac
fi

# ------------------------------------------------------------------- пакеты
step "Системные пакеты"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  PKGS="python3 ca-certificates"
  [ "$MODE" = "browser" ] && PKGS="$PKGS python3-pip python3-venv xvfb x11vnc novnc websockify fonts-liberation"
  apt-get update -qq
  # shellcheck disable=SC2086
  apt-get install -y -qq $PKGS >/dev/null
  ok "установлены: $PKGS"
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q python3 ca-certificates >/dev/null
  ok "установлены: python3"
else
  warn "неизвестный пакетный менеджер — проверь, что есть python3"
fi
command -v python3 >/dev/null 2>&1 || die "python3 не найден"
ok "python: $(python3 --version 2>&1)"

# --------------------------------------------------------------------- swap
step "Память и swap"
SWAP_MB=$(awk '/SwapTotal/{printf "%d", $2/1024}' /proc/meminfo)
if [ "$SWAP_MB" -gt 0 ]; then
  ok "swap уже есть: ${SWAP_MB} МБ"
elif [ "$DO_SWAP" != "1" ]; then
  warn "swap нет, создание отключено ключом --no-swap"
elif [ "$RAM_MB" -ge 2048 ]; then
  ok "памяти достаточно, swap не нужен"
else
  FREE_MB=$(df -m --output=avail / | tail -1 | tr -d ' ')
  if [ "$FREE_MB" -lt 3000 ]; then
    warn "мало места на диске (${FREE_MB} МБ) — swap не создаю"
  else
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile && mkswap -q /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "создан swap 2 ГБ (/swapfile), подключён и прописан в fstab"
  fi
fi

# ------------------------------------------------------------- пользователь
step "Пользователь и каталоги"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -m -d "$DIR" -s /usr/sbin/nologin "$SVC_USER"
mkdir -p "$DIR/data"
ok "пользователь ${SVC_USER}, каталог ${DIR}"

# -------------------------------------------------------------------- файлы
step "Файлы бота"
install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$SRC/bot.py" "$DIR/bot.py"
umask 077
cat > "$DIR/.env" <<EOF
TG_TOKEN=${TOKEN}
TG_OWNER=${OWNER}
BOT_MODE=${MODE}
BOT_DATA=${DIR}/data
BOT_HEADLESS=$([ "$MODE" = "browser" ] && echo 1 || echo "")
PYTHONIOENCODING=utf-8
EOF
chown root:"$SVC_USER" "$DIR/.env"; chmod 640 "$DIR/.env"
chown -R "$SVC_USER":"$SVC_USER" "$DIR/data"
ok "bot.py и .env на месте (.env читает только служба)"

# ---------------------------------------------------------------- playwright
if [ "$MODE" = "browser" ]; then
  step "Браузер для бота"
  if ! "${AS_USER[@]}" python3 -c "import playwright" 2>/dev/null; then
    pip3 install --quiet --break-system-packages playwright 2>/dev/null \
      || pip3 install --quiet playwright
    ok "playwright установлен"
  else ok "playwright уже установлен"; fi
  if "${AS_USER[@]}" env HOME="$DIR" python3 -m playwright install chromium >/dev/null 2>&1; then
    ok "chromium загружен"
  else
    warn "chromium не загрузился — доустанови вручную:"
    warn "  runuser -u ${SVC_USER} -- env HOME=${DIR} python3 -m playwright install chromium"
  fi
fi

# ------------------------------------------------------------------ systemd
step "Служба systemd"
if [ "$MODE" = "browser" ]; then MEM_MAX="1400M"; MEM_HIGH="1100M"; CPUQ="80%"
else MEM_MAX="200M"; MEM_HIGH="150M"; CPUQ="25%"; fi

cat > "$UNIT" <<EOF
[Unit]
Description=sabz-notifier — уведомления Avito/VK в Telegram
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${DIR}
Environment=HOME=${DIR}
ExecStart=/usr/bin/python3 -u ${DIR}/bot.py
Restart=always
RestartSec=10
MemoryMax=${MEM_MAX}
MemoryHigh=${MEM_HIGH}
CPUQuota=${CPUQ}
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=no
ProtectSystem=strict
ReadWritePaths=${DIR}
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
ok "юнит ${APP}.service (память до ${MEM_MAX}, CPU до ${CPUQ})"

# --------------------------------------------------------------------- CLI
step "Команда управления"
if [ -f "$SRC/sabzctl" ]; then install -m 0755 "$SRC/sabzctl" "$CLI"; ok "команда: ${APP}"
else warn "sabzctl не найден рядом — команда ${APP} не установлена"; fi

# -------------------------------------------------------------- самопроверка
step "Самопроверка"
set +e
"${AS_USER[@]}" env HOME="$DIR" python3 "$DIR/bot.py" --selfcheck 2>&1 | sed 's/^/  /'
CHECK=${PIPESTATUS[0]}
set -e
[ "$CHECK" = "0" ] || warn "самопроверка нашла проблемы (см. выше)"

# ---------------------------------------------------------------------- пуск
if [ "$DO_START" = "1" ]; then
  step "Запуск"
  systemctl enable --now "${APP}.service" >/dev/null 2>&1
  sleep 3
  if systemctl is-active --quiet "${APP}.service"; then ok "служба работает"
  else warn "служба не поднялась — логи: ${APP} logs"; fi
else
  warn "не запускаю (--no-start). Старт: systemctl enable --now ${APP}"
fi

# ---------------------------------------------------------------------- итог
say ""
say "${G}${BD}  Готово.${N}"
say ""
say "  ${BD}Дальше:${N}"
say "    1. Напиши боту в Telegram ${BD}/start${N} — он запомнит твой чат."
if [ "$MODE" = "light" ]; then
  say "    2. Дай доступ к мессенджерам:"
  say "       ${BD}/vk${N}    — пришли cURL или токен ВК"
  say "       ${BD}/avito${N} — пришли cURL страницы мессенджера Авито"
  say "       ${D}cURL берётся так: F12 → Network → нужный запрос →${N}"
  say "       ${D}правой кнопкой → Copy → Copy as cURL (bash)${N}"
else
  say "    2. Войди в аккаунты: ${BD}${APP} login${N} (откроет браузер через SSH-туннель)"
fi
say ""
say "  ${BD}Управление:${N}  ${APP} status | logs | restart | selfcheck | uninstall"
say ""
if [ "$MODE" = "light" ]; then
  say "  ${Y}Важно:${N} Авито блокирует зарубежные дата-центры (HTTP 429)."
  say "  Если сервер не в РФ — задай боту прокси: ${BD}/proxy avito http://user:pass@host:port${N}"
  say ""
fi
say "  ${Y}Один токен — один бот.${N} Если он уже крутится на другой машине,"
say "  останови там, иначе Telegram будет отдавать 409 обоим."
say ""
