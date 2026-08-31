import asyncio
import hashlib
import json
import os
import platform
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode
import urllib.request

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "pingusha.db")
SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))
SESSION_COOKIE = "pingusha_session"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'manager'
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sites (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            lat        REAL NOT NULL,
            lng        REAL NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS devices (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id        INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            parent_id      INTEGER REFERENCES devices(id) ON DELETE CASCADE,
            name           TEXT NOT NULL,
            type           TEXT NOT NULL,
            ip             TEXT,
            ping_interval  INTEGER NOT NULL DEFAULT 60,
            must_be_online INTEGER NOT NULL DEFAULT 1,
            status         TEXT NOT NULL DEFAULT 'unknown',
            last_checked   TEXT,
            sort_order     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS status_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            old_status TEXT,
            new_status TEXT NOT NULL,
            changed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notifications_config (
            id             INTEGER PRIMARY KEY DEFAULT 1,
            telegram_token TEXT,
            telegram_chat  TEXT,
            email_smtp     TEXT,
            enabled        INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS user_site_access (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, site_id)
        );

        CREATE TABLE IF NOT EXISTS telegram_bindings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            binding_key  TEXT UNIQUE NOT NULL,
            chat_id      INTEGER NOT NULL,
            username     TEXT,
            user_id      INTEGER REFERENCES users(id) ON DELETE CASCADE,
            bound_at     TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notify_config (
            user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            control_time    TEXT NOT NULL DEFAULT '10:00',
            control_enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS notify_sites (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, site_id)
        );

        CREATE TABLE IF NOT EXISTS notify_sent (
            user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            sent_at   TEXT NOT NULL,
            PRIMARY KEY (user_id, device_id)
        );

        CREATE TABLE IF NOT EXISTS control_sent (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            date    TEXT NOT NULL,
            PRIMARY KEY (user_id, date)
        );
    """)
    # Первый запуск: создаём ТОЛЬКО admin.
    # Пароль берём из ADMIN_PASSWORD (env); если не задан — генерируем и печатаем в консоль.
    if cur.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        pw = os.environ.get("ADMIN_PASSWORD", "").strip() or secrets.token_urlsafe(12)
        cur.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("admin", hashlib.sha256(pw.encode()).hexdigest(), "admin"),
        )
        print("=" * 50)
        print(f"  Первый запуск Pingusha!")
        print(f"  Пользователь: admin")
        print(f"  Пароль: {pw}")
        print("  Смените пароль в настройках после входа!")
        print("=" * 50)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Auth — cookie sessions
# ---------------------------------------------------------------------------

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def get_session_user(
    pingusha_session: Optional[str] = Cookie(default=None),
    db=Depends(get_db),
):
    if not pingusha_session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    row = db.execute(
        "SELECT s.token, s.expires_at, u.id, u.username, u.role, u.password_hash "
        "FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.token=?",
        (pingusha_session,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid session")
    if datetime.fromisoformat(row["expires_at"]) < datetime.now():
        db.execute("DELETE FROM sessions WHERE token=?", (pingusha_session,))
        db.commit()
        raise HTTPException(status_code=401, detail="Session expired")
    return dict(row)


def require_admin(user=Depends(get_session_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return user


def can_view_site(user, site_id, db):
    """Право пользователя на просмотр объекта: админ видит всё, менеджер — только назначенные."""
    if user["role"] == "admin":
        return True
    row = db.execute(
        "SELECT 1 FROM user_site_access WHERE user_id=? AND site_id=?",
        (user["id"], site_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

async def ping_host(ip: str) -> bool:
    if not ip:
        return False
    param = "-n" if platform.system().lower() == "windows" else "-c"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", param, "1", "-W", "2", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        return proc.returncode == 0
    except Exception:
        return False


def log_status_change(conn, device_id: int, old_status: str, new_status: str):
    if old_status == new_status:
        return
    conn.execute(
        "INSERT INTO status_log (device_id, old_status, new_status, changed_at) VALUES (?,?,?,?)",
        (device_id, old_status, new_status, datetime.now().isoformat()),
    )
    conn.execute("""
        DELETE FROM status_log WHERE device_id=? AND id NOT IN (
            SELECT id FROM status_log WHERE device_id=? ORDER BY id DESC LIMIT 1000
        )
    """, (device_id, device_id))


# ---------------------------------------------------------------------------
# Background poller
# ---------------------------------------------------------------------------

_last_ping: dict[int, float] = {}


async def poller_loop():
    while True:
        await asyncio.sleep(5)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            devices = conn.execute(
                "SELECT * FROM devices WHERE type != 'switch' AND ip IS NOT NULL AND ip != ''"
            ).fetchall()
            now = time.time()
            to_ping = [dict(d) for d in devices if now - _last_ping.get(d["id"], 0) >= d["ping_interval"]]
            if to_ping:
                results = await asyncio.gather(*[ping_host(d["ip"]) for d in to_ping])
                for dev, alive in zip(to_ping, results):
                    new_status = "online" if alive else "offline"
                    conn.execute(
                        "UPDATE devices SET status=?, last_checked=? WHERE id=?",
                        (new_status, datetime.now().isoformat(), dev["id"]),
                    )
                    log_status_change(conn, dev["id"], dev["status"], new_status)
                    _last_ping[dev["id"]] = now
            # Clean expired sessions
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.now().isoformat(),))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Poller error: {e}")


# ---------------------------------------------------------------------------
# Telegram notifications
# ---------------------------------------------------------------------------

OFFLINE_NOTIFY_DELAY_S = 600  # устройство должно быть offline 10+ минут, чтобы попасть в уведомление
NOTIFY_BATCH_INTERVAL_S = 600  # не чаще раза в 10 минут: всё накопленное отправляется одним сообщением
_last_batch_sent = {}  # user_id -> timestamp последней отправки

def get_bot_token():
    # Токен бота хранится в окружении контейнера (docker-compose: TELEGRAM_TOKEN)
    env_token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if env_token:
        return env_token
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT telegram_token FROM notifications_config WHERE id=1").fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def tg_api(method: str, token: str, **params):
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        req = urllib.request.Request(url, data=urlencode(params).encode())
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def send_telegram(chat_id, text: str, keyboard=None):
    token = get_bot_token()
    if not token:
        return False
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = json.dumps({
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": False,
        })
    res = tg_api("sendMessage", token, **params)
    return bool(res and res.get("ok"))


CONTROL_BTN = "📊 Контроль"


def bot_loop():
    """Фоновый поток: long-polling Telegram; /start выдаёт ключ привязки."""
    offset = 0
    while True:
        time.sleep(2)
        token = get_bot_token()
        if not token:
            continue
        data = tg_api("getUpdates", token, offset=offset, timeout=30)
        if not data or not data.get("ok"):
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            first = msg["from"].get("first_name", "")
            last = msg["from"].get("last_name", "")
            uname = msg["from"].get("username", "")
            display = (f"{first} {last}").strip() or uname or f"user{chat_id}"
            text = (msg.get("text") or "").strip()
            if text.startswith("/start"):
                # Приветствие + инструкция + кнопка «Контроль», затем ключ отдельным сообщением
                send_telegram(chat_id,
                    "👋 Привет! Это бот Пингушa.\n\n"
                    "Скопируйте следующее сообщение (ключ) и вставьте его в "
                    "Пингушa → Настройки → Уведомления → Привязать.\n\n"
                    "Кнопка «Контроль» в любой момент пришлёт статус всех ваших объектов.",
                    keyboard=[[CONTROL_BTN]])
                key = secrets.token_hex(18)  # 36 символов
                conn = sqlite3.connect(DB_PATH)
                conn.execute(
                    "INSERT INTO telegram_bindings (binding_key, chat_id, username, created_at) VALUES (?,?,?,?)",
                    (key, chat_id, display, datetime.now().isoformat()),
                )
                conn.commit()
                conn.close()
                send_telegram(chat_id, key)
            elif text.strip() == CONTROL_BTN:
                # Кнопка «Контроль»: статус ВСЕХ объектов, доступных аккаунту
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT user_id FROM telegram_bindings WHERE chat_id=? AND user_id IS NOT NULL",
                    (chat_id,),
                ).fetchone()
                conn.close()
                if not row:
                    send_telegram(chat_id,
                        "Аккаунт не привязан. Нажмите /start, получите ключ и вставьте его в "
                        "Пингушу (Настройки → Уведомления).",
                        keyboard=[[CONTROL_BTN]])
                else:
                    send_telegram(chat_id, build_all_summary_text(row["user_id"]))
            elif text:
                # Прочее сообщение — короткая подсказка
                send_telegram(chat_id, "Используйте /start или кнопку «Контроль».",
                              keyboard=[[CONTROL_BTN]])


def user_site_ids(user_id: int):
    """Объекты, на которые у пользователя есть право (админ — все)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    u = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    if u and u["role"] == "admin":
        rows = conn.execute("SELECT id AS site_id FROM sites").fetchall()
        conn.close()
        return [r["site_id"] for r in rows]
    rows = conn.execute(
        "SELECT site_id FROM user_site_access WHERE user_id=?", (user_id,)
    ).fetchall()
    conn.close()
    return [r["site_id"] for r in rows]


def notify_site_ids(user_id: int, allowed_site_ids):
    """Объекты для уведомлений: пересечение галочек и прав. По умолчанию — все доступные."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT site_id FROM notify_sites WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    checked = {r[0] for r in rows}
    if not checked:
        return list(allowed_site_ids)  # галочки не трогали — все доступные
    return [sid for sid in allowed_site_ids if sid in checked]


DEVICE_ICONS = {
    "gateway": "🌐", "switch_managed": "🔀", "switch": "🔀",
    "wifi_bridge": "📶", "access_point": "🛜",
    "device": "🖥️", "camera": "🎥", "nvr": "📼",
}


def build_site_tree_text(site_id: int) -> str:
    """Полное дерево объекта со статусами — для Telegram."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    site = conn.execute("SELECT name FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        conn.close()
        return ""
    devs = conn.execute(
        "SELECT * FROM devices WHERE site_id=? ORDER BY sort_order, id", (site_id,)
    ).fetchall()
    conn.close()
    by_parent = {}
    for d in devs:
        by_parent.setdefault(d["parent_id"] or 0, []).append(d)

    def stat(d):
        if d["type"] == "switch":  # неуправляемый — без пинга
            return "⚪"
        return {"online": "✅", "offline": "🔴", "unknown": "⚪"}.get(d["status"], "⚪")

    def walk(pid, prefix):
        lines = []
        kids = by_parent.get(pid, [])
        for i, d in enumerate(kids):
            last = i == len(kids) - 1
            branch = "└─ " if last else "├─ "
            lines.append(f"{prefix}{branch}{DEVICE_ICONS.get(d['type'], '📡')} {d['name']} {stat(d)}")
            lines.extend(walk(d["id"], prefix + ("   " if last else "│  ")))
        return lines

    lines = [f"🌐 {site['name']}"]
    lines.extend(walk(0, ""))
    return "\n".join(lines)


def build_summary_text(user_id: int) -> str:
    """Сводка по объектам пользователя из подписки (для контроля и теста)."""
    allowed = user_site_ids(user_id)
    site_ids = notify_site_ids(user_id, allowed)
    lines = [f"📋 Контроль {datetime.now().strftime('%H:%M')} — Пингушa"]
    for sid in site_ids:
        lines.append("\n" + build_site_tree_text(sid))
    return "\n".join(lines)


def build_all_summary_text(user_id: int) -> str:
    """Сводка по ВСЕМ объектам, доступным пользователю (кнопка «Контроль» в боте)."""
    site_ids = user_site_ids(user_id)
    lines = [f"📊 Контроль {datetime.now().strftime('%H:%M')} — Пингушa"]
    for sid in site_ids:
        lines.append("\n" + build_site_tree_text(sid))
    return "\n".join(lines)


def check_offline_notifications():
    """Раз в минуту: все устройства offline 5+ минут собираются в ОДНО уведомление."""
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # все offline-устройства, которые уже 5+ минут не в сети
    devices = conn.execute(
        "SELECT d.id, d.name, d.site_id, s.name AS site_name FROM devices d "
        "JOIN sites s ON s.id = d.site_id WHERE d.status='offline' AND d.must_be_online=1"
    ).fetchall()
    pending = []
    for dev in devices:
        row = conn.execute(
            "SELECT changed_at FROM status_log WHERE device_id=? AND new_status='offline' "
            "ORDER BY id DESC LIMIT 1", (dev["id"],)
        ).fetchone()
        if not row:
            continue
        try:
            changed = datetime.fromisoformat(row["changed_at"])
        except Exception:
            continue
        if (now - changed).total_seconds() < OFFLINE_NOTIFY_DELAY_S:
            continue
        pending.append(dev)
    # кому слать: привязанные пользователи с правом на объект и галочкой
    users = conn.execute(
        "SELECT u.id, tb.chat_id FROM users u JOIN telegram_bindings tb ON tb.user_id=u.id "
        "WHERE tb.user_id IS NOT NULL"
    ).fetchall()
    for u in users:
        allowed = user_site_ids(u["id"])
        site_ids = notify_site_ids(u["id"], allowed)
        # устройства пользователя, про которые ещё не уведомляли
        my_pending = []
        for d in pending:
            if d["site_id"] not in site_ids:
                continue
            already = conn.execute(
                "SELECT 1 FROM notify_sent WHERE user_id=? AND device_id=?", (u["id"], d["id"])
            ).fetchone()
            if not already:
                my_pending.append(d)
        if not my_pending:
            continue
        # Окно сбора: не чаще раза в 10 минут, всё накопленное — одним сообщением
        last_sent = _last_batch_sent.get(u["id"], 0)
        if now.timestamp() - last_sent < NOTIFY_BATCH_INTERVAL_S:
            continue
        # ОДНО сообщение: все устройства сгруппированы по объектам + дерево каждого объекта
        by_site = {}
        for d in my_pending:
            by_site.setdefault(d["site_id"], []).append(d)
        parts = ["🔴 Устройства не в сети более 5 минут:"]
        for sid in sorted(by_site):
            site_name = by_site[sid][0]["site_name"]
            names = ", ".join(f"«{d['name']}»" for d in by_site[sid])
            parts.append(f"\n🌐 {site_name}: {names}")
            parts.append(build_site_tree_text(sid))
        send_telegram(u["chat_id"], "\n".join(parts))
        _last_batch_sent[u["id"]] = now.timestamp()
        for d in my_pending:
            conn.execute(
                "INSERT OR IGNORE INTO notify_sent (user_id, device_id, sent_at) VALUES (?,?,?)",
                (u["id"], d["id"], now.isoformat()),
            )
        conn.commit()
    # сброс флагов для вернувшихся в сеть
    conn.execute(
        "DELETE FROM notify_sent WHERE device_id IN "
        "(SELECT id FROM devices WHERE status='online')"
    )
    conn.commit()
    conn.close()


def check_control_messages():
    """Ежедневное контрольное сообщение со статусами всех устройств."""
    now = datetime.now()
    cur_time = now.strftime("%H:%M")
    today = now.date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    users = conn.execute(
        "SELECT u.id, tb.chat_id, tb.username, nc.control_time FROM users u "
        "JOIN telegram_bindings tb ON tb.user_id=u.id "
        "JOIN notify_config nc ON nc.user_id=u.id "
        "WHERE tb.user_id IS NOT NULL AND nc.control_enabled=1"
    ).fetchall()
    for u in users:
        if u["control_time"] != cur_time:
            continue
        sent = conn.execute(
            "SELECT 1 FROM control_sent WHERE user_id=? AND date=?", (u["id"], today)
        ).fetchone()
        if sent:
            continue
        allowed = user_site_ids(u["id"])
        site_ids = notify_site_ids(u["id"], allowed)
        lines = [f"📋 Контроль {cur_time} — Пингушa"]
        for sid in site_ids:
            lines.append("\n" + build_site_tree_text(sid))
        send_telegram(u["chat_id"], "\n".join(lines))
        conn.execute(
            "INSERT OR IGNORE INTO control_sent (user_id, date) VALUES (?,?)", (u["id"], today)
        )
        conn.commit()
    conn.close()


def notification_loop():
    while True:
        time.sleep(60)
        try:
            check_offline_notifications()
            check_control_messages()
        except Exception as e:
            print(f"Notification error: {e}")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    task = asyncio.create_task(poller_loop())
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=notification_loop, daemon=True).start()
    yield
    task.cancel()


VERSION = "1.0.1"


app = FastAPI(title="Pingusha", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LoginBody(BaseModel):
    username: str
    password: str

class SiteCreate(BaseModel):
    name: str
    lat: float
    lng: float

class SiteUpdate(BaseModel):
    name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None

class DeviceCreate(BaseModel):
    site_id: int
    parent_id: Optional[int] = None
    name: str
    type: str
    ip: Optional[str] = None
    ping_interval: int = 60
    must_be_online: bool = True
    sort_order: int = 0

class DeviceUpdate(BaseModel):
    parent_id: Optional[int] = None
    name: Optional[str] = None
    type: Optional[str] = None
    ip: Optional[str] = None
    ping_interval: Optional[int] = None
    must_be_online: Optional[bool] = None
    sort_order: Optional[int] = None

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "manager"

class UserSitesBody(BaseModel):
    site_ids: list[int] = []

class BindBody(BaseModel):
    key: str

class NotifyConfigBody(BaseModel):
    control_time: Optional[str] = None
    control_enabled: Optional[bool] = None
    site_ids: Optional[list[int]] = None

class TokenBody(BaseModel):
    token: str = ""

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class ImportBody(BaseModel):
    data: dict


# ---------------------------------------------------------------------------
# Site status computation
# ---------------------------------------------------------------------------

def compute_site_status(devices: list[dict]) -> str:
    gateway = next((d for d in devices if d["type"] == "gateway"), None)
    if gateway is None:
        return "unknown"
    if gateway["status"] == "unknown":
        return "unknown"
    if gateway["status"] == "offline":
        return "red"

    # Устройства с реальным статусом (пингуемые), кроме шлюза и неуправляемых коммутаторов
    monitored = [
        d for d in devices
        if d["id"] != gateway["id"]
        and d["type"] != "switch"
        and d["status"] in ("online", "offline")
    ]
    if not monitored:
        return "green"
    offline = [d for d in monitored if d["status"] == "offline"]
    if not offline:
        return "green"
    if len(offline) >= len(monitored):
        return "red"      # всё отвалилось
    return "yellow"       # что-то отвалилось


# ---------------------------------------------------------------------------
# Routes — Login / Logout / Me
# ---------------------------------------------------------------------------

# Защита от перебора паролей: 5 неудачных попыток с одного IP -> блокировка 5 минут
_login_attempts = {}
MAX_LOGIN_ATTEMPTS = 5
LOGIN_BLOCK_MINUTES = 5

@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response, db=Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    rec = _login_attempts.get(ip)
    if rec and rec.get("blocked_until") and now < rec["blocked_until"]:
        wait_min = int((rec["blocked_until"] - now) / 60) + 1
        raise HTTPException(status_code=429,
                            detail=f"Слишком много попыток входа. Подождите {wait_min} мин.")
    row = db.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not row or row["password_hash"] != hash_password(body.password):
        rec = _login_attempts.setdefault(ip, {"count": 0, "blocked_until": 0})
        rec["count"] += 1
        if rec["count"] >= MAX_LOGIN_ATTEMPTS:
            rec["blocked_until"] = now + LOGIN_BLOCK_MINUTES * 60
            rec["count"] = 0
        time.sleep(0.4)  # дополнительное замедление перебора
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # успешный вход — сбрасываем счётчик
    _login_attempts.pop(ip, None)
    token = secrets.token_hex(32)
    expires = (datetime.now() + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    db.execute(
        "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (token, row["id"], expires, datetime.now().isoformat()),
    )
    db.commit()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_DAYS * 86400,
        path="/",
    )
    return {"username": row["username"], "role": row["role"]}


@app.post("/api/logout")
def logout(response: Response, pingusha_session: Optional[str] = Cookie(default=None), db=Depends(get_db)):
    if pingusha_session:
        db.execute("DELETE FROM sessions WHERE token=?", (pingusha_session,))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(get_session_user)):
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/change-password")
def change_password(body: PasswordChange, response: Response, user=Depends(get_session_user), db=Depends(get_db)):
    if hash_password(body.old_password) != user["password_hash"]:
        raise HTTPException(400, "Wrong current password")
    db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(body.new_password), user["id"]))
    # Invalidate all other sessions for this user
    db.execute("DELETE FROM sessions WHERE user_id=? AND token!=?", (user["id"], user["pingusha_session"] if "pingusha_session" in user else ""))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Sites
