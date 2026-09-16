#!/usr/bin/env bash
# Установщик sabz-notifier на VPS. Идемпотентный: можно гонять повторно.
set -euo pipefail

APP="sabz-notifier"
DIR="/opt/${APP}"
SVC_USER="sabz"
UNIT="/etc/systemd/system/${APP}.service"
CLI="/usr/local/bin/${APP}"
LOG="/var/log/${APP}-install.log"

# откуда брать исходники, если скрипт запущен ссылкой: curl … | bash
GH_REPO="${GH_REPO:-sabz04/sabz-notifier}"
GH_BRANCH="${GH_BRANCH:-vps}"

# при запуске через pipe BASH_SOURCE указывает не на файл — тогда SRC пуст
_self="${BASH_SOURCE[0]:-}"
if [ -n "$_self" ] && [ -f "$_self" ]; then
  SRC="$(cd "$(dirname "$_self")" && pwd)"
else
  SRC=""
fi

TOKEN=""; OWNER=""; RECIPIENT=""; MODE="browser"; INTERVAL="25"
DO_SWAP=1; DO_START=1; ASSUME_YES=0; FORCE=0; UNINSTALL=0; PURGE=0; QUIET=0; FRESH=0
MODE_SET=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; D=$'\e[2m'; N=$'\e[0m'; BD=$'\e[1m'
else R=""; G=""; Y=""; B=""; D=""; N=""; BD=""; fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s%s%s\n' "$B" "$N" "$BD" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
sub()  { printf '  %s%s%s\n' "$D" "$*" "$N"; }

# вопрос пользователю: работает и когда скрипт пришёл по конвейеру (curl | bash)
ask() {
  local a=""
  if [ -t 0 ]; then read -r -p "$1" a
  elif [ -r /dev/tty ]; then read -r -p "$1" a </dev/tty
  fi
  printf '%s' "$a"
}
can_ask() { [ -t 0 ] || [ -r /dev/tty ]; }

# выполняет команду, показывая её вывод (прогресс закачки виден как есть)
show() {
  if [ "$QUIET" = "1" ]; then
    "$@" >>"$LOG" 2>&1
  else
    "$@" 2>&1 | tee -a "$LOG"
    return "${PIPESTATUS[0]}"
  fi
}

usage() {
  cat <<EOF
${BD}sabz-notifier${N} — уведомления о новых сообщениях Avito и VK в Telegram.

  sudo ./install.sh [ключи]

${BD}Ключи${N}
  --token TOKEN     токен бота от @BotFather
  --owner NAME      кто управляет ботом: @username или числовой id
  --recipient ID    кому слать уведомления: @username, @канал или id
                    ${D}(по умолчанию — тот же, кто владелец)${N}
  --mode browser    свой браузер, вход один раз  ${D}(по умолчанию)${N}
                    ${D}ничего доставать не надо: логинишься один раз${N}
  --mode light      без браузера, ~25 МБ RAM; доступ приносишь сам
                    ${D}(cURL для Авито, токен для ВК)${N}
  --interval SEC    период опроса, по умолчанию ${INTERVAL}
  --no-swap         не создавать swap на машине с малой памятью
  --no-start        установить, но не запускать
  --force           игнорировать предупреждения о ресурсах
  -q, --quiet       без подробного вывода (всё пишется в ${LOG})
  --repo O/R        репозиторий с исходниками ${D}(${GH_REPO})${N}
  --branch NAME     ветка ${D}(${GH_BRANCH})${N}
  -y, --yes         не задавать вопросов
  --uninstall       удалить бота (данные можно сохранить)
  --purge           снести подчистую: данные, профиль, браузер,
                    пользователя и swap, созданный установщиком
  --fresh           переустановить с нуля: стереть и данные тоже
                    ${D}(обычная переустановка их сохраняет)${N}
  -h, --help        эта справка

${BD}Примеры${N}
  sudo ./install.sh --token 123:AA... --owner sabzrr -y

  ${D}# прямо с гитхаба, без клонирования:${N}
  curl -fsSL https://raw.githubusercontent.com/${GH_REPO}/${GH_BRANCH}/install.sh \\
    | sudo bash -s -- --token 123:AA... --owner sabzrr
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2;;
    --owner) OWNER="${2:-}"; shift 2;;
    --recipient|--to) RECIPIENT="${2:-}"; shift 2;;
    --mode) MODE="${2:-}"; MODE_SET=1; shift 2;;
    --interval) INTERVAL="${2:-}"; shift 2;;
    --no-swap) DO_SWAP=0; shift;;
    --no-start) DO_START=0; shift;;
    --force) FORCE=1; shift;;
    -q|--quiet) QUIET=1; shift;;
    --repo) GH_REPO="${2:-}"; shift 2;;
    --branch) GH_BRANCH="${2:-}"; shift 2;;
    -y|--yes) ASSUME_YES=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    --purge) PURGE=1; shift;;
    --fresh) FRESH=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "неизвестный ключ: $1 (--help)";;
  esac
