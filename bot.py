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


# light  — без браузера: куки/токен присылаются боту в Telegram
# browser — свой профиль браузера, вход один раз
MODE = (os.environ.get("BOT_MODE") or "browser").strip().lower()
HEADLESS = _flag("BOT_HEADLESS")
IS_WINDOWS = sys.platform.startswith("win")

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


def notify_target():
    """Куда шлём уведомления.

    Числовой id — берём как есть. Для @username Telegram умеет доставлять
    только в канал/супергруппу, поэтому для обычного человека используем
    id, который бот запомнил, когда тот написал ему /start.
    """
    r = (RECIPIENT or "").strip()
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
_CURL_TOKEN = re.compile(
    "'([^']*)'"
    '|"((?:[^"\\\\]|\\\\.)*)"'
    "|(\\S+)")

_NO_ARG_FLAGS = {"--compressed", "-s", "--silent", "-i", "-k", "--insecure",
                 "-l", "--location", "-g", "-v", "--verbose", "-f", "--fail"}
_ARG_FLAGS = {"-a", "--user-agent", "-e", "--referer", "-u", "--user",
              "--proxy", "-o", "--output", "--max-time", "--connect-timeout",
              "-m", "--retry"}


def parse_curl(text):
    """Разбирает 'Copy as cURL' (bash и cmd) -> {url, method, headers, data}."""
    text = text.strip()
    text = re.sub(r"\^\r?\n", " ", text)
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"\s+", " ", text)
    if text.lower().startswith("curl"):
        text = text[4:]
    toks = []
    for m in _CURL_TOKEN.finditer(text):
        if m.group(1) is not None:
            toks.append(m.group(1))
        elif m.group(2) is not None:
            toks.append(m.group(2).replace('\\"', '"'))
        else:
            toks.append(m.group(3))
    url = None
    method = None
    data = None
    headers = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        if low in ("-h", "--header") and i + 1 < len(toks):
            i += 1
            if ":" in toks[i]:
                k, v = toks[i].split(":", 1)
                headers[k.strip()] = v.strip()
        elif low in ("-b", "--cookie") and i + 1 < len(toks):
            i += 1
            headers["Cookie"] = toks[i]
        elif low in ("-x", "--request") and i + 1 < len(toks):
            i += 1
            method = toks[i].upper()
        elif low in ("-d", "--data", "--data-raw", "--data-binary",
                     "--data-urlencode") and i + 1 < len(toks):
            i += 1
            data = toks[i]
        elif low in _NO_ARG_FLAGS:
            pass
        elif low in _ARG_FLAGS:
            i += 1
        elif t.startswith("http://") or t.startswith("https://"):
            url = t
        i += 1
    for bad in ("Content-Length", "content-length", "Accept-Encoding",
                "accept-encoding"):
        headers.pop(bad, None)
    if not url:
        raise ValueError("в cURL не нашёл URL")
    return {"url": url, "method": method or ("POST" if data else "GET"),
            "headers": headers, "data": data}


def cookie_domain(url):
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return "?"


# ------------------------------------------------------- generic message ---
def _dig(o, *keys):
    for k in keys:
        if isinstance(o, dict) and k in o:
            o = o[k]
        else:
            return None
    return o


