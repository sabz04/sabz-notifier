#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sabzNotifierBot - лёгкий уведомитель о новых сообщениях (Avito, VK) в Telegram.
Только stdlib. Один процесс, два потока.
"""
import os
import re
import sys
import ssl
import json
import gzip
import zlib
import time
import html
import threading
import traceback
import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Подхватывает .env рядом с ботом, не перетирая заданные переменные."""
    try:
        with open(os.path.join(HERE, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k.strip():
                    os.environ.setdefault(k.strip(), v)
    except OSError:
        pass


_load_dotenv()

DATA = os.environ.get("BOT_DATA", os.path.join(HERE, "data"))
CONFIG_PATH = os.path.join(DATA, "config.json")
STATE_PATH = os.path.join(DATA, "state.json")

TG_TOKEN = os.environ.get("TG_TOKEN", "")
# кто управляет ботом: @username или числовой id
OWNER = os.environ.get("TG_OWNER", "").strip().lstrip("@").lower()
# куда слать уведомления: @username, @канал или числовой id.
# пусто — шлём туда, откуда владелец написал /start
RECIPIENT = os.environ.get("TG_RECIPIENT", "").strip()
TG_API = "https://api.telegram.org/bot%s/%s"


def _flag(name, default=""):
    return os.environ.get(name, default).strip().lower() \
        not in ("", "0", "false", "no", "off")


HEADLESS = _flag("BOT_HEADLESS")
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
# есть живой экран: окно браузера можно показать как есть
HAS_DESKTOP = IS_WINDOWS or IS_MAC

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

_lock = threading.Lock()
_log_lock = threading.Lock()


def log(*a):
    with _log_lock:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- storage ---
def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))


def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


DEFAULT_CFG = {
    "chat_id": None,
    "interval": 25,
    "muted": False,
    "ignore": [],
    "sources": {},
}

cfg = _load(CONFIG_PATH, DEFAULT_CFG)
for _k, _v in DEFAULT_CFG.items():
    cfg.setdefault(_k, _v)
state = _load(STATE_PATH, {})


def save_cfg():
    with _lock:
        _save(CONFIG_PATH, cfg)


def save_state():
    with _lock:
        _save(STATE_PATH, state)


# ------------------------------------------------------------------ http ---
_ctx = ssl.create_default_context()