done

[ "$(id -u)" = "0" ] || die "нужен root: sudo ./install.sh"

# запуск команды от имени служебного пользователя
if command -v runuser >/dev/null 2>&1; then AS_USER=(runuser -u "$SVC_USER" --)
else AS_USER=(sudo -u "$SVC_USER"); fi

touch "$LOG" 2>/dev/null || LOG="/tmp/${APP}-install.log"
touch "$LOG" 2>/dev/null || true
chmod 600 "$LOG" 2>/dev/null || true
printf '\n===== %s =====\n' "$(date -Is)" >>"$LOG" 2>/dev/null || true

# ------------------------------------------------------------------ удаление
if [ "$UNINSTALL" = "1" ] || [ "$PURGE" = "1" ]; then
  step "Удаляю ${APP}"
  systemctl disable --now "${APP}.service" 2>/dev/null || true
  rm -f "$UNIT" "$CLI"; systemctl daemon-reload || true
  ok "служба и команда удалены"
  keep="y"
  if [ "$PURGE" = "1" ]; then
    keep="n"
  elif [ -d "$DIR" ] && [ "$ASSUME_YES" != "1" ] && can_ask; then
    a="$(ask "  Сохранить данные в ${DIR}/data (куки, история)? [Y/n] ")"
    case "${a:-y}" in [Nn]*) keep="n";; esac
  fi
  if [ "$keep" = "n" ]; then
    rm -rf "$DIR"; ok "каталог ${DIR} удалён вместе с данными и браузером"
  elif [ -d "$DIR" ]; then
    rm -f "$DIR"/bot.py "$DIR"/.env; ok "данные сохранены в ${DIR}/data"
  fi
  if [ "$PURGE" = "1" ]; then
    userdel -r "$SVC_USER" 2>/dev/null || true
    ok "пользователь ${SVC_USER} удалён"
    if grep -q '^/swapfile' /etc/fstab 2>/dev/null && [ -f /swapfile ]; then
      swapoff /swapfile 2>/dev/null || true
      sed -i '\#^/swapfile#d' /etc/fstab
      rm -f /swapfile
      ok "swapfile, созданный установщиком, убран"
    fi
    say ""
    say "${G}Удалено полностью.${N} Системные пакеты не трогал —"
    say "их могли ставить не мы, и от них могут зависеть другие сервисы."
  else
    say ""
    say "${G}Готово.${N} Данные, пользователь ${SVC_USER} и swap на месте."
    say "  ${D}снести подчистую: sudo ./install.sh --purge${N}"
  fi
  exit 0
fi

say ""
say "${BD}  sabz-notifier${N} ${D}— уведомления Avito и VK в Telegram${N}"
say ""

# ----------------------------------------------------------------- исходники
if [ -z "$SRC" ] || [ ! -f "$SRC/bot.py" ]; then
  step "Исходники"
  sub "bot.py рядом нет — забираю из ${GH_REPO}@${GH_BRANCH}"
  if ! command -v curl >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >>"$LOG" 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >>"$LOG" 2>&1 || true
  fi
  command -v curl >/dev/null 2>&1 || die "нужен curl"
  SRC="$(mktemp -d)"
  RAW="https://raw.githubusercontent.com/${GH_REPO}/${GH_BRANCH}"
  for f in bot.py sabzctl; do
    curl -fSL --progress-bar "$RAW/$f" -o "$SRC/$f" \
      || die "не скачался ${f} — проверь --repo/--branch и доступность репозитория"
    ok "получен ${f} ($(wc -c <"$SRC/$f") байт)"
  done
  chmod +x "$SRC/sabzctl"