# ---------------------------------------------------------------------------

@app.get("/api/sites")
def list_sites(user=Depends(get_session_user), db=Depends(get_db)):
    if user["role"] == "admin":
        rows = db.execute("SELECT * FROM sites ORDER BY name").fetchall()
    else:
        # Менеджер видит только назначенные ему объекты
        rows = db.execute(
            "SELECT s.* FROM sites s JOIN user_site_access a ON a.site_id = s.id "
            "WHERE a.user_id = ? ORDER BY s.name",
            (user["id"],),
        ).fetchall()
    sites = [dict(r) for r in rows]
    for site in sites:
        devices = [dict(r) for r in db.execute("SELECT * FROM devices WHERE site_id=?", (site["id"],)).fetchall()]
        site["status"] = compute_site_status(devices)
        site["devices_total"] = len(devices)
        site["devices_online"] = sum(1 for d in devices if d["status"] == "online")
        site["devices_offline"] = sum(1 for d in devices if d["status"] == "offline" and d["must_be_online"])
        site["devices_unknown"] = sum(1 for d in devices if d["status"] not in ("online", "offline"))
        site["device_count"] = len(devices)
        site["offline_count"] = sum(
            1 for d in devices
            if d["type"] != "switch" and d["status"] == "offline" and d["must_be_online"]
        )
    return sites