def _text_of(content):
    """Достаёт текст из content разных форм Avito."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for k in ("text", "title", "body"):
            v = content.get(k)
            if isinstance(v, str) and v.strip():
                return v
        if "call" in content:
            return "📞 звонок"
        if "image" in content or "images" in content:
            return "🖼 изображение"
        if "location" in content:
            return "📍 геолокация"
        if "item" in content:
            return "📦 объявление"
        if "link" in content:
            return _dig(content, "link", "text") or "🔗 ссылка"
    return ""


def _looks_like_chat(d):
    if not isinstance(d, dict):
        return False
    if "last_message" in d or "lastMessage" in d:
        return True
    return "id" in d and "users" in d and isinstance(d.get("users"), list)


def deep_find_chats(root, limit=400000):
    """Ищет список чатов где угодно внутри дерева. Возвращает самый большой."""
    best = None
    stack = [root]
    seen = 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            if node and sum(1 for x in node[:10] if _looks_like_chat(x)) \
                    >= max(1, min(len(node), 10) // 2):
                if best is None or len(node) > len(best):
                    best = node
            else:
                stack.extend(node)
    return best


_HTML_STATE_PATTERNS = (
    # React Router SSR — то, что реально использует мессенджер Avito
    r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(\s*"((?:[^"\\]|\\.)*)"\s*\)',
    r'window\.__staticRouterHydrationData\s*=\s*(\{.*?\})\s*;',
    r'window\.__initialData__\s*=\s*"((?:[^"\\]|\\.)*)"',
    r"window\.__initialData__\s*=\s*'((?:[^'\\]|\\.)*)'",
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>',
    r'JSON\.parse\(\s*"((?:[^"\\]|\\.)*\\"(?:loaderData|chats)\\".*?)"\s*\)',
)


def _try_decode_state(blob):
    """Раскодирует вшитое в HTML состояние в объект.

    Кусок может быть: голым JSON, JS-строковым литералом с JSON внутри
    (JSON.parse("...")), либо URL-кодированным JSON.
    """
    candidates = [blob]
    try:
        # содержимое строкового литерала: \" \\ \n \uXXXX разбираются по JSON
        candidates.append(json.loads('"' + blob + '"'))
    except Exception:
        pass
    for c in list(candidates):
        if isinstance(c, str) and "%" in c:
            try:
                candidates.append(urllib.parse.unquote(c))
            except Exception:
                pass
    for c in candidates:
        if not isinstance(c, str):
            continue
        c = c.strip()
        if not c:
            continue
        try:
            obj = json.loads(c)
        except Exception:
            continue
        if isinstance(obj, str):
            try:
                obj = json.loads(urllib.parse.unquote(obj))
            except Exception:
                continue
        if isinstance(obj, (dict, list)):
            return obj
    return None


def extract_state_from_html(text):
    """Достаёт JSON-состояние из HTML-страницы мессенджера."""
    for pat in _HTML_STATE_PATTERNS:
        for m in re.finditer(pat, text, re.S):
            obj = _try_decode_state(m.group(1))
            if obj is not None:
                return obj
    return None


def extract_avito_chats(js):
    """Нормализует ответ chats-эндпоинта в список dict. None = не распознал."""
    chats = None
    if isinstance(js, dict):
        for path in (("chats",), ("data", "chats"), ("result", "chats"),
                     ("payload", "chats")):
            v = _dig(js, *path)
            if isinstance(v, list):
                chats = v
                break
    if chats is None and isinstance(js, list) \
            and any(_looks_like_chat(x) for x in js[:10]):
        chats = js
    if chats is None:
        chats = deep_find_chats(js)
    if chats is None:
        return None
    out = []
    for c in chats:
        if not isinstance(c, dict):
            continue
        lm = c.get("last_message") or c.get("lastMessage") or {}
        if not isinstance(lm, dict):
            lm = {}
        users = c.get("users")
        users = users if isinstance(users, list) else []
        me = c.get("user_id") or c.get("self_id")
        author_id = lm.get("author_id") or lm.get("authorId")
        name = None
        for u in users:
            if isinstance(u, dict) and author_id is not None \
                    and u.get("id") == author_id:
                name = u.get("name") or _dig(u, "public_user_profile", "name")
                break
        if not name:
            for u in users:
                if isinstance(u, dict) and u.get("id") != me:
                    name = u.get("name")
                    break
        ctx = c.get("context") or {}
        item = _dig(ctx, "value") or {}
        title = item.get("title") if isinstance(item, dict) else None
        author = None
        for u in users:
            if isinstance(u, dict) and author_id is not None \
                    and u.get("id") == author_id:
                author = u
                break
        official = bool(
            (author or {}).get("is_official")
            or (author or {}).get("official")
            or c.get("is_official")
            or _dig(author or {}, "public_user_profile", "is_official"))
        out.append({
            "chat_id": str(c.get("id") or c.get("chat_id") or ""),
            "msg_id": str(lm.get("id") or lm.get("created") or ""),
            "author_id": str(author_id) if author_id is not None else "",
            "self_id": str(me) if me is not None else "",
            "name": name or "Собеседник",
            "title": title or "",
            "text": _text_of(lm.get("content") or lm.get("text")),
            "created": lm.get("created") or 0,
            "direction": lm.get("direction") or "",
            "unread": c.get("unread_count") or c.get("unreadCount") or 0,
            "msg_type": str(lm.get("type") or ""),
            "chat_type": str(ctx.get("type") or "") if isinstance(ctx, dict) else "",
            "official": official,
        })
    return out


# Сообщения не от живых людей: сервис, автоответы, системные уведомления.
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


def extract_vk_response(js):
    """Находит тело ответа VK (response с items) в любом виде. None = нет."""
    if not isinstance(js, dict):
        return None
    resp = js.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("items"), list):
        return resp
    if isinstance(js.get("items"), list):
        return js
    items = deep_find_chats(js)
    if items is None:
        return None
    return {"items": items,
            "profiles": js.get("profiles") or _dig(js, "response", "profiles") or [],
            "groups": js.get("groups") or _dig(js, "response", "groups") or []}


def map_vk_conversations(resp):
    """Нормализует ответ messages.getConversations в общий вид."""
    out = []
    if not isinstance(resp, dict):
        return out
    names = {}
    for p in (resp.get("profiles") or []):
        if isinstance(p, dict) and "id" in p:
            names[p["id"]] = ("%s %s" % (p.get("first_name", ""),
                                         p.get("last_name", ""))).strip()
    for g in (resp.get("groups") or []):
        if isinstance(g, dict) and "id" in g:
            names[-g["id"]] = g.get("name", "Сообщество")
    for it in (resp.get("items") or []):
        if not isinstance(it, dict):
            continue
        msg = it.get("last_message") or it.get("lastMessage") or {}
        conv = it.get("conversation") or it
        if not isinstance(msg, dict):
            continue
        peer = _dig(conv, "peer", "id")
        if peer is None:
            peer = msg.get("peer_id") or conv.get("id")
        frm = msg.get("from_id")
        text = msg.get("text") or ""
        if not text:
            att = msg.get("attachments") or []
            if att and isinstance(att[0], dict):
                text = "📎 вложение (%s)" % att[0].get("type", "?")
            elif msg.get("action"):
                text = "ℹ️ действие в беседе"
        title = ""
        if _dig(conv, "peer", "type") == "chat":
            title = _dig(conv, "chat_settings", "title") or "Беседа"
        out.append({
            "chat_id": str(peer),
            "msg_id": str(msg.get("id") or msg.get("conversation_message_id") or ""),
            "author_id": str(frm) if frm is not None else "",
            "self_id": "",
            "name": names.get(frm) or ("ID %s" % frm),
            "title": title,
            "text": text,
            "created": msg.get("date") or 0,
            "direction": "out" if msg.get("out") else "in",
            "unread": conv.get("unread_count") or 0,
            "msg_type": "system" if msg.get("action") else "",
            "chat_type": _dig(conv, "peer", "type") or "",
            "official": False,
        })
    return out


def looks_like_vk(url):
    host = cookie_domain(url).lower()
    return any(d in host for d in ("vk.com", "vk.ru", "vk.me", "userapi.com"))


# --------------------------------------------------------------- errors ---
class SessionDead(Exception):
    pass


class Blocked(Exception):
    """Сайт показал проверку «вы не робот» — её проходит человек, не бот."""
    pass


_BLOCK_PAGE = re.compile(
    r"Доступ ограничен|проблема с IP|не робот|Проверка безопасности", re.I)


def blocked_hint():
    """Что делать владельцу, чтобы снять проверку. Зависит от режима."""
    if MODE == "browser":
        return ("Проверку нужно пройти вручную в окне браузера бота — "
                "открой его и нажми кнопку.\nНа сервере: "
                "<code>sabz-notifier login</code>, дальше по SSH-туннелю.")
    return ("Проверку нужно пройти <b>с того же IP, с которого ходит бот</b> — "
            "домашний браузер тут не поможет.\nНа сервере: поставь режим "
            "<code>browser</code> и пройди её через "
            "<code>sabz-notifier login</code>, либо задай выход в РФ: "
            "<code>/proxy avito http://user:pass@host:port</code>")


class Transient(Exception):
    pass


# ----------------------------------------------------------------- source ---
class Source(object):
    def __init__(self, name, conf):
        self.name = name
        self.conf = conf

    def st(self):
        return state.setdefault(self.name, {"seen": {}, "primed": False})


class ReplaySource(Source):
    """Повторяет запрос, скопированный из DevTools (Avito, VK и подобные)."""

    def parser(self):
        return self.conf.get("parser") or (
            "vk" if looks_like_vk(self.conf["request"]["url"]) else "avito")

    def poll(self):
        r = self.conf["request"]
        st, raw, _ = http(r["url"], headers=r["headers"],
                          data=r.get("data"), method=r.get("method"),
                          proxy=self.conf.get("proxy"), timeout=45)
        if st in (401, 403):
            raise SessionDead("HTTP %s — куки больше не действуют" % st)
        if st == 429:
            raise Blocked("HTTP 429 — Авито просит пройти проверку с этого IP")
        if st >= 400:
            raise Transient("HTTP %s" % st)
        text = raw.decode("utf-8", "replace")
        if _BLOCK_PAGE.search(text[:8000]):
            raise Blocked("вместо данных пришла страница проверки Авито")
        try:
            body = json.loads(text)
        except Exception:
            body = extract_state_from_html(text)
            if body is None:
                low = text[:4000].lower()
                if "login" in low or "вход" in low or "авториз" in low:
                    raise SessionDead("вернулась страница входа — сессия истекла")
                raise SessionDead("не нашёл данные в ответе (%d байт)" % len(raw))

        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            e = body["error"]
            code = e.get("error_code")
            msg = e.get("error_msg") or e.get("error_descr") or ""
            if code in (5, 27, 28) or "authoriz" in str(msg).lower():
                raise SessionDead("доступ отозван (%s)" % msg)
            if code in (6, 29):
                raise Transient("лимит запросов")
            raise Transient("ошибка %s: %s" % (code, msg))

        if self.parser() == "vk":
            resp = extract_vk_response(body)
            if resp is None:
                raise SessionDead("в ответе нет списка диалогов")
            return map_vk_conversations(resp)
        chats = extract_avito_chats(body)
        if chats is None:
            raise SessionDead("в ответе нет списка чатов")
        return chats

    def link(self, chat_id):
        if self.parser() == "vk":
            return "https://vk.com/im?sel=%s" % chat_id
        host = cookie_domain(self.conf["request"]["url"])
        if "avito" in host:
            return "https://www.avito.ru/profile/messenger/channel/%s" % chat_id
        return None


class VkSource(Source):
    """VK: последние диалоги через messages.getConversations."""
    API = "https://api.vk.com/method/%s"
    V = "5.199"

    def call(self, method, **p):
        p["access_token"] = self.conf["token"]
        p["v"] = self.V
        st, js, _ = http_json(
            self.API % method, data=urllib.parse.urlencode(p),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            proxy=self.conf.get("proxy"), timeout=40)
        if not isinstance(js, dict):
            raise Transient("VK: ответ не JSON (HTTP %s)" % st)
        if "error" in js:
            e = js["error"]
            code = e.get("error_code")
            msg = e.get("error_msg", "")
            if code in (5, 27, 28):
                raise SessionDead("токен недействителен (%s)" % msg)
            if code in (6, 29):
                raise Transient("лимит запросов")
            raise Transient("VK error %s: %s" % (code, msg))
        return js.get("response")

    def poll(self):
        return map_vk_conversations(
            self.call("messages.getConversations", count=30, extended=1))

    def link(self, chat_id):
        return "https://vk.com/im?sel=%s" % chat_id


BROWSER_DIR = os.path.join(DATA, "browser")


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
        order = ("chrome", "msedge", None) if IS_WINDOWS \
            else (None, "chromium", "chrome")
        channels = [prefer] if prefer else []
        channels += [c for c in order if c != prefer]
        args = ["--disable-blink-features=AutomationControlled"]
        if not IS_WINDOWS:
            # на сервере часто root и маленький /dev/shm
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        last = None
        for ch in channels:
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(
                    BROWSER_DIR,
                    channel=ch,
                    headless=HEADLESS,
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
    kind = conf.get("kind")
    if kind == "browser":
        if conf.get("site") == "vk":
            return BrowserVkSource(name, conf)
        return BrowserAvitoSource(name, conf)
    if kind == "vk":
        return VkSource(name, conf)
    return ReplaySource(name, conf)


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
            srcs = build_sources()
            if not cfg.get("chat_id") or not srcs:
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
                        send("⚠️ <b>%s</b>: %s\n\nПришли новый доступ: команда "
                             "<code>/%s</code>, потом вставь cURL/токен."
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
/interval 40 — период опроса, сек
/ignore слово — не слать, если есть в имени/тексте
/ignore — показать список, /ignore- слово — убрать
/mute, /unmute — тишина
/test — проверить связь
/help — это сообщение
"""