fi

# ------------------------------------------------------------------- вводные

if [ -z "$TOKEN" ] && [ -f "$DIR/.env" ]; then
  TOKEN="$(grep -E '^TG_TOKEN=' "$DIR/.env" | cut -d= -f2- || true)"
  [ -n "$TOKEN" ] && ok "токен взят из установленного ранее .env"
fi
if [ -z "$TOKEN" ]; then
  can_ask || die "нужен --token (запуск без терминала)"
  TOKEN="$(ask "  Токен бота от @BotFather: ")"
fi
[[ "$TOKEN" == *:* ]] || die "токен не похож на токен (ожидается 123456:AA...)"

if [ -z "$OWNER" ] && [ -f "$DIR/.env" ]; then
  OWNER="$(grep -E '^TG_OWNER=' "$DIR/.env" | cut -d= -f2- || true)"
fi
if [ -z "$RECIPIENT" ] && [ -f "$DIR/.env" ]; then
  RECIPIENT="$(grep -E '^TG_RECIPIENT=' "$DIR/.env" | cut -d= -f2- || true)"
fi
# режим при обновлении не меняем молча: берём прежний, если не указан явно
if [ "$MODE_SET" = "0" ] && [ -f "$DIR/.env" ]; then
  _m="$(grep -E '^BOT_MODE=' "$DIR/.env" | cut -d= -f2- || true)"
  case "$_m" in light|browser) MODE="$_m"; ok "режим взят из прежней установки: $MODE";; esac
fi
if [ -z "$OWNER" ]; then
  can_ask || die "нужен --owner (запуск без терминала)"
  say ""
  sub "Владелец — ТВОЙ аккаунт в Telegram, а не имя бота."
  OWNER="$(ask "  Твой @username или числовой id: ")"
fi
OWNER="${OWNER#@}"

if [ -z "$RECIPIENT" ]; then
  if can_ask; then
    RECIPIENT="$(ask "  Кому слать уведомления (@username или id, Enter — себе): ")"
  fi
  [ -z "$RECIPIENT" ] && RECIPIENT="@${OWNER}"
fi
case "$RECIPIENT" in
  ""|@*) ;;
  *[!0-9-]*) RECIPIENT="@${RECIPIENT}";;
esac

# спрашиваем у Telegram имя бота: проверяем токен и ловим частую ошибку,
# когда владельцем вписывают самого бота
BOT_USERNAME=""
if command -v curl >/dev/null 2>&1; then
  BOT_USERNAME="$(curl -fsS --max-time 20 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
fi
if [ -n "$BOT_USERNAME" ]; then
  ok "бот найден: @${BOT_USERNAME}"
  if [ "${OWNER,,}" = "${BOT_USERNAME,,}" ]; then
    die "владелец — это твой аккаунт в Telegram, а не имя бота (@${BOT_USERNAME})"
  fi
  if [ "${RECIPIENT#@}" = "$BOT_USERNAME" ]; then
    die "получатель — человек или канал, а не сам бот (@${BOT_USERNAME})"
  fi
else
  warn "Telegram не ответил на проверку токена — продолжаю, но проверь его"
fi

case "$MODE" in light|browser) ;; *) die "--mode должен быть light или browser";; esac

RAM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/{printf "%d", $2/1024}' /proc/meminfo)

say ""
say "  режим:     ${BD}${MODE}${N}   опрос: ${BD}${INTERVAL}с${N}"
say "  владелец:  ${BD}${OWNER}${N}"
say "  получатель:${BD} ${RECIPIENT}${N}"
say "  каталог:   ${DIR}"
say "  память:    ${RAM_MB} МБ"
say ""
if [ "$ASSUME_YES" != "1" ] && can_ask; then
  a="$(ask "  Продолжаем? [Y/n] ")"
  case "${a:-y}" in [Nn]*) say "отменено"; exit 0;; esac
fi