@app.post("/api/sites")
def create_site(body: SiteCreate, user=Depends(require_admin), db=Depends(get_db)):
    cur = db.execute(
        "INSERT INTO sites (name, lat, lng, created_at) VALUES (?,?,?,?)",
        (body.name, body.lat, body.lng, datetime.now().isoformat()),
    )
    new_id = cur.lastrowid
    # Новый объект автоматически включается в уведомления всех пользователей
    for (uid,) in db.execute("SELECT id FROM users").fetchall():
        db.execute(
            "INSERT OR IGNORE INTO notify_sites (user_id, site_id) VALUES (?,?)",
            (uid, new_id),
        )
    db.commit()
    return {"id": new_id, **body.dict()}


@app.put("/api/sites/{site_id}")
def update_site(site_id: int, body: SiteUpdate, user=Depends(require_admin), db=Depends(get_db)):
    fields = {k: v for k, v in body.dict().items() if v is not None}
    if not fields:
        raise HTTPException(400, "Nothing to update")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE sites SET {set_clause} WHERE id=?", (*fields.values(), site_id))
    db.commit()
    return {"ok": True}


@app.delete("/api/sites/{site_id}")
def delete_site(site_id: int, user=Depends(require_admin), db=Depends(get_db)):
    db.execute("DELETE FROM sites WHERE id=?", (site_id,))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Devices
