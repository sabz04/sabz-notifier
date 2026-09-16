#!/usr/bin/env bash
# Установщик sabz-notifier на macOS (локально, без sudo).
# Штатный bash в macOS — 3.2, поэтому без ${var,,}, без пустых массивов и
# без GNU-ключей вроде sed -i / date -Is.
set -euo pipefail

APP="sabz-notifier"
DIR="$HOME/.sabz-notifier"
LABEL="com.sabz.notifier"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
PW_PATH="${DIR}/ms-playwright"
PY_BIN="${DIR}/venv/bin/python3"
LOG="${DIR}/install.log"

GH_REPO="${GH_REPO:-sabz04/sabz-notifier}"
GH_BRANCH="${GH_BRANCH:-vps}"

_self="${BASH_SOURCE[0]:-}"
if [ -n "$_self" ] && [ -f "$_self" ]; then
  SRC="$(cd "$(dirname "$_self")" && pwd)"
else
  SRC=""
fi

TOKEN=""; OWNER=""; RECIPIENT=""
ASSUME_YES=0; UNINSTALL=0; PURGE=0; DO_START=1; FRESH=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; D=$'\e[2m'; N=$'\e[0m'; BD=$'\e[1m'
else R=""; G=""; Y=""; B=""; D=""; N=""; BD=""; fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s%s%s\n' "$B" "$N" "$BD" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
sub()  { printf '  %s%s%s\n' "$D" "$*" "$N"; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

ask() {
  local a=""
  if [ -t 0 ]; then read -r -p "$1" a
  elif [ -r /dev/tty ]; then read -r -p "$1" a </dev/tty
  fi
  printf '%s' "$a"
}
can_ask() { [ -t 0 ] || [ -r /dev/tty ]; }

usage() {
  cat <<EOF
${BD}sabz-notifier${N} — уведомления Avito и VK в Telegram. Установка на macOS.

  ./install-mac.sh [ключи]

${BD}Ключи${N}
  --token TOKEN     токен бота от @BotFather
  --owner NAME      кто управляет ботом: @username или числовой id
  --recipient ID    кому слать уведомления (по умолчанию — владельцу)
  --no-start        установить, но не запускать
  --fresh           переустановить с нуля, стерев и данные
  -y, --yes         не задавать вопросов
  --uninstall       удалить, данные оставить
  --purge           снести подчистую, вместе с данными и браузером
  -h, --help        эта справка

${BD}Прямо с гитхаба${N}
  curl -fsSL https://raw.githubusercontent.com/${GH_REPO}/${GH_BRANCH}/install-mac.sh | bash

${D}Без sudo: бот работает от твоего пользователя, чтобы видеть экран.${N}
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --token) TOKEN="${2:-}"; shift 2;;
    --owner) OWNER="${2:-}"; shift 2;;
    --recipient|--to) RECIPIENT="${2:-}"; shift 2;;
    --no-start) DO_START=0; shift;;
    --fresh) FRESH=1; shift;;
    -y|--yes) ASSUME_YES=1; shift;;
    --uninstall) UNINSTALL=1; shift;;
    --purge) PURGE=1; shift;;
    --repo) GH_REPO="${2:-}"; shift 2;;
    --branch) GH_BRANCH="${2:-}"; shift 2;;
    -h|--help) usage; exit 0;;
    *) die "неизвестный ключ: $1 (--help)";;
  esac
done

[ "$(uname -s)" = "Darwin" ] || die "это установщик для macOS. Для сервера — install.sh"
[ "$(id -u)" != "0" ] || die "запускай без sudo: бот должен работать от твоего пользователя"

mkdir -p "$DIR/data"
: > /dev/null
touch "$LOG" 2>/dev/null || true

# ------------------------------------------------------------------ удаление
if [ "$UNINSTALL" = "1" ] || [ "$PURGE" = "1" ]; then
  step "Удаляю ${APP}"
  launchctl unload -w "$PLIST" 2>/dev/null || true
  launchctl remove "$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  ok "автозапуск снят, бот остановлен"
  if [ "$PURGE" = "1" ]; then
    rm -rf "$DIR"
    ok "каталог ${DIR} удалён вместе с данными, профилем и браузером"
    say ""; say "${G}Удалено полностью.${N}"
  else
    rm -f "$DIR/bot.py" "$DIR/.env"
    ok "данные сохранены в ${DIR}/data"
    say ""; say "${G}Готово.${N} Снести подчистую: ./install-mac.sh --purge"
  fi
  exit 0
fi