def http(url, headers=None, data=None, method=None, proxy=None, timeout=30):
    """Возвращает (status, body_bytes, final_url). Не кидает на HTTP-ошибках."""
    h = dict(headers or {})
    h.setdefault("User-Agent", UA)
    h.setdefault("Accept-Encoding", "gzip, deflate")
    if isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h,
                                 method=method or ("POST" if data else "GET"))
    handlers = [urllib.request.HTTPSHandler(context=_ctx)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy,
                                                     "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        r = opener.open(req, timeout=timeout)
        status, raw, furl = r.getcode(), r.read(), r.geturl()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as e:
        status, raw, furl = e.code, e.read(), url
        enc = (e.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    return status, raw, furl


def http_json(*a, **kw):
    st, raw, furl = http(*a, **kw)
    try:
        return st, json.loads(raw.decode("utf-8", "replace")), furl
    except Exception:
        return st, None, furl


# -------------------------------------------------------------- telegram ---
def tg(method, **params):
    url = TG_API % (TG_TOKEN, method)
    flat = {}
    for k, v in params.items():
        if v is None:
            continue
        flat[k] = json.dumps(v) if isinstance(v, (dict, list)) else v
    body = urllib.parse.urlencode(flat).encode()
    st, js, _ = http_json(
        url, data=body, timeout=70,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return js or {"ok": False, "status": st}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def _norm_target(v):
    """'@name' / 'name' / '-100123' -> то, что понимает Telegram."""
    v = str(v).strip()
    if not v:
        return None
    if v.lstrip("-").isdigit():
        return int(v)
    return v if v.startswith("@") else "@" + v


def eff_recipient():
    """Получатель: заданный командой важнее записанного в .env."""
    return (cfg.get("recipient") or RECIPIENT or "").strip()


def notify_target():
    """Куда шлём уведомления.

    Числовой id — берём как есть. Для @username Telegram умеет доставлять
    только в канал/супергруппу, поэтому для обычного человека используем
    id, который бот запомнил, когда тот написал ему /start.
    """
    r = eff_recipient()
    if not r:
        return cfg.get("chat_id")
    if r.lstrip("-").isdigit():
        return int(r)
    rid = cfg.get("recipient_chat_id")
    if rid:
        return rid
    return "@" + r.lstrip("@")


def is_owner(frm):
    """Владелец задаётся как @username или как числовой id."""
    if not OWNER:
        return True
    uname = (frm.get("username") or "").lower()
    return uname == OWNER or str(frm.get("id")) == OWNER


def send(text, preview=False, chat_id=None):
    cid = chat_id or notify_target()
    if not cid:
        log("send: получатель неизвестен — напиши боту /start")
        return
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        r = tg("sendMessage", chat_id=cid, text=chunk, parse_mode="HTML",
               disable_web_page_preview=(not preview))
        if not r.get("ok"):
            log("telegram error:", r)


# ------------------------------------------------------------ curl parse ---
# ------------------------------------------------------- generic message ---
SYSTEM_NAMES = (
    "авито", "avito", "служба поддержки", "поддержка", "техподдержка",
    "помощник", "ассистент", "бот", "модерац", "доставка авито",
    "авито доставка", "безопасная сделка", "команда авито", "сервис",
    "уведомлени", "автоответ", "робот",
)
SYSTEM_MSG_TYPES = ("system", "service", "deleted", "appcall", "platform")


def is_system_sender(m):
    """True, если это не живой собеседник."""
    if m.get("official"):
        return True
    if m.get("msg_type", "").lower() in SYSTEM_MSG_TYPES:
        return True
    aid = m.get("author_id") or ""
    # 0/-1/отрицательные id — служебные; пустой id (браузерный режим) — норм
    if aid in ("0", "-1") or aid.startswith("-"):
        return True
    name = (m.get("name") or "").strip().lower()
    for bad in SYSTEM_NAMES:
        if bad in name:
            return True
    for bad in (cfg.get("ignore") or []):
        b = str(bad).strip().lower()
        if b and (b in name or b in (m.get("text") or "").lower()):
            return True
    return False


# --------------------------------------------------------------- errors ---
class SessionDead(Exception):
    pass


class Blocked(Exception):
    """Сайт показал проверку «вы не робот» — её проходит человек, не бот."""
    pass


def blocked_hint():
    """Что делать владельцу, чтобы снять проверку «вы не робот»."""
    return ("Проверку проходит человек, не бот. Пришли мне "
            "<code>/login</code> — я открою окно и дам ссылку, "
            "там и нажмёшь кнопку.\n\n"
            "<i>Проходить надо с того же адреса, откуда хожу я, — "
            "из своего браузера не поможет.</i>")


class Transient(Exception):
    pass


# ----------------------------------------------------------------- source ---
class Source(object):
    def __init__(self, name, conf):
        self.name = name
        self.conf = conf

    def st(self):
        return state.setdefault(self.name, {"seen": {}, "primed": False})


BROWSER_DIR = os.path.join(DATA, "browser")

# Виртуальный экран вместо headless: сайту браузер виден обычным, просто
# показывать окно некуда. Headless палится по десятку признаков, это — нет.
XVFB = {"proc": None, "display": ":98"}


def ensure_xvfb():
    """Поднимает Xvfb один раз на весь процесс. Возвращает :display или None."""
    if HAS_DESKTOP:
        return None
    p = XVFB.get("proc")
    if p is not None and p.poll() is None:
        return XVFB["display"]
    if not _which("Xvfb"):
        log("Xvfb не найден — браузер пойдёт в headless")
        return None
    import subprocess
    try:
        proc = subprocess.Popen(
            ["Xvfb", XVFB["display"], "-screen", "0", "1280x900x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        if proc.poll() is not None:
            log("Xvfb не запустился")
            return None
        XVFB["proc"] = proc
        log("виртуальный экран %s поднят" % XVFB["display"])
        return XVFB["display"]
    except Exception as e:
        log("Xvfb: %s" % e)
        return None


def _dbg_dump(site, obj, tag=""):
    """Разово сохраняет структуру ответа, чтобы подстроить разбор."""
    p = os.path.join(DATA, "debug_%s%s.json" % (site, tag))
    if os.path.exists(p):
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


class BrowserHub(object):
    """Один постоянный профиль Chromium на все браузерные источники."""
    _inst = None

    def __init__(self):
        self._pw = None
        self.ctx = None
        self.pages = {}
        self.headless_override = None   # на время входа окно должно быть видно

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = BrowserHub()
        return cls._inst

    def ensure(self):
        if self.ctx is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            raise Transient("playwright не установлен (pip install playwright)")
        os.makedirs(BROWSER_DIR, exist_ok=True)
        self._pw = sync_playwright().start()
        # системный Chrome/Edge имеют нужные библиотеки; bundled Chromium
        # на этой машине падает с SxS-ошибкой, поэтому он последним
        prefer = cfg.get("browser_channel")
        if IS_WINDOWS:
            order = ("chrome", "msedge", None)
        elif IS_MAC:
            order = ("chrome", None, "msedge")
        else:
            order = (None, "chromium", "chrome")
        # None в списке — это скачанный chromium, и он часто единственный
        # рабочий. Раньше фильтр по prefer выбрасывал именно его.
        channels = []
        if prefer:
            channels.append(prefer)
        for c in order:
            if c not in channels:
                channels.append(c)
        args = ["--disable-blink-features=AutomationControlled"]
        if not HAS_DESKTOP:
            # на сервере часто root и маленький /dev/shm
            args += ["--no-sandbox", "--disable-dev-shm-usage"]

        want_headless = (HEADLESS if self.headless_override is None
                         else self.headless_override)
        # окна не нужно — но лучше обычный браузер на виртуальном экране,
        # чем headless: последний слишком легко узнать
        if want_headless:
            disp = ensure_xvfb()
            if disp:
                os.environ["DISPLAY"] = disp
                want_headless = False
        last = None
        for ch in channels:
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(
                    BROWSER_DIR,
                    channel=ch,
                    headless=want_headless,
                    viewport={"width": 1280, "height": 880},
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    user_agent=UA,
                    args=args,
                )
                if cfg.get("browser_channel") != ch:
                    cfg["browser_channel"] = ch
                    save_cfg()
                log("браузер запущен, channel=%s, профиль %s"
                    % (ch or "bundled", BROWSER_DIR))
                break
            except Exception as e:
                last = e
                log("channel=%s не пошёл: %s" % (ch, str(e)[:100]))
        if self.ctx is None:
            # playwright обязательно остановить: иначе в этом потоке остаётся
            # живой цикл asyncio, и все следующие попытки падают навсегда
            # с «Sync API inside the asyncio loop»
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None
            raise Transient("не удалось запустить браузер: %s" % str(last)[:120])
        self.ctx.set_default_timeout(45000)
        self.ctx.set_default_navigation_timeout(60000)

    def page(self, name):
        self.ensure()
        pg = self.pages.get(name)
        if pg is not None and not pg.is_closed():
            return pg
        # переиспользуем стартовую пустую вкладку под первый источник
        pg = None
        for p in self.ctx.pages:
            if p not in self.pages.values() and (p.url in ("about:blank", "")):
                pg = p
                break
        if pg is None:
            pg = self.ctx.new_page()
        self.pages[name] = pg
        return pg

    def reset(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.ctx = None
        self._pw = None
        self.pages = {}


# ------------------------------------------------------------ вход в окно ---
# На сервере окна нет, поэтому поднимаем виртуальный экран и отдаём его в
# браузер по ссылке. Всё это живёт только на время входа.
LOGIN = {"want": False, "stop": False, "until": 0, "procs": [], "url": None}
NOVNC_DIRS = ("/usr/share/novnc", "/usr/share/webapps/novnc")


def _which(name):
    from shutil import which
    return which(name)


def _public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            st, raw, _ = http(url, timeout=8)
            ip = raw.decode("ascii", "ignore").strip()
            if st == 200 and ip and len(ip) < 46:
                return ip
        except Exception:
            pass
    return None


def stop_login_session(quiet=False):
    import subprocess                                    # noqa: F401
    for p in LOGIN.get("procs") or []:
        try:
            p.terminate()
        except Exception:
            pass
    LOGIN["procs"] = []
    LOGIN["until"] = 0
    LOGIN["url"] = None
    os.environ.pop("DISPLAY", None)
    hub = BrowserHub.get()
    hub.headless_override = None
    hub.reset()
    if not quiet:
        send("🔒 Окно входа закрыто, доступ снаружи больше не открыт. "
             "Продолжаю следить.")
    log("сессия входа закрыта")


def start_login_session():
    """Поднимает виртуальный экран и отдаёт ссылку на него в Telegram."""
    import subprocess
    import secrets

    missing = [n for n in ("Xvfb", "x11vnc", "websockify") if not _which(n)]
    if missing:
        send("Не могу открыть окно: на сервере нет %s.\nПереустанови бота — "
             "установщик их ставит." % esc(", ".join(missing)))
        return
    novnc = next((d for d in NOVNC_DIRS if os.path.isdir(d)), None)
    if not novnc:
        send("Не могу открыть окно: не найден novnc. Переустанови бота.")
        return

    stop_login_session(quiet=True)
    BrowserHub.get().reset()

    tmpdir = os.path.join(DATA, "login")
    os.makedirs(tmpdir, exist_ok=True)
    pw = secrets.token_hex(4)
    pwfile = os.path.join(tmpdir, "vncpw")
    devnull = subprocess.DEVNULL
    try:
        subprocess.run(["x11vnc", "-storepasswd", pw, pwfile],
                       stdout=devnull, stderr=devnull, timeout=20)
        os.chmod(pwfile, 0o600)
        procs = [subprocess.Popen(["Xvfb", ":99", "-screen", "0",
                                   "1280x900x24"],
                                  stdout=devnull, stderr=devnull)]
        time.sleep(2)
        procs.append(subprocess.Popen(
            ["x11vnc", "-display", ":99", "-rfbauth", pwfile,
             "-forever", "-shared", "-quiet"], stdout=devnull, stderr=devnull))
        time.sleep(1)
        procs.append(subprocess.Popen(
            ["websockify", "--web", novnc, "0.0.0.0:6080", "127.0.0.1:5900"],
            stdout=devnull, stderr=devnull))
        time.sleep(1)
        LOGIN["procs"] = procs
        for p in procs:
            if p.poll() is not None:
                raise RuntimeError("процесс экрана не поднялся")
    except Exception as e:
        log("login: %s" % e)
        stop_login_session(quiet=True)
        send("Не смог поднять окно входа: %s" % esc(e))
        return

    os.environ["DISPLAY"] = ":99"
    hub = BrowserHub.get()
    hub.headless_override = False
    try:
        pg = hub.page("avito")
        pg.goto("https://www.avito.ru/profile/messenger",
                wait_until="domcontentloaded")
        vk = hub.page("vk")
        vk.goto("https://vk.ru/im", wait_until="domcontentloaded")
    except Exception as e:
        log("login pages: %s" % e)

    ip = _public_ip() or "IP_СЕРВЕРА"
    url = "http://%s:6080/vnc.html?autoconnect=true&password=%s" % (ip, pw)
    LOGIN["url"] = url
    LOGIN["until"] = time.time() + 1800
    send("🖥 <b>Окно открыто — жми сюда:</b>\n\n%s\n\n"
         "Увидишь две вкладки: <b>Авито</b> и <b>ВК</b>. Залогинься в обеих. "
         "Если Авито попросит подтвердить, что ты не робот — нажми кнопку "
         "там же.\n\nКогда закончишь, пришли <code>/login stop</code>. "
         "Само закроется через 30 минут.\n\n"
         "<i>Ссылка одноразовая, пароль в ней новый каждый раз, и доступ "
         "снаружи открыт только пока идёт вход.</i>" % url, preview=False)
    log("сессия входа открыта: %s" % url)


def _pw_is_closed_error(e):
    s = str(e).lower()
    return ("target closed" in s or "has been closed" in s
            or "browser has disconnected" in s or "closed" in s
            and "page" in s)


AVITO_EXTRACT_JS = r"""
() => {
  const links = document.querySelectorAll('a[data-marker="channels/channelLink"]');
  const rows = [];
  links.forEach(a => {
    const href = a.getAttribute('href') || '';
    const id = href.split('/channel/')[1] || '';
    if (!id) return;
    const q = (m) => { const e = a.querySelector('[data-marker="'+m+'"]');
                       return e ? (e.innerText||'').replace(/\s+/g,' ').trim() : ''; };
    const ch = a.querySelector('[data-marker="channels/channel"]');
    const read = ch ? ch.getAttribute('data-read') : null;
    const outgoing = !!a.querySelector('[data-marker^="icon/messenger-status"]');
    rows.push({ id: id, type: (id.split('-')[0] || ''),
                name: q('channels/user-title'),
                item: q('channels/item-title'),
                text: q('channels/last-message'),
                time: q('channels/channel-datetime'),
                unread: read === 'false', outgoing: outgoing });
  });
  const bt = document.body ? document.body.innerText : '';
  const ttl = document.title || '';
  return { rows: rows,
           logged: !!document.querySelector('[data-marker^="channels/"]'),
           login: /Войти в личный кабинет|Войти по паролю|Введите телефон/.test(bt),
           blocked: /Доступ ограничен|проблема с IP|не робот|Проверка безопасности|подозрительн/i
                      .test(bt + ' ' + ttl) };
}
"""

_AVITO_SYS_TEXT = re.compile(
    r"создал чат|посмотрел номер|пока ничего не написал|чат создан|"
    r"показ телефона|поделился контакт|отправил файл$", re.I)


class BrowserAvitoSource(Source):
    """Список чатов Avito из DOM своего залогиненного браузера.
    DOM живёт по WebSocket, поэтому страницу перегружаем редко."""

    def poll(self):
        hub = BrowserHub.get()
        try:
            pg = hub.page("avito")
            if self.st().get("relogin"):
                self.st()["relogin"] = False
                try:
                    pg.bring_to_front()
                except Exception:
                    pass
            self._goto_if_needed(pg)
            res = pg.evaluate(AVITO_EXTRACT_JS)
            if not res.get("rows") and not res.get("logged"):
                pg.wait_for_timeout(2500)
                res = pg.evaluate(AVITO_EXTRACT_JS)
        except Exception as e:
            if _pw_is_closed_error(e):
                hub.reset()
                raise Transient("окно браузера закрылось — переоткрываю")
            raise Transient("браузер Avito: %s" % str(e)[:120])
        if res.get("blocked"):
            try:
                pg.bring_to_front()
            except Exception:
                pass
            raise Blocked("Авито показывает проверку «подтвердите, что вы не робот»")
        if not res.get("logged"):
            if res.get("login"):
                raise SessionDead("нужно войти в Авито в окне браузера бота")
            raise Transient("мессенджер ещё грузится")
        out = []
        for r in res.get("rows", []):
            cid = r.get("id") or ""
            if not cid:
                continue
            typ = r.get("type") or ""
            text = r.get("text") or ""
            official = (typ == "a2u") or bool(_AVITO_SYS_TEXT.search(text))
            out.append({
                "chat_id": cid,
                "msg_id": (text + "|" + (r.get("time") or ""))[:120] or cid,
                "author_id": "", "self_id": "",
                "name": r.get("name") or "Собеседник",
                "title": r.get("item") or "",
                "text": text, "created": 0,
                "direction": "out" if r.get("outgoing") else "in",
                "unread": 1 if r.get("unread") else 0,
                "msg_type": "", "chat_type": typ,
                "official": official,
            })
        return out

    def _goto_if_needed(self, pg):
        need = "profile/messenger" not in (pg.url or "")
        if not need:
            try:
                need = not pg.evaluate(
                    "() => !!document.querySelector("
                    "'a[data-marker=\"channels/channelLink\"]')")
            except Exception:
                need = True
        if not need and (time.time() - self.st().get("nav_ts", 0)) > 1200:
            need = True
        if need:
            pg.goto("https://www.avito.ru/profile/messenger",
                    wait_until="domcontentloaded")
            pg.wait_for_timeout(6000)
            self.st()["nav_ts"] = time.time()

    def link(self, chat_id):
        return "https://www.avito.ru/profile/messenger/channel/%s" % chat_id


VK_EXTRACT_JS = r"""
() => {
  const rows = [];
  const items = document.querySelectorAll(
    '[data-testid="vkme_convo_list_item"], button.ConvoListItem[data-peer-id]');
  items.forEach(it => {
    const peer = it.getAttribute('data-peer-id') || '';
    if (!peer) return;
    const img = it.querySelector('.ConvoListItem__avatar img, img[alt]');
    let name = img ? (img.getAttribute('alt') || '').trim() : '';
    let full = (it.innerText || '').replace(/\s+/g, ' ').trim();
    if (name && full.indexOf(name) === 0) full = full.slice(name.length).trim();
    let preview = full.split(' · ')[0].trim();   // отрезаем "· дата время счётчик"
    const outgoing = /^Вы:/.test(preview);
    const unread = !!it.querySelector('[class*="Counter"], [class*="unread"], [class*="Unread"]');
    rows.push({ peer: peer, name: name, preview: preview.slice(0, 200),
                outgoing: outgoing, unread: unread });
  });
  const bt = document.body ? document.body.innerText : '';
  return { url: location.href, rows: rows,
           logged: items.length > 0 || /Сообщения/.test(bt) };
}
"""


class BrowserVkSource(Source):
    """Диалоги VK из DOM своего залогиненного браузера.
    Сообщества (peer<0) и служебный peer 100 отсекаются — только люди."""

    def poll(self):
        hub = BrowserHub.get()
        try:
            pg = hub.page("vk")
            if self.st().get("relogin"):
                self.st()["relogin"] = False
                try:
                    pg.bring_to_front()
                except Exception:
                    pass
            cur = pg.url or ""
            if "/im" not in cur or ("vk.ru" not in cur and "vk.com" not in cur):
                pg.goto("https://vk.ru/im", wait_until="domcontentloaded")
                pg.wait_for_timeout(6000)
                self.st()["nav_ts"] = time.time()
            elif (time.time() - self.st().get("nav_ts", 0)) > 1200:
                pg.goto("https://vk.ru/im", wait_until="domcontentloaded")
                pg.wait_for_timeout(6000)
                self.st()["nav_ts"] = time.time()
            u = (pg.url or "").lower()
            if "login" in u or "/join" in u:
                raise SessionDead("нужно войти во ВКонтакте в окне браузера бота")
            res = pg.evaluate(VK_EXTRACT_JS)
        except SessionDead:
            raise
        except Exception as e:
            if _pw_is_closed_error(e):
                hub.reset()
                raise Transient("окно браузера закрылось — переоткрываю")
            raise Transient("браузер VK: %s" % str(e)[:120])
        if not res.get("logged"):
            raise SessionDead("нужно войти во ВКонтакте в окне браузера бота")
        out = []
        for r in res.get("rows", []):
            peer = r.get("peer") or ""
            try:
                n = int(peer)
            except Exception:
                continue
            if n <= 0 or n == 100:       # сообщества и служебные — мимо
                continue
            preview = r.get("preview") or ""
            nm = (r.get("name") or "")
            if preview.startswith("Избранное") or nm == "Избранное":
                continue                  # свои сохранённые сообщения
            out.append({
                "chat_id": peer,
                "msg_id": preview[:120] or peer,
                "author_id": "", "self_id": "",
                "name": r.get("name") or ("Диалог %s" % peer),
                "title": "", "text": preview, "created": 0,
                "direction": "out" if r.get("outgoing") else "in",
                "unread": 1 if r.get("unread") else 0,
                "msg_type": "", "chat_type": "",
                "official": False,
            })
        return out

    def link(self, chat_id):
        return "https://vk.ru/im?sel=%s" % chat_id


ICON = {"avito": "🟢", "vk": "🔵"}
SRC_LABEL = {"avito": "АВИТО", "vk": "ВК"}


def render(src_name, m, url):
    icon = ICON.get(src_name, "🔔")
    label = SRC_LABEL.get(src_name, src_name.upper())
    head = "%s <b>%s</b> · %s" % (icon, label, esc(m["name"]))
    if m.get("title"):
        head += "\n<i>%s</i>" % esc(m["title"][:120])
    body = esc((m.get("text") or "").strip()[:700]) or "<i>(без текста)</i>"
    tail = '\n\n<a href="%s">Открыть чат</a>' % url if url else ""
    return "%s\n\n%s%s" % (head, body, tail)


def build_one(name, conf):
    if conf.get("site") == "vk":
        return BrowserVkSource(name, conf)
    return BrowserAvitoSource(name, conf)


def build_sources():
    return [build_one(n, c) for n, c in (cfg.get("sources") or {}).items()
            if c.get("enabled", True)]


def poll_once(src):
    msgs = src.poll()
    s = src.st()
    seen = s.setdefault("seen", {})
    fresh = []
    for m in msgs:
        if not m["chat_id"] or not m["msg_id"]:
            continue
        mine = (m.get("direction") == "out"
                or (m["self_id"] and m["author_id"] == m["self_id"]))
        if seen.get(m["chat_id"]) == m["msg_id"]:
            continue
        seen[m["chat_id"]] = m["msg_id"]
        if mine:
            continue
        if is_system_sender(m):
            log("%s: пропущено не от человека — %s" % (src.name, m["name"]))
            continue
        fresh.append(m)
    if len(seen) > 400:
        for k in list(seen)[:-300]:
            seen.pop(k, None)
    if not s.get("primed"):
        s["primed"] = True
        save_state()
        log("%s: первый опрос, запомнил %d чатов молча" % (src.name, len(msgs)))
        return 0
    save_state()
    fresh.sort(key=lambda x: x.get("created") or 0)
    if not cfg.get("muted"):
        for m in fresh:
            send(render(src.name, m, src.link(m["chat_id"])))
    return len(fresh)


def source_loop():
    backoff = {}
    while True:
        try:
            if LOGIN.get("want"):
                LOGIN["want"] = False
                start_login_session()
            if LOGIN.get("stop"):
                LOGIN["stop"] = False
                stop_login_session()
            if LOGIN.get("until") and time.time() > LOGIN["until"]:
                stop_login_session()

            srcs = build_sources()
            # раньше ждали chat_id, то есть /start от владельца, — и браузер
            # не открывался вовсе. Получателя знаем уже при установке, этого
            # хватает, чтобы начать работу и показать окно для входа.
            if not srcs or not notify_target():
                time.sleep(5)
                continue
            for src in srcs:
                if backoff.get(src.name, 0) > time.time():
                    continue
                st = state.setdefault(src.name, {})
                try:
                    n = poll_once(src)
                    if n:
                        log("%s: %d новых" % (src.name, n))
                    if st.get("dead") or st.get("blocked"):
                        was_blocked = st.get("blocked")
                        st["dead"] = False
                        st["blocked"] = False
                        save_state()
                        send("✅ <b>%s</b>: %s" % (
                            esc(src.name),
                            "проверка пройдена, слежу дальше." if was_blocked
                            else "связь восстановлена."))
                    backoff.pop(src.name, None)
                except SessionDead as e:
                    if not st.get("dead"):
                        st["dead"] = True
                        save_state()
                        send("⚠️ <b>%s</b>: %s\n\nПришли <code>/login</code> — "
                             "я открою окно и дам ссылку, там войдёшь.\n"
                             "Если этот источник сейчас не нужен, выключи его: "
                             "<code>/%s off</code>"
                             % (esc(src.name), esc(e), esc(src.name)))
                    backoff[src.name] = time.time() + 600
                    log("%s DEAD: %s" % (src.name, e))
                except Blocked as e:
                    now = time.time()
                    if not st.get("blocked") \
                            or (now - st.get("blocked_at", 0)) > 3600:
                        st["blocked"] = True
                        st["blocked_at"] = now
                        save_state()
                        send("🚧 <b>%s</b>: %s\n\n%s"
                             % (esc(src.name), esc(e), blocked_hint()))
                    backoff[src.name] = now + 300
                    log("%s BLOCKED: %s" % (src.name, e))
                except Transient as e:
                    backoff[src.name] = time.time() + 120
                    log("%s transient: %s" % (src.name, e))
                except Exception as e:
                    backoff[src.name] = time.time() + 120
                    log("%s unexpected: %s\n%s"
                        % (src.name, e, traceback.format_exc()))
        except Exception:
            log("source_loop:", traceback.format_exc())
        time.sleep(max(10, int(cfg.get("interval", 25))))


# ------------------------------------------------------------- tg command ---
_HELP_BASE = """<b>Команды</b>
/status — что настроено и живо ли
/settings — все настройки и как их менять
/recipient @name — кому слать уведомления
/interval 40 — период опроса, сек
/ignore слово — не слать, если есть в имени/тексте
/ignore — показать список, /ignore- слово — убрать
/mute, /unmute — тишина
/restart — перезапустить бота
/test — проверить связь
/help — это сообщение
"""

HELP = _HELP_BASE + """/login — войти в Авито и ВК
/avito, /vk — состояние; on/off — включить или выключить

Читаю через собственный браузер: логинишься в его
окне один раз, дальше всё само."""

HELP += ("\n\nСлужебные отправители, сообщества и свои исходящие "
         "отсекаются — шлю только живых людей.")

def handle(msg):
    frm = msg.get("from") or {}
    uname = (frm.get("username") or "").lower()
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    # получателя запоминаем до проверки владельца: он может быть другим
    # человеком, и другого шанса узнать его числовой id у нас нет
    want = eff_recipient().lstrip("@").lower()
    if want and not want.lstrip("-").isdigit() \
            and (uname == want or str(frm.get("id")) == want) \
            and cfg.get("recipient_chat_id") != chat_id:
        cfg["recipient_chat_id"] = chat_id
        save_cfg()
        log("получатель %s распознан, чат %s" % (want, chat_id))
        send("✅ Готово, буду присылать уведомления сюда.", chat_id=chat_id)

    if not is_owner(frm):
        log("отклонён чужой пользователь: @%s (%s)" % (uname, frm.get("id")))
        # молчать нельзя: чаще всего это сам хозяин, просто владелец задан
        # неверно (например, вписали имя бота). Отвечаем не чаще раза в час.
        seen = state.setdefault("_rejected", {})
        key = str(chat_id)
        if time.time() - seen.get(key, 0) > 3600:
            seen[key] = time.time()
            save_state()
            who = ("@" + uname) if uname else str(frm.get("id"))
            send("Команды принимаю только от владельца, а сейчас владелец — "
                 "<code>%s</code>.\n\nЕсли бот твой, укажи себя владельцем "
                 "на сервере:\n<code>sabz-notifier config owner %s</code>\n\n"
                 "<i>Владелец — твой аккаунт в Telegram, не имя бота.</i>"
                 % (esc(("@" + OWNER) if OWNER else "не задан"), esc(who)),
                 chat_id=chat_id)
        return
    if cfg.get("chat_id") != chat_id:
        cfg["chat_id"] = chat_id
        save_cfg()
        log("chat_id сохранён: %s" % chat_id)

    low = text.lower()
    if low.startswith("/start") or low.startswith("/help"):
        send(HELP, chat_id=chat_id)
        return
    if low.startswith("/test"):
        send("✅ Бот жив. Источников: %d" % len(build_sources()), chat_id=chat_id)
        return
    if low.startswith("/mute"):
        cfg["muted"] = True
        save_cfg()
        send("🔇 Уведомления выключены.", chat_id=chat_id)
        return
    if low.startswith("/unmute"):
        cfg["muted"] = False
        save_cfg()
        send("🔔 Уведомления включены.", chat_id=chat_id)
        return
    if low.startswith("/interval"):
        try:
            cfg["interval"] = max(10, int(text.split()[1]))
            save_cfg()
            send("⏱ Период опроса: %d сек." % cfg["interval"], chat_id=chat_id)
        except Exception:
            send("Формат: <code>/interval 25</code>", chat_id=chat_id)
        return
    if low.startswith("/ignore"):
        rest = text[len("/ignore"):].strip()
        lst = cfg.setdefault("ignore", [])
        if rest.startswith("-"):
            word = rest[1:].strip().lower()
            if word in lst:
                lst.remove(word)
                save_cfg()
                send("🗑 Убрал из игнора: <code>%s</code>" % esc(word),
                     chat_id=chat_id)
            else:
                send("Такого в списке нет.", chat_id=chat_id)
        elif rest:
            word = rest.lower()
            if word not in lst:
                lst.append(word)
                save_cfg()
            send("🙈 Игнорирую сообщения со словом <code>%s</code>"
                 % esc(word), chat_id=chat_id)
        else:
            send("<b>Список игнора</b>\n" +
                 ("\n".join("• <code>%s</code>" % esc(w) for w in lst)
                  if lst else "<i>пусто</i>") +
                 "\n\nДобавить: <code>/ignore слово</code>\n"
                 "Убрать: <code>/ignore- слово</code>", chat_id=chat_id)
        return
    if low.startswith("/recipient") or low.startswith("/to "):
        parts = text.split(None, 1)
        val = parts[1].strip() if len(parts) > 1 else ""
        if not val:
            send("Сейчас уведомления идут: <code>%s</code>\n\n"
                 "Сменить: <code>/recipient @username</code> или "
                 "<code>/recipient 123456789</code>\n"
                 "Вернуть себе: <code>/recipient -</code>"
                 % esc(eff_recipient() or "в этот чат"), chat_id=chat_id)
            return
        if val == "-":
            cfg.pop("recipient", None)
            cfg.pop("recipient_chat_id", None)
            cfg["chat_id"] = chat_id
            save_cfg()
            send("✅ Уведомления снова приходят сюда.", chat_id=chat_id)
            return
        cfg["recipient"] = val
        cfg.pop("recipient_chat_id", None)
        save_cfg()
        tgt = notify_target()
        if tgt and tg("getChat", chat_id=tgt).get("ok"):
            send("✅ Получатель: <code>%s</code> — доступен."
                 % esc(val), chat_id=chat_id)
        else:
            send("✅ Получатель записан: <code>%s</code>\n\n"
                 "Telegram не разрешает боту писать первым — попроси его "
                 "отправить мне <code>/start</code>, после этого уведомления "
                 "пойдут." % esc(val), chat_id=chat_id)
        return

    if low.startswith("/restart"):
        send("♻️ Перезапускаюсь, вернусь через несколько секунд.",
             chat_id=chat_id)
        log("перезапуск по команде владельца")
        threading.Timer(1.5, lambda: os._exit(1)).start()
        return

    if low.startswith("/settings"):
        send("<b>Настройки</b>\n"
             "режим: <code>%s</code>%s\n"
             "владелец: <code>%s</code>\n"
             "получатель: <code>%s</code>\n"
             "период опроса: <code>%s сек</code>\n"
             "тишина: <code>%s</code>\n"
             "игнор-слов: <code>%d</code>\n\n"
             "<b>Что можно менять прямо здесь</b>\n"
             "<code>/recipient @name</code> — кому слать\n"
             "<code>/interval 40</code> — как часто проверять\n"
             "<code>/ignore слово</code> — что не слать\n"
             "<code>/mute</code> и <code>/unmute</code> — тишина\n"
             "<code>/login</code> — войти в Авито и ВК (пришлю ссылку)\n"
             "<code>/restart</code> — перезапустить бота\n\n"
             "Токен, владельца и режим меняют на сервере:\n"
             "<code>sabz-notifier config recipient @name</code>\n"
             "<code>sabz-notifier reconfigure</code> — спросит всё заново\n\n"
             "Удалить: <code>sabz-notifier uninstall</code> — данные останутся,\n"
             "<code>sabz-notifier uninstall --full</code> — снести подчистую"
             % (" (headless)" if HEADLESS else "",
                esc(("@" + OWNER) if OWNER else "не задан"),
                esc(eff_recipient() or "этот чат"),
                esc(cfg.get("interval")),
                "включена" if cfg.get("muted") else "выключена",
                len(cfg.get("ignore") or [])), chat_id=chat_id)
        return

    if low.startswith("/status"):
        lines = ["<b>Статус</b>",
                 "период: %s сек" % cfg.get("interval"),
                 "тишина: %s" % ("да" if cfg.get("muted") else "нет")]
        if not cfg.get("sources"):
            lines.append("\n<i>источники не настроены</i>")
        for n, c in (cfg.get("sources") or {}).items():
            s = state.get(n, {})
            if not c.get("enabled", True):
                st_txt = "⛔️ выключен"
            elif s.get("blocked"):
                st_txt = "🚧 ждёт прохождения проверки «не робот»"
            elif s.get("dead"):
                st_txt = "❌ нужен вход — команда /login"
            else:
                st_txt = "✅ работает"
            lines.append("\n%s <b>%s</b> — %s"
                         % (ICON.get(n, "•"), esc(n), st_txt))
            lines.append("  чатов в памяти: %d" % len(s.get("seen", {})))
        send("\n".join(lines), chat_id=chat_id)
        return

    if low.startswith("/login"):
        arg = text[len("/login"):].strip().lower()
        if arg in ("stop", "стоп", "close"):
            if LOGIN.get("until"):
                LOGIN["stop"] = True
                send("Закрываю окно входа…", chat_id=chat_id)
            else:
                send("Окно входа и так закрыто.", chat_id=chat_id)
            return
        if HAS_DESKTOP:
            for n in ("avito", "vk"):
                state.setdefault(n, {})["relogin"] = True
            save_state()
            send("Вывожу окна Авито и ВК на экран этого компьютера — "
                 "залогинься в них как обычно.", chat_id=chat_id)
            return
        if LOGIN.get("until") and LOGIN.get("url"):
            send("Окно уже открыто, вот ссылка:\n\n%s" % LOGIN["url"],
                 chat_id=chat_id)
            return
        LOGIN["want"] = True
        send("Открываю окно… пришлю ссылку сюда через полминуты.",
             chat_id=chat_id)
        return
    if low.startswith("/avito") or low.startswith("/vk"):
        name = "avito" if low.startswith("/avito") else "vk"
        parts = text.split(None, 1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""
        src = (cfg.setdefault("sources", {})
               .setdefault(name, {"kind": "browser", "site": name,
                                  "enabled": True}))
        if arg in ("off", "выкл", "выключи", "0", "-"):
            src["enabled"] = False
            save_cfg()
            state.pop(name, None)
            save_state()
            send("⛔️ <b>%s</b> выключен — больше не опрашиваю и не беспокою.\n"
                 "Включить обратно: <code>/%s on</code>"
                 % (esc(name), esc(name)), chat_id=chat_id)
            return
        if arg in ("on", "вкл", "включи", "1", "+"):
            src["enabled"] = True
            save_cfg()
            state.pop(name, None)
            save_state()
            send("✅ <b>%s</b> включён. Если попросит войти — "
                 "<code>/login</code>." % esc(name), chat_id=chat_id)
            return
        s = state.get(name, {})
        if not src.get("enabled", True):
            status = "⛔️ выключен"
        elif s.get("blocked"):
            status = "🚧 ждёт проверки «не робот»"
        elif s.get("dead"):
            status = "❌ нужен вход"
        else:
            status = "✅ работает"
        send("<b>%s</b> — %s\n\nЧитаю через собственный браузер, присылать "
             "ничего не нужно.\n\nВойти: <code>/login</code>\n"
             "Выключить: <code>/%s off</code>\n"
             "Включить: <code>/%s on</code>"
             % (esc(name), status, esc(name), esc(name)), chat_id=chat_id)
        return

    send("Не понял.\n\n" + HELP, chat_id=chat_id)


def telegram_loop():
    offset = None
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=50,
                   allowed_updates=["message"])
            if not r.get("ok"):
                time.sleep(5)
                continue
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message")
                if m:
                    try:
                        handle(m)
                    except Exception:
                        log("handle:", traceback.format_exc())
        except Exception:
            log("telegram_loop:", traceback.format_exc())
            time.sleep(5)


DEFAULT_SOURCES = {
    "avito": {"kind": "browser", "site": "avito", "enabled": True},
    "vk": {"kind": "browser", "site": "vk", "enabled": True},
}


def selfcheck():
    """Проверка установки без обращения к очереди обновлений Telegram."""
    ok = True
    print("режим        : браузер%s" % (" (headless)" if HEADLESS else ""))
    print("каталог      : %s" % DATA)
    print("владелец     : %s" % (("@" + OWNER) if OWNER else "любой (не задан)"))
    if not TG_TOKEN:
        print("✗ TG_TOKEN не задан")
        return 1
    r = tg("getMe")
    if r.get("ok"):
        print("✓ Telegram   : бот @%s" % (r.get("result") or {}).get("username"))
    else:
        print("✗ Telegram   : %s" % str(r)[:160])
        ok = False
    tgt = notify_target()
    print("получатель   : %s" % (tgt if tgt else "не задан"))
    if tgt:
        rc = tg("getChat", chat_id=tgt)
        if rc.get("ok"):
            print("✓ получатель : доступен")
        else:
            print("! получатель : пока недоступен — он должен сам написать "
                  "боту /start (Telegram не даёт писать первым)")
    try:
        os.makedirs(DATA, exist_ok=True)
        probe = os.path.join(DATA, ".writetest")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        print("✓ запись     : ок")
    except Exception as e:
        print("✗ запись     : %s" % e)
        ok = False
    if True:
        try:
            import playwright                                  # noqa: F401
            print("✓ playwright : установлен")
        except Exception:
            print("✗ playwright : не установлен — без него бот не заработает")
            ok = False
    cid = cfg.get("chat_id")
    print("chat_id      : %s" % (cid or "ещё нет — напиши боту /start"))
    print("ИТОГ: %s" % ("всё готово" if ok else "есть проблемы"))
    return 0 if ok else 1


def main():
    if not TG_TOKEN:
        raise SystemExit("TG_TOKEN не задан (см. .env)")
    os.makedirs(DATA, exist_ok=True)
    if not cfg.get("sources"):
        cfg["sources"] = json.loads(json.dumps(DEFAULT_SOURCES))
        cfg.setdefault("interval", 40)
        save_cfg()
    log("старт. владелец=@%s источников=%d"
        % (OWNER, len(cfg.get("sources") or {})))
    threading.Thread(target=source_loop, daemon=True).start()
    telegram_loop()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    main()