# ---------------------------------------------------------------------------

@app.get("/api/sites/{site_id}/devices")
def list_devices(site_id: int, user=Depends(get_session_user), db=Depends(get_db)):
    if not can_view_site(user, site_id, db):
        raise HTTPException(status_code=403, detail="No access to site")
    rows = db.execute("SELECT * FROM devices WHERE site_id=? ORDER BY sort_order, id", (site_id,)).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/devices")
def create_device(body: DeviceCreate, user=Depends(require_admin), db=Depends(get_db)):
    # Авто-порядок: новое устройство встаёт в конец своего уровня (sort_order = max+1 среди siblings)
    max_so = db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) FROM devices WHERE site_id=? AND parent_id IS ?",
        (body.site_id, body.parent_id),
    ).fetchone()[0]
    so = max_so + 1 if body.sort_order == 0 else body.sort_order
    cur = db.execute(
        "INSERT INTO devices (site_id, parent_id, name, type, ip, ping_interval, must_be_online, sort_order) VALUES (?,?,?,?,?,?,?,?)",
        (body.site_id, body.parent_id, body.name, body.type, body.ip, body.ping_interval, int(body.must_be_online), so),
    )
    db.commit()
    return {"id": cur.lastrowid, **body.dict()}


@app.put("/api/devices/{device_id}")
def update_device(device_id: int, body: DeviceUpdate, user=Depends(require_admin), db=Depends(get_db)):
    data = body.dict(exclude_unset=True)
    if "must_be_online" in data:
        data["must_be_online"] = int(data["must_be_online"])
    if not data:
        raise HTTPException(400, "Nothing to update")
    set_clause = ", ".join(f"{k}=?" for k in data)
    db.execute(f"UPDATE devices SET {set_clause} WHERE id=?", (*data.values(), device_id))
    db.commit()
    return {"ok": True}


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: int, user=Depends(require_admin), db=Depends(get_db)):
    db.execute("DELETE FROM devices WHERE id=?", (device_id,))
    db.commit()
    return {"ok": True}