say ""
say "${BD}  sabz-notifier${N} ${D}— уведомления Avito и VK в Telegram (macOS)${N}"
say ""

# ---------------------------------------------------------------- исходники
if [ -z "$SRC" ] || [ ! -f "$SRC/bot.py" ]; then
  step "Исходники"
  command -v curl >/dev/null 2>&1 || die "нужен curl"
  SRC="$(mktemp -d)"
  curl -fSL --progress-bar \
    "https://raw.githubusercontent.com/${GH_REPO}/${GH_BRANCH}/bot.py" \
    -o "$SRC/bot.py" || die "не скачался bot.py"
  ok "получен bot.py ($(wc -c <"$SRC/bot.py" | tr -d ' ') байт)"
fi

# ------------------------------------------------------------------ вводные
if [ -z "$TOKEN" ] && [ -f "$DIR/.env" ]; then
  TOKEN="$(grep '^TG_TOKEN=' "$DIR/.env" | cut -d= -f2- || true)"
  [ -n "$TOKEN" ] && ok "токен взят из прошлой установки"
fi
if [ -z "$TOKEN" ]; then
  can_ask || die "нужен --token"
  TOKEN="$(ask "  Токен бота от @BotFather: ")"
fi
case "$TOKEN" in *:*) ;; *) die "токен не похож на токен (ожидается 123456:AA...)";; esac

if [ -z "$OWNER" ] && [ -f "$DIR/.env" ]; then
  OWNER="$(grep '^TG_OWNER=' "$DIR/.env" | cut -d= -f2- || true)"
fi
if [ -z "$RECIPIENT" ] && [ -f "$DIR/.env" ]; then
  RECIPIENT="$(grep '^TG_RECIPIENT=' "$DIR/.env" | cut -d= -f2- || true)"
fi
if [ -z "$OWNER" ]; then
  can_ask || die "нужен --owner"
  say ""
  sub "Владелец — ТВОЙ аккаунт в Telegram, а не имя бота."
  OWNER="$(ask "  Твой @username или числовой id: ")"
fi
OWNER="${OWNER#@}"
if [ -z "$RECIPIENT" ]; then
  if can_ask; then
    RECIPIENT="$(ask "  Кому слать уведомления (Enter — себе): ")"
  fi
  [ -z "$RECIPIENT" ] && RECIPIENT="@${OWNER}"
fi
case "$RECIPIENT" in
  ""|@*) ;;
  *[!0-9-]*) RECIPIENT="@${RECIPIENT}";;
esac

BOT_USERNAME="$(curl -fsS --max-time 20 "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
if [ -n "$BOT_USERNAME" ]; then
  ok "бот найден: @${BOT_USERNAME}"
  if [ "$(lower "$OWNER")" = "$(lower "$BOT_USERNAME")" ]; then
    die "владелец — это твой аккаунт в Telegram, а не имя бота (@${BOT_USERNAME})"
  fi
else
  warn "Telegram не ответил на проверку токена — продолжаю, но проверь его"
fi

say ""
say "  владелец:   ${BD}${OWNER}${N}"
say "  получатель: ${BD}${RECIPIENT}${N}"
say "  каталог:    ${DIR}"
say ""
if [ "$ASSUME_YES" != "1" ] && can_ask; then
  a="$(ask "  Продолжаем? [Y/n] ")"
  case "${a:-y}" in [Nn]*) say "отменено"; exit 0;; esac
fi

# ------------------------------------------------------------------- python
step "Python"
command -v python3 >/dev/null 2>&1 \
  || die "нет python3. Поставь инструменты разработчика: xcode-select --install"
ok "python: $(python3 --version 2>&1)"

# ------------------------------------------------------- прежняя установка
step "Прежняя установка"
if [ -f "$PLIST" ]; then
  launchctl unload -w "$PLIST" 2>/dev/null || true
  ok "прежний экземпляр остановлен"
else
  sub "прежней установки нет"
fi
if [ "$FRESH" = "1" ] && [ -d "$DIR" ]; then
  rm -rf "$DIR"; mkdir -p "$DIR/data"
  ok "старые данные стёрты (--fresh)"
fi
rm -f "$DIR/bot.py"

# -------------------------------------------------------------------- файлы
step "Файлы"
cp "$SRC/bot.py" "$DIR/bot.py"
umask 077
cat > "$DIR/.env" <<EOF
TG_TOKEN=${TOKEN}
TG_OWNER=${OWNER}
TG_RECIPIENT=${RECIPIENT}
BOT_DATA=${DIR}/data
PLAYWRIGHT_BROWSERS_PATH=${PW_PATH}
PYTHONIOENCODING=utf-8
EOF
chmod 600 "$DIR/.env"
ok "bot.py и .env на месте"