if MODE == "browser":
    HELP = _HELP_BASE + """/login — открыть Авито и ВК для входа

Читаю через собственный браузер: логинишься в его
окне один раз, дальше всё само."""
else:
    HELP = _HELP_BASE + """/avito — прислать cURL страницы мессенджера Авито
/vk — прислать cURL или токен ВК
/proxy avito http://... — выход в РФ для Авито

Лёгкий режим, без браузера: доступ обновляешь,
присылая боту cURL из DevTools."""

HELP += ("\n\nСлужебные отправители, сообщества и свои исходящие "
         "отсекаются — шлю только живых людей.")

pending = {"await": None}


def handle(msg):
    frm = msg.get("from") or {}
    uname = (frm.get("username") or "").lower()
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    # получателя запоминаем до проверки владельца: он может быть другим
    # человеком, и другого шанса узнать его числовой id у нас нет
    want = (RECIPIENT or "").strip().lstrip("@").lower()
    if want and not want.lstrip("-").isdigit() \
            and (uname == want or str(frm.get("id")) == want) \
            and cfg.get("recipient_chat_id") != chat_id:
        cfg["recipient_chat_id"] = chat_id
        save_cfg()
        log("получатель %s распознан, чат %s" % (want, chat_id))
        send("✅ Готово, буду присылать уведомления сюда.", chat_id=chat_id)

    if not is_owner(frm):
        log("отклонён чужой пользователь: @%s (%s)" % (uname, frm.get("id")))
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
    if low.startswith("/proxy"):
        parts = text.split(None, 2)
        if len(parts) < 3:
            cur = {n: (c.get("proxy") or "нет")
                   for n, c in (cfg.get("sources") or {}).items()}
            send("<b>Прокси</b>\n" +
                 ("\n".join("• %s: <code>%s</code>" % (esc(k), esc(v))
                            for k, v in cur.items()) or "<i>источников нет</i>") +
                 "\n\nЗадать: <code>/proxy avito http://user:pass@host:8080</code>"
                 "\nУбрать: <code>/proxy avito -</code>"
                 "\n\nТолько HTTP(S)-прокси. Нужен выход в РФ — "
                 "с зарубежного IP Авито отдаёт 429.", chat_id=chat_id)
            return
        name, val = parts[1], parts[2].strip()
        src_conf = (cfg.get("sources") or {}).get(name)
        if not src_conf:
            send("Нет источника <code>%s</code>." % esc(name), chat_id=chat_id)
            return
        old = src_conf.get("proxy")
        src_conf["proxy"] = None if val == "-" else val
        try:
            build_one(name, src_conf).poll()
        except Exception as e:
            src_conf["proxy"] = old
            send("❌ Через этот прокси не вышло: %s" % esc(e), chat_id=chat_id)
            return
        save_cfg()
        send("✅ Прокси для <b>%s</b>: <code>%s</code>"
             % (esc(name), esc(src_conf["proxy"] or "нет")), chat_id=chat_id)
        return
    if low.startswith("/status"):
        lines = ["<b>Статус</b>",
                 "период: %s сек" % cfg.get("interval"),
                 "тишина: %s" % ("да" if cfg.get("muted") else "нет")]
        if not cfg.get("sources"):
            lines.append("\n<i>источники не настроены</i>")
        for n, c in (cfg.get("sources") or {}).items():
            s = state.get(n, {})
            if s.get("blocked"):
                st_txt = "🚧 ждёт прохождения проверки «не робот»"
            elif s.get("dead"):
                st_txt = "❌ нужен вход в окне браузера" \
                    if c.get("kind") == "browser" else "❌ нужен новый доступ"
            else:
                st_txt = "✅ работает"
            lines.append("\n%s <b>%s</b> — %s"
                         % (ICON.get(n, "•"), esc(n), st_txt))
            if c.get("kind") == "browser":
                lines.append("  режим: свой браузер (%s)" % esc(c.get("site", "?")))
            elif c.get("kind") != "vk":
                lines.append("  хост: <code>%s</code>"
                             % esc(cookie_domain(c["request"]["url"])))
            lines.append("  чатов в памяти: %d" % len(s.get("seen", {})))
        send("\n".join(lines), chat_id=chat_id)
        return

    if low.startswith("/login"):
        for n in ("avito", "vk"):
            st = state.setdefault(n, {})
            st["relogin"] = True
        save_state()
        send("Открываю окна Авито и ВК в браузере бота. Залогинься в них "
             "как обычно — я подхвачу сам, ничего вставлять не надо.",
             chat_id=chat_id)
        return
    if low.startswith("/avito"):
        rest = text[len("/avito"):].strip()
        cur = (cfg.get("sources") or {}).get("avito") or {}
        if cur.get("kind") == "browser" and not rest:
            send("Авито читается через браузер бота. Если просит войти — "
                 "команда <code>/login</code>, дальше логинишься в окне. "
                 "cURL не нужен.", chat_id=chat_id)
            return
        if rest:
            return setup_replay("avito", rest, chat_id)
        pending["await"] = "avito"
        send("Жду cURL для Avito (ручной режим). "
             "F12 → Network → запрос мессенджера → Copy as cURL (bash).",
             chat_id=chat_id)
        return
    if low.startswith("/vk"):
        rest = text[len("/vk"):].strip()
        cur = (cfg.get("sources") or {}).get("vk") or {}
        if cur.get("kind") == "browser" and not rest:
            send("ВК читается через браузер бота. Если просит войти — "
                 "команда <code>/login</code>.", chat_id=chat_id)
            return
        if rest:
            return setup_vk(rest, chat_id)
        pending["await"] = "vk"
        send("Жду токен VK (строка вида <code>vk1.a....</code>) "
             "или cURL — ручной режим.", chat_id=chat_id)
        return

    if pending.get("await") == "avito":
        pending["await"] = None
        return setup_replay("avito", text, chat_id)
    if pending.get("await") == "vk":
        pending["await"] = None
        return setup_vk(text, chat_id)

    if "curl" in low:
        return setup_replay("avito", text, chat_id)
    send("Не понял.\n\n" + HELP, chat_id=chat_id)