@app.get("/api/devices/{device_id}/log")
def device_log(device_id: int, user=Depends(get_session_user), db=Depends(get_db)):
    dev = db.execute("SELECT site_id FROM devices WHERE id=?", (device_id,)).fetchone()
    if not dev:
        raise HTTPException(404, "Device not found")
    if not can_view_site(user, dev["site_id"], db):
        raise HTTPException(status_code=403, detail="No access to site")
    rows = db.execute(
        "SELECT * FROM status_log WHERE device_id=? ORDER BY id DESC LIMIT 100", (device_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes — Users
# ---------------------------------------------------------------------------

@app.get("/api/users")
def list_users(user=Depends(require_admin), db=Depends(get_db)):
    rows = db.execute("SELECT id, username, role FROM users").fetchall()
    result = []
    for r in rows:
        u = dict(r)
        u["site_ids"] = [x[0] for x in db.execute(
            "SELECT site_id FROM user_site_access WHERE user_id=?", (u["id"],)
        ).fetchall()]
        result.append(u)
    return result


@app.post("/api/users")
def create_user(body: UserCreate, user=Depends(require_admin), db=Depends(get_db)):
    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            (body.username, hash_password(body.password), body.role),
        )
        db.commit()
        return {"id": cur.lastrowid, "username": body.username, "role": body.role}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Username already exists")


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, user=Depends(require_admin), db=Depends(get_db)):
    if user_id == user["id"]:
        raise HTTPException(400, "Нельзя удалить самого себя")
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    return {"ok": True}