# ------------------------------------------------------------------ браузер
step "Браузер"
if [ ! -x "$PY_BIN" ]; then
  python3 -m venv "$DIR/venv" >>"$LOG" 2>&1 || die "не создать venv"
  ok "создано окружение ${DIR}/venv"
else
  ok "окружение уже есть"
fi
if ! "$PY_BIN" -c "import playwright" >/dev/null 2>&1; then
  sub "ставлю playwright…"
  "$DIR/venv/bin/pip" install --quiet --upgrade pip >>"$LOG" 2>&1 || true
  "$DIR/venv/bin/pip" install playwright 2>&1 | tee -a "$LOG" \
    || die "не установить playwright"
fi
ok "playwright $("$PY_BIN" -c 'from importlib.metadata import version; print(version("playwright"))' 2>/dev/null || echo "установлен")"

mkdir -p "$PW_PATH"
if [ -d "/Applications/Google Chrome.app" ]; then
  ok "нашёл Google Chrome — использую его"
else
  sub "Chrome не найден, качаю chromium (это несколько минут)…"
  PLAYWRIGHT_BROWSERS_PATH="$PW_PATH" "$DIR/venv/bin/playwright" install chromium \
    2>&1 | tee -a "$LOG" || die "не удалось поставить chromium"
  ok "chromium установлен"
fi

sub "проверяю запуск браузера…"
if PLAYWRIGHT_BROWSERS_PATH="$PW_PATH" "$PY_BIN" - >>"$LOG" 2>&1 <<'PYEOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    for ch in ("chrome", None):
        try:
            b = pw.chromium.launch(channel=ch, headless=True)
            print(b.version)
            b.close()
            raise SystemExit(0)
        except SystemExit:
            raise
        except Exception:
            continue
    raise SystemExit(1)
PYEOF
then ok "браузер запускается"
else
  warn "браузер не стартанул, последние строки:"
  tail -6 "$LOG" 2>/dev/null | sed 's/^/      /'
  die "останавливаюсь — без рабочего браузера бот бесполезен"
fi

# ---------------------------------------------------------------- автозапуск
step "Автозапуск (launchd)"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY_BIN}</string>
    <string>-u</string>
    <string>${DIR}/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>${DIR}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>${DIR}/bot.log</string>
  <key>StandardErrorPath</key><string>${DIR}/bot.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>${HOME}</string>
    <key>PLAYWRIGHT_BROWSERS_PATH</key><string>${PW_PATH}</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
  </dict>
</dict>
</plist>
EOF
ok "агент ${LABEL}: запуск при входе, перезапуск при падении"

# ------------------------------------------------------------- самопроверка
step "Самопроверка"
"$PY_BIN" "$DIR/bot.py" --selfcheck 2>&1 | sed 's/^/  /' || warn "есть замечания (см. выше)"

# ---------------------------------------------------------------------- пуск
if [ "$DO_START" = "1" ]; then
  step "Запуск"
  launchctl unload -w "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST" 2>/dev/null || die "launchctl не смог загрузить агент"
  sleep 4
  if launchctl list 2>/dev/null | grep -q "$LABEL"; then ok "бот запущен"
  else warn "бот не поднялся — смотри ${DIR}/bot.log"; fi
else
  warn "не запускаю (--no-start). Старт: launchctl load -w ${PLIST}"
fi

say ""
say "${G}${BD}  Готово.${N}"
say ""
say "  ${BD}Осталось два шага:${N}"
say "    1. Напиши боту в Telegram ${BD}/start${N}"
say "    2. Через полминуты на экране появится окно браузера с вкладками"
say "       ${BD}Авито${N} и ${BD}ВК${N} — залогинься в них. Это нужно один раз."
say ""
say "  ${D}Закроешь окно — бот откроет его снова. Куки живут в ${DIR}/data.${N}"
say ""
say "  ${BD}Управление:${N}"
say "    launchctl unload -w ${PLIST}   ${D}остановить${N}"
say "    launchctl load -w ${PLIST}     ${D}запустить${N}"
say "    tail -f ${DIR}/bot.log         ${D}логи${N}"
say "    ./install-mac.sh --uninstall   ${D}удалить${N}"
say ""
say "  ${D}Настройки меняются прямо в Telegram: /settings${N}"
say ""
say "  ${Y}Один токен — один бот.${N} Если он крутится ещё где-то —"
say "  останови там, иначе Telegram будет отдавать 409 обоим."
say ""