def setup_replay(name, blob, chat_id):
    try:
        req = parse_curl(blob)
    except Exception as e:
        send("❌ Не смог разобрать cURL: %s" % esc(e), chat_id=chat_id)
        return
    if not any(k.lower() == "cookie" for k in req["headers"]):
        send("❌ В этом cURL нет заголовка Cookie — скопируй запрос со "
             "страницы, где ты залогинен.", chat_id=chat_id)
        return
    conf = {"kind": "replay", "request": req, "enabled": True}
    if name == "vk" or looks_like_vk(req["url"]):
        conf["parser"] = "vk"
    old = (cfg.get("sources") or {}).get(name) or {}
    if old.get("proxy"):
        conf["proxy"] = old["proxy"]
    try:
        chats = ReplaySource(name, conf).poll()
    except Exception as e:
        send("❌ Проверка не прошла: %s\n\nХост: <code>%s</code>\n"
             "Похоже, это не тот запрос — нужен тот, что отдаёт список чатов."
             % (esc(e), esc(cookie_domain(req["url"]))), chat_id=chat_id)
        return
    cfg.setdefault("sources", {})[name] = conf
    save_cfg()
    state.pop(name, None)
    save_state()
    send("✅ <b>%s</b> подключён.\nХост: <code>%s</code>\nВижу чатов: %d\n\n"
         "Первый опрос запомнит текущее состояние молча, дальше пришлю "
         "только новое." % (esc(name), esc(cookie_domain(req["url"])),
                            len(chats)), chat_id=chat_id)