@app.put("/api/users/{user_id}/sites")
def set_user_sites(user_id: int, body: UserSitesBody, user=Depends(require_admin), db=Depends(get_db)):
    """Назначить менеджеру права на просмотр объектов."""
    target = db.execute("SELECT id, role FROM users WHERE id=?", (user_id,)).fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    db.execute("DELETE FROM user_site_access WHERE user_id=?", (user_id,))
    for sid in body.site_ids:
        db.execute(
            "INSERT OR IGNORE INTO user_site_access (user_id, site_id) VALUES (?,?)",
            (user_id, sid),
        )
    db.commit()
    return {"ok": True, "site_ids": body.site_ids}


# ---------------------------------------------------------------------------
# Routes — Notifications (Telegram)
# ---------------------------------------------------------------------------

@app.get("/api/notifications")
def get_notifications(user=Depends(get_session_user), db=Depends(get_db)):
    bind = db.execute(
        "SELECT chat_id, username FROM telegram_bindings WHERE user_id=? AND user_id IS NOT NULL",
        (user["id"],),
    ).fetchone()
    cfg = db.execute(
        "SELECT control_time, control_enabled FROM notify_config WHERE user_id=?", (user["id"],)
    ).fetchone()
    checked = [r[0] for r in db.execute(
        "SELECT site_id FROM notify_sites WHERE user_id=?", (user["id"],)
    ).fetchall()]
    # все объекты, к которым есть доступ
    if user["role"] == "admin":
        all_sites = [dict(r) for r in db.execute("SELECT id, name FROM sites ORDER BY name").fetchall()]
    else:
        all_sites = [dict(r) for r in db.execute(
            "SELECT s.id, s.name FROM sites s JOIN user_site_access a ON a.site_id=s.id "
            "WHERE a.user_id=? ORDER BY s.name", (user["id"],)
        ).fetchall()]
    # если галочки не настроены — показываем все объекты включёнными
    if not checked:
        checked = [s["id"] for s in all_sites]
    # username бота для ссылки
    token = get_bot_token()
    bot_username = None
    if token:
        me = tg_api("getMe", token)
        if me and me.get("ok"):
            bot_username = me["result"].get("username")
    return {
        "bound": bool(bind),
        "telegram_username": bind["username"] if bind else None,
        "control_time": cfg["control_time"] if cfg else "10:00",
        "control_enabled": bool(cfg["control_enabled"]) if cfg else True,
        "site_ids_checked": checked,
        "sites": all_sites,
        "bot_username": bot_username,
    }