# ------------------------------------------------------------------- пакеты
step "Системные пакеты"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  PKGS="python3 ca-certificates"
  [ "$MODE" = "browser" ] && PKGS="$PKGS python3-pip python3-venv xvfb x11vnc novnc websockify fonts-liberation"
  sub "обновляю список пакетов…"
  show apt-get update -q
  sub "ставлю: $PKGS"
  # shellcheck disable=SC2086
  show apt-get install -y $PKGS || die "apt не смог поставить пакеты (подробности в $LOG)"
  ok "пакеты готовы"
elif command -v dnf >/dev/null 2>&1; then
  show dnf install -y python3 ca-certificates || die "dnf не смог поставить пакеты"
  ok "пакеты готовы"
else
  warn "неизвестный пакетный менеджер — проверь, что есть python3"
fi
command -v python3 >/dev/null 2>&1 || die "python3 не найден"
ok "python: $(python3 --version 2>&1)"

# --------------------------------------------------------------------- swap
step "Память и swap"
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

# Режим решаем ЗДЕСЬ, а не раньше: swap уже создан и его надо учесть,
# иначе на чистой машине браузерный режим откатывался бы зря.
SWAP_MB=$(awk '/SwapTotal/{printf "%d", $2/1024}' /proc/meminfo)
EFFECTIVE_MB=$((RAM_MB + SWAP_MB))
if [ "$MODE" = "browser" ]; then
  if [ "$EFFECTIVE_MB" -lt 1500 ] && [ "$FORCE" != "1" ]; then
    warn "${RAM_MB} МБ RAM и ${SWAP_MB} МБ swap — браузер тут не выживет."
    warn "ставлю лёгкий режим. Настоять можно ключом --force."
    MODE="light"
  elif [ "$RAM_MB" -lt 1800 ]; then
    warn "${RAM_MB} МБ RAM — браузер будет жить за счёт swap (${SWAP_MB} МБ),"
    warn "работать будет, но неспешно."
  fi
fi

PW_PATH="${DIR}/ms-playwright"
if [ "$MODE" = "browser" ]; then PY_BIN="${DIR}/venv/bin/python3"
else PY_BIN="/usr/bin/python3"; fi

# ------------------------------------------------------------- пользователь
step "Пользователь и каталоги"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd -r -m -d "$DIR" -s /usr/sbin/nologin "$SVC_USER"
mkdir -p "$DIR/data"
ok "пользователь ${SVC_USER}, каталог ${DIR}"

# -------------------------------------------------------------------- файлы
step "Прежняя установка"
if [ -f "$UNIT" ] || systemctl is-active --quiet "${APP}.service" 2>/dev/null; then
  systemctl stop "${APP}.service" 2>/dev/null || true
  ok "прежний экземпляр остановлен"
else
  sub "прежней установки нет"
fi
if [ "$FRESH" = "1" ] && [ -d "$DIR" ]; then
  rm -rf "$DIR"; mkdir -p "$DIR/data"
  chown -R "$SVC_USER":"$SVC_USER" "$DIR" 2>/dev/null || true
  ok "старые данные стёрты (--fresh)"
fi
rm -f "$DIR/bot.py" 2>/dev/null || true
rm -rf "$DIR/__pycache__" 2>/dev/null || true

step "Файлы бота"
install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$SRC/bot.py" "$DIR/bot.py"
PW_LINE=""
[ "$MODE" = "browser" ] && PW_LINE="PLAYWRIGHT_BROWSERS_PATH=${PW_PATH}"
umask 077
cat > "$DIR/.env" <<EOF
TG_TOKEN=${TOKEN}
TG_OWNER=${OWNER}
TG_RECIPIENT=${RECIPIENT}
BOT_MODE=${MODE}
BOT_DATA=${DIR}/data
BOT_HEADLESS=$([ "$MODE" = "browser" ] && echo 1 || echo "")
${PW_LINE}
PYTHONIOENCODING=utf-8
EOF
chown root:"$SVC_USER" "$DIR/.env"; chmod 640 "$DIR/.env"
chown -R "$SVC_USER":"$SVC_USER" "$DIR/data"
ok "bot.py и .env на месте (.env читает только служба)"