def setup_vk(blob, chat_id):
    blob = blob.strip()
    # cURL из DevTools -> куки-режим; иначе считаем, что прислали токен
    if blob.lower().startswith("curl") or "\nhost:" in blob.lower() \
            or "http://" in blob or "https://" in blob:
        return setup_replay("vk", blob, chat_id)
    token = blob.split()[0].strip()
    conf = {"kind": "vk", "token": token, "enabled": True}
    try:
        VkSource("vk", conf).poll()
    except Exception as e:
        send("❌ Токен не подошёл: %s" % esc(e), chat_id=chat_id)
        return
    cfg.setdefault("sources", {})["vk"] = conf
    save_cfg()
    state.pop("vk", None)
    save_state()
    send("✅ <b>VK</b> подключён.", chat_id=chat_id)


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
    print("режим        : %s%s" % (MODE, " (headless)" if HEADLESS else ""))
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
    if MODE == "browser":
        try:
            import playwright                                  # noqa: F401
            print("✓ playwright : установлен")
        except Exception:
            print("✗ playwright : не установлен (нужен для BOT_MODE=browser)")
            ok = False
    cid = cfg.get("chat_id")
    print("chat_id      : %s" % (cid or "ещё нет — напиши боту /start"))
    print("ИТОГ: %s" % ("всё готово" if ok else "есть проблемы"))
    return 0 if ok else 1


def main():
    if not TG_TOKEN:
        raise SystemExit("TG_TOKEN не задан (см. .env)")
    os.makedirs(DATA, exist_ok=True)
    if MODE == "browser" and not cfg.get("sources"):
        cfg["sources"] = json.loads(json.dumps(DEFAULT_SOURCES))
        cfg.setdefault("interval", 40)
        save_cfg()
    log("старт. режим=%s владелец=@%s источников=%d"
        % (MODE, OWNER, len(cfg.get("sources") or {})))
    threading.Thread(target=source_loop, daemon=True).start()
    telegram_loop()


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    main()