@app.post("/api/notifications/bind")
def bind_telegram(body: BindBody, user=Depends(get_session_user), db=Depends(get_db)):
    key = body.key.strip()
    row = db.execute(
        "SELECT id, chat_id, username FROM telegram_bindings WHERE binding_key=? AND user_id IS NULL",
        (key,),
    ).fetchone()
    if not row:
        raise HTTPException(400, "Неверный ключ. Нажмите /start у бота, чтобы получить новый.")
    # снять старые привязки пользователя
    db.execute("UPDATE telegram_bindings SET user_id=NULL WHERE user_id=?", (user["id"],))
    db.execute(
        "UPDATE telegram_bindings SET user_id=?, bound_at=? WHERE id=?",
        (user["id"], datetime.now().isoformat(), row["id"]),
    )
    # дефолтный конфиг
    db.execute(
        "INSERT OR IGNORE INTO notify_config (user_id) VALUES (?)", (user["id"],)
    )
    # по умолчанию в уведомления включены ВСЕ доступные объекты
    allowed = user_site_ids(user["id"])
    for sid in allowed:
        db.execute(
            "INSERT OR IGNORE INTO notify_sites (user_id, site_id) VALUES (?,?)",
            (user["id"], sid),
        )
    db.commit()
    send_telegram(row["chat_id"], "✅ Привязка успешна! Уведомления Пингушa включены.")
    return {"telegram_username": row["username"]}