# ---------------------------------------------------------------- playwright
if [ "$MODE" = "browser" ]; then
  step "Браузер для бота"

  # своё окружение, чтобы не трогать системный python
  if [ ! -x "$PY_BIN" ]; then
    python3 -m venv "$DIR/venv" >/dev/null 2>&1 || die "не создать venv (нужен пакет python3-venv)"
    ok "создано окружение ${DIR}/venv"
  else ok "окружение уже есть"; fi
  show "$DIR/venv/bin/pip" install --upgrade pip || true
  if ! "$PY_BIN" -c "import playwright" 2>/dev/null; then
    sub "ставлю playwright…"
    show "$DIR/venv/bin/pip" install playwright || die "не установить playwright"
  fi
  ok "playwright $("$PY_BIN" -c 'from importlib.metadata import version; print(version("playwright"))' 2>/dev/null || echo "установлен")"

  # браузер кладём в предсказуемое место внутри каталога бота
  mkdir -p "$PW_PATH"
  say "  ${D}качаю chromium и системные библиотеки (это долго на слабой машине)…${N}"
  PW_OK=0
  # --with-deps ставит недостающие библиотеки через apt — поэтому от root
  for attempt in \
      "$DIR/venv/bin/playwright install --with-deps chromium" \
      "$PY_BIN -m playwright install --with-deps chromium" \
      "$DIR/venv/bin/playwright install chromium"; do
    rc=0
    # shellcheck disable=SC2086
    if [ "$QUIET" = "1" ]; then
      PLAYWRIGHT_BROWSERS_PATH="$PW_PATH" $attempt >>"$LOG" 2>&1 || rc=$?
    else
      PLAYWRIGHT_BROWSERS_PATH="$PW_PATH" $attempt 2>&1 | tee -a "$LOG" || rc=$?
    fi
    [ "$rc" = "0" ] && { PW_OK=1; break; } || true
  done
  if [ "$PW_OK" = "1" ]; then ok "chromium и зависимости установлены"
  else
    warn "не удалось поставить chromium. Последние строки из ${LOG}:"
    tail -8 "$LOG" 2>/dev/null | sed 's/^/      /'
    die "останавливаюсь — без браузера режим browser не заработает"
  fi
  chown -R "$SVC_USER":"$SVC_USER" "$PW_PATH" "$DIR/venv"

  # настоящая проверка: реально ли запускается браузер от имени службы
  say "  ${D}проверяю запуск браузера…${N}"
  if "${AS_USER[@]}" env HOME="$DIR" PLAYWRIGHT_BROWSERS_PATH="$PW_PATH" \
      "$PY_BIN" - >/tmp/pw_smoke.log 2>&1 <<'PYEOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True,
                           args=["--no-sandbox", "--disable-dev-shm-usage"])
    print(b.version)
    b.close()
PYEOF
  then ok "браузер запускается: $(tail -1 /tmp/pw_smoke.log)"
  else
    warn "браузер не стартует. Последние строки:"
    tail -6 /tmp/pw_smoke.log 2>/dev/null | sed 's/^/      /'
    die "останавливаюсь — режим browser не готов"
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
ExecStart=${PY_BIN} -u ${DIR}/bot.py
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
"${AS_USER[@]}" env HOME="$DIR" "$PY_BIN" "$DIR/bot.py" --selfcheck 2>&1 | sed 's/^/  /'
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
say "  ${BD}Всё управление одной командой:${N}"
say ""
say "      ${BD}${APP}${N}"
say ""
say "  ${D}откроется меню: статус, логи, перезапуск, настройки, удаление${N}"
say ""
say "  ${BD}В Telegram:${N} /settings — настройки и что меняется прямо из чата"
say "  ${D}лог установки: ${LOG}${N}"
say ""
if [ "$MODE" = "light" ]; then
  say "  ${Y}Важно:${N} Авито блокирует зарубежные дата-центры (HTTP 429)."
  say "  Если сервер не в РФ — задай боту прокси: ${BD}/proxy avito http://user:pass@host:port${N}"
  say ""
fi
say "  ${Y}Один токен — один бот.${N} Если он уже крутится на другой машине,"
say "  останови там, иначе Telegram будет отдавать 409 обоим."
say ""