@app.post("/api/notifications/unbind")
def unbind_telegram(user=Depends(get_session_user), db=Depends(get_db)):
    bind = db.execute(
        "SELECT chat_id FROM telegram_bindings WHERE user_id=?", (user["id"],)
    ).fetchone()
    if bind:
        send_telegram(bind["chat_id"], "🔓 Привязка отменена. Уведомления отключены.")
    db.execute("UPDATE telegram_bindings SET user_id=NULL WHERE user_id=?", (user["id"],))
    db.execute("DELETE FROM notify_sent WHERE user_id=?", (user["id"],))
    db.execute("DELETE FROM control_sent WHERE user_id=?", (user["id"],))
    db.commit()
    return {"ok": True}


@app.put("/api/notifications/config")
def update_notifications(body: NotifyConfigBody, user=Depends(get_session_user), db=Depends(get_db)):
    db.execute(
        "INSERT OR IGNORE INTO notify_config (user_id) VALUES (?)", (user["id"],)
    )
    if body.control_time is not None:
        db.execute(
            "UPDATE notify_config SET control_time=? WHERE user_id=?",
            (body.control_time, user["id"]),
        )
    if body.control_enabled is not None:
        db.execute(
            "UPDATE notify_config SET control_enabled=? WHERE user_id=?",
            (int(body.control_enabled), user["id"]),
        )
    if body.site_ids is not None:
        db.execute("DELETE FROM notify_sites WHERE user_id=?", (user["id"],))
        for sid in body.site_ids:
            db.execute(
                "INSERT OR IGNORE INTO notify_sites (user_id, site_id) VALUES (?,?)",
                (user["id"], sid),
            )
    db.commit()
    return {"ok": True}


@app.post("/api/notifications/test")
def test_notification(user=Depends(get_session_user), db=Depends(get_db)):
    bind = db.execute(
        "SELECT chat_id FROM telegram_bindings WHERE user_id=?", (user["id"],)
    ).fetchone()
    if not bind:
        raise HTTPException(400, "Сначала привяжите Telegram")
    ok = send_telegram(bind["chat_id"], build_summary_text(user["id"]))
    if not ok:
        raise HTTPException(500, "Ошибка отправки (проверьте токен бота)")
    return {"ok": True}


@app.get("/api/notifications/token")
def get_notification_token(user=Depends(require_admin), db=Depends(get_db)):
    row = db.execute("SELECT telegram_token FROM notifications_config WHERE id=1").fetchone()
    return {"token_set": bool(row and row["telegram_token"])}


@app.put("/api/notifications/token")
def set_notification_token(body: TokenBody, user=Depends(require_admin), db=Depends(get_db)):
    db.execute(
        "INSERT OR IGNORE INTO notifications_config (id, telegram_token) VALUES (1, '')"
    )
    db.execute(
        "UPDATE notifications_config SET telegram_token=? WHERE id=1", (body.token.strip(),)
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — Export / Import
# ---------------------------------------------------------------------------

@app.get("/api/sites/{site_id}/export")
def export_site(site_id: int, user=Depends(get_session_user), db=Depends(get_db)):
    if not can_view_site(user, site_id, db):
        raise HTTPException(status_code=403, detail="No access to site")
    site = db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "Site not found")
    devices = [dict(r) for r in db.execute(
        "SELECT * FROM devices WHERE site_id=? ORDER BY id", (site_id,)
    ).fetchall()]
    payload = {"pingusha_export_version": 1, "site": dict(site), "devices": devices}
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="site_{site_id}.json"'},
    )


@app.post("/api/import")
def import_site(body: ImportBody, user=Depends(require_admin), db=Depends(get_db)):
    data = body.data
    if data.get("pingusha_export_version") != 1:
        raise HTTPException(400, "Invalid export file")
    site_data = data["site"]
    devices_data = data["devices"]
    cur = db.execute(
        "INSERT INTO sites (name, lat, lng, created_at) VALUES (?,?,?,?)",
        (site_data["name"], site_data["lat"], site_data["lng"], datetime.now().isoformat()),
    )
    new_site_id = cur.lastrowid
    id_map = {}
    for dev in sorted(devices_data, key=lambda d: d["id"]):
        old_id = dev["id"]
        new_parent_id = id_map.get(dev["parent_id"]) if dev["parent_id"] else None
        c = db.execute(
            "INSERT INTO devices (site_id, parent_id, name, type, ip, ping_interval, must_be_online, sort_order) VALUES (?,?,?,?,?,?,?,?)",
            (new_site_id, new_parent_id, dev["name"], dev["type"], dev.get("ip"),
             dev.get("ping_interval", 60), dev.get("must_be_online", 1), dev.get("sort_order", 0)),
        )
        id_map[old_id] = c.lastrowid
    db.commit()
    return {"ok": True, "new_site_id": new_site_id}


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-store, max-age=0"})
