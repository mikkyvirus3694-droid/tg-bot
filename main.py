#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⚡ OPTIMIZED & STABLE Telegram Bot - YourMikk
+ Maintenance mode now **immediately** blocks all callbacks and messages.
+ Old inline buttons show "Maintenance" popup instead of processing.
+ After maintenance off, sending "hello" always starts a fresh conversation.
"""

import os
import sqlite3
import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 123456789))
DB_PATH = "data/bot_data.db"

RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60
CACHE_TTL = 60

SELECT_MEDIA, WAIT_MEDIA, ASK_LINK, WAIT_LINK, ASK_TWITTER, WAIT_TWITTER = range(6)

START_TIME = time.time()
_rate_limits = defaultdict(list)
_cache = {}
_cache_ts = {}
_active_reply = None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= SAFE DATABASE LAYER =================
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    os.makedirs("data", exist_ok=True)
    with db() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, username TEXT, first_name TEXT,
                link TEXT, twitter TEXT, media_type TEXT,
                media_file_id TEXT, status TEXT DEFAULT 'pending',
                admin_msg_chat_id INTEGER, admin_msg_id INTEGER,
                created_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_sub_user ON submissions(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sub_status ON submissions(status);
            
            CREATE TABLE IF NOT EXISTS blocked_users (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS active_reply (submission_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT);
            
            CREATE TABLE IF NOT EXISTS user_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, action TEXT, details TEXT, created_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_act_time ON user_activity(created_at DESC);
            
            CREATE TABLE IF NOT EXISTS auto_reply (
                trigger_word TEXT PRIMARY KEY COLLATE NOCASE, reply_text TEXT);
        ''')
        try:
            c.execute("ALTER TABLE submissions ADD COLUMN user_reply INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        c.executemany("INSERT OR IGNORE INTO bot_settings VALUES (?,?)", [
            ('welcome_msg', 'Hello @{username}! I am YourMikk bot.\nWhat do you want me to promote?'),
            ('accept_msg', '🎉 Congratulations! Your post is accepted. It will be posted soon.'),
            ('reject_msg', '😔 Sorry, I can\'t post this media.'),
            ('require_twitter', 'no'), ('require_link', 'no'),
            ('maintenance_mode', 'off')
        ])

def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()
    _cache.clear()
    _cache_ts.clear()
    global _active_reply
    _active_reply = None
    _rate_limits.clear()

# ================= CACHE SYSTEM =================
def cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    result = fn()
    _cache[key] = result
    _cache_ts[key] = now
    return result

def invalidate(key=None):
    if key:
        _cache.pop(key, None)
        _cache_ts.pop(key, None)
    else:
        _cache.clear()
        _cache_ts.clear()

# ================= DB OPERATIONS =================
def _get_setting_db(k):
    with db() as c:
        r = c.execute("SELECT value FROM bot_settings WHERE key=?", (k,)).fetchone()
        return r['value'] if r else None

def get_setting(k):
    return cached(f"set_{k}", lambda k=k: _get_setting_db(k))

def set_setting(k, v):
    with db() as c: c.execute("INSERT OR REPLACE INTO bot_settings VALUES (?,?)", (k, v))
    invalidate(f"set_{k}")

def _is_blocked_db(uid):
    with db() as c:
        return c.execute("SELECT 1 FROM blocked_users WHERE user_id=?", (uid,)).fetchone() is not None

def is_blocked(uid):
    return cached(f"blk_{uid}", lambda uid=uid: _is_blocked_db(uid), 30)

def block_user(uid):
    with db() as c: c.execute("INSERT OR IGNORE INTO blocked_users VALUES (?)", (uid,))
    invalidate(f"blk_{uid}")
    log_act(uid, "blocked", "admin")

def unblock_user(uid):
    with db() as c: c.execute("DELETE FROM blocked_users WHERE user_id=?", (uid,))
    invalidate(f"blk_{uid}")
    log_act(uid, "unblocked", "admin")

def _get_blocked_db():
    with db() as c:
        return [r['user_id'] for r in c.execute("SELECT user_id FROM blocked_users").fetchall()]

def get_blocked():
    return cached("blks", _get_blocked_db)

def _get_auto_rules_db():
    with db() as c:
        return {r['trigger_word'].lower(): r['reply_text'] 
        for r in c.execute("SELECT trigger_word, reply_text FROM auto_reply").fetchall()}

def get_auto_rules():
    return cached("arules", _get_auto_rules_db)

def add_auto_rule(t, r):
    with db() as c: c.execute("INSERT OR REPLACE INTO auto_reply VALUES (?,?)", (t.lower(), r))
    invalidate("arules")

def del_auto_rule(t):
    with db() as c: c.execute("DELETE FROM auto_reply WHERE trigger_word=?", (t.lower(),))
    invalidate("arules")

def log_act(uid, action, details=""):
    with db() as c: c.execute(
        "INSERT INTO user_activity (user_id,action,details,created_at) VALUES (?,?,?,?)",
        (uid, action, details, datetime.now().isoformat()))

def add_sub(uid, uname, fname, link, tw, mtype, mfid):
    with db() as c:
        sid = c.execute('''INSERT INTO submissions 
            (user_id,username,first_name,link,twitter,media_type,media_file_id,status,created_at) 
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (uid, uname, fname, link, tw, mtype, mfid, "pending", datetime.now().isoformat())).lastrowid
    log_act(uid, "submission", f"#{sid} {mtype}")
    return sid

def get_sub(sid):
    with db() as c:
        r = c.execute('''SELECT user_id,username,first_name,link,twitter,media_type,
            media_file_id,status,admin_msg_chat_id,admin_msg_id,created_at,user_reply 
            FROM submissions WHERE id=?''', (sid,)).fetchone()
        return dict(r) if r else None

def set_sub_admin_msg(sid, cid, mid):
    with db() as c: c.execute("UPDATE submissions SET admin_msg_chat_id=?,admin_msg_id=? WHERE id=?", (cid, mid, sid))

def set_sub_status(sid, status):
    with db() as c: c.execute("UPDATE submissions SET status=? WHERE id=?", (status, sid))
    s = get_sub(sid)
    if s: log_act(s["user_id"], f"admin_{status}", f"#{sid}")

def set_user_reply(sid, value):
    with db() as c: c.execute("UPDATE submissions SET user_reply=? WHERE id=?", (value, sid))
    s = get_sub(sid)
    if s: log_act(s["user_id"], f"user_reply_{'on' if value else 'off'}", f"#{sid}")

def get_user_subs(uid, limit=5):
    with db() as c:
        return c.execute("SELECT id,media_type,status,created_at FROM submissions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (uid, limit)).fetchall()

def get_all_users():
    with db() as c:
        return [r['user_id'] for r in c.execute(
            "SELECT DISTINCT user_id FROM submissions UNION SELECT user_id FROM user_activity").fetchall()]

def get_stats():
    with db() as c:
        r = c.execute('''SELECT COUNT(DISTINCT user_id) as users, COUNT(*) as total,
            SUM(status='accepted') as accepted, SUM(status='rejected') as rejected,
            SUM(status='pending') as pending, (SELECT COUNT(*) FROM blocked_users) as blocked
            FROM submissions''').fetchone()
        return dict(r)

def get_activity(limit=15):
    with db() as c:
        return c.execute("SELECT user_id,action,details,created_at FROM user_activity ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

def get_top_users(limit=10):
    with db() as c:
        return c.execute("SELECT user_id,username,COUNT(*) as cnt FROM submissions GROUP BY user_id ORDER BY cnt DESC LIMIT ?", (limit,)).fetchall()

# ================= ACTIVE REPLY =================
def set_active_reply(sid):
    global _active_reply
    with db() as c:
        c.execute("DELETE FROM active_reply")
        c.execute("INSERT INTO active_reply VALUES (?)", (sid,))
    _active_reply = sid

def get_active_reply():
    global _active_reply
    if _active_reply is not None: return _active_reply
    with db() as c:
        r = c.execute("SELECT submission_id FROM active_reply").fetchone()
        _active_reply = r['submission_id'] if r else None
    return _active_reply

def clear_active_reply():
    global _active_reply
    with db() as c: c.execute("DELETE FROM active_reply")
    _active_reply = None

# ================= RATE LIMITING =================
def rate_limit(uid):
    now = time.time()
    ts = _rate_limits[uid]
    while ts and ts[0] < now - RATE_LIMIT_WINDOW:
        ts.pop(0)
    if len(ts) >= RATE_LIMIT_MAX:
        return False
    ts.append(now)
    return True

def cleanup_rates():
    now = time.time()
    for uid in list(_rate_limits):
        while _rate_limits[uid] and _rate_limits[uid][0] < now - RATE_LIMIT_WINDOW:
            _rate_limits[uid].pop(0)
        if not _rate_limits[uid]:
            del _rate_limits[uid]

async def check_rate(update, ctx):
    if not update.effective_user: return True
    uid = update.effective_user.id
    if uid == ADMIN_ID: return True
    if is_blocked(uid):
        await update.message.reply_text("🚫 You are blocked.")
        return False
    if not rate_limit(uid):
        await update.message.reply_text(f"⚠️ Slow down! {RATE_LIMIT_MAX} msgs/{RATE_LIMIT_WINDOW}s")
        return False
    return True

# ================= KEYBOARD BUILDERS =================
def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Stats", callback_data="a_stats"),
         InlineKeyboardButton("📋 Activity", callback_data="a_activity")],
        [InlineKeyboardButton("🏆 Top Users", callback_data="a_top"),
         InlineKeyboardButton("📢 Broadcast", callback_data="a_bcast")],
        [InlineKeyboardButton("🚫 Blacklist", callback_data="a_blk"),
         InlineKeyboardButton("🤖 Auto-Reply", callback_data="a_ar")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="a_set"),
         InlineKeyboardButton("📝 Messages", callback_data="a_msg")],
        [InlineKeyboardButton("🔄 Ping", callback_data="a_ping"),
         InlineKeyboardButton("💣 Reset Bot", callback_data="a_reset")],
        [InlineKeyboardButton("🔧 Maintenance", callback_data="a_maint")]
    ])

def kb_back(cb="a_back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=cb)]])

def kb_review(sid, admin_reply_active=False, user_reply_active=0, is_user_blocked=False):
    block_text = "✅ Unblock" if is_user_blocked else "🚫 Block"
    reply_text = "🛑 Stop" if admin_reply_active else "💬 Reply"
    user_reply_text = "🔊 User Reply: ON" if user_reply_active else "🔇 User Reply: OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept", callback_data=f"ar_acc_{sid}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"ar_rej_{sid}")],
        [InlineKeyboardButton(block_text, callback_data=f"ar_blk_{sid}"),
         InlineKeyboardButton(reply_text, callback_data=f"ar_rep_{sid}")],
        [InlineKeyboardButton(user_reply_text, callback_data=f"ur_tog_{sid}")]
    ])

# ================= FORMATTERS =================
def fmt_time(ts):
    try: return datetime.fromisoformat(ts).strftime("%d/%m %H:%M")
    except: return "?"

def fmt_full(ts):
    try: return datetime.fromisoformat(ts).strftime("%d-%m-%Y %H:%M:%S")
    except: return ts

def fmt_stats():
    s = get_stats()
    t = s.get('total', 0) or 0
    a = s.get('accepted', 0) or 0
    return (f"📊 *Stats*\n👥 Users: {s.get('users',0) or 0}\n📨 Total: {t}\n"
            f"✅ {a} ❌ {s.get('rejected',0) or 0} ⏳ {s.get('pending',0) or 0}\n"
            f"🚫 Blocked: {s.get('blocked',0) or 0}\n📈 Rate: {(a/t*100) if t else 0:.1f}%")

def fmt_activity():
    rows = get_activity()
    if not rows: return "No activity."
    return "*Recent Activity*\n" + "\n".join(
        f"{fmt_time(r['created_at'])} `{r['user_id']}`: {r['action']} {r['details']}" for r in rows)[:4000]

def fmt_top():
    rows = get_top_users()
    if not rows: return "No submissions."
    return "🏆 *Top Users*\n" + "\n".join(
        f"{i}. @{r['username'] or r['user_id']} – {r['cnt']}" for i, r in enumerate(rows, 1))

# ================= USER COMMANDS =================
async def cmd_status(update, ctx):
    u = update.effective_user
    if is_blocked(u.id): return await update.message.reply_text("🚫 Blocked.")
    rows = get_user_subs(u.id)
    if not rows: return await update.message.reply_text("No submissions. Send 'hello' to start.")
    em = {"pending": "⏳", "accepted": "✅", "rejected": "❌"}
    msg = "📋 *Your Submissions:*\n\n" + "\n".join(
        f"#{r['id']} {r['media_type']} {em.get(r['status'],'❓')} {r['status']} ({fmt_time(r['created_at'])})"
        for r in rows)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_help(update, ctx):
    admin = update.effective_user.id == ADMIN_ID
    await update.message.reply_text(
        "🤖 *Admin*\n/stats /activity /topusers /broadcast /blacklist /unblock\n/autoreply add|remove|list /ping /admin" if admin
        else "🤖 *Help*\nSend 'hello' to start\n/status - check subs\n/cancel - cancel",
        parse_mode="Markdown")

# ================= ADMIN COMMANDS =================
async def cmd_stats(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(fmt_stats(), parse_mode="Markdown")

async def cmd_activity(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(fmt_activity(), parse_mode="Markdown")

async def cmd_top(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(fmt_top(), parse_mode="Markdown")

async def cmd_broadcast(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.args: return await update.message.reply_text("Usage: /broadcast <msg>")
    users = get_all_users()
    if not users: return await update.message.reply_text("No users.")
    msg = " ".join(ctx.args)
    status = await update.message.reply_text(f"📢 Sending to {len(users)}...")
    sem = asyncio.Semaphore(10)
    async def send(uid):
        async with sem:
            try:
                await asyncio.sleep(0.05)
                await ctx.bot.send_message(uid, f"📢 *Announcement*\n\n{msg}", parse_mode="Markdown")
                return 1
            except: return 0
    results = await asyncio.gather(*[send(u) for u in users])
    await status.edit_text(f"✅ {sum(results)} sent | ❌ {len(results)-sum(results)} failed")

async def cmd_blacklist(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    bl = get_blocked()
    await update.message.reply_text("🚫 *Blocked*\n" + ("\n".join(f"• `{u}`" for u in bl) if bl else "None"),
                                    parse_mode="Markdown")

async def cmd_unblock(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    try: uid = int(ctx.args[0])
    except: return await update.message.reply_text("Usage: /unblock <id>")
    unblock_user(uid)
    await update.message.reply_text(f"✅ {uid} unblocked")

async def cmd_autoreply(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    if len(ctx.args) < 2: return await update.message.reply_text("/autoreply add|remove|list")
    a = ctx.args[0].lower()
    if a == "add" and len(ctx.args) >= 3:
        add_auto_rule(ctx.args[1], " ".join(ctx.args[2:]))
        await update.message.reply_text(f"✅ Added '{ctx.args[1]}'")
    elif a == "remove":
        del_auto_rule(ctx.args[1])
        await update.message.reply_text(f"✅ Removed '{ctx.args[1]}'")
    elif a == "list":
        rules = get_auto_rules()
        await update.message.reply_text("📝 *Rules*\n" + ("\n".join(f"• `{k}` → {v[:40]}..." for k,v in rules.items()) if rules else "None"),
                                        parse_mode="Markdown")

async def cmd_ping(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    t = time.time()
    await update.message.reply_text("Pong!")
    await update.message.reply_text(f"⚡ {(time.time()-t)*1000:.0f}ms\n🕒 {timedelta(seconds=int(time.time()-START_TIME))}")

# ================= BROADCAST HELPER =================
async def broadcast_to_all(ctx, text):
    users = get_all_users()
    if not users: return
    for uid in users:
        try:
            await ctx.bot.send_message(uid, text)
        except Exception:
            pass

# ================= SEND TO ADMIN =================
async def send_to_admin(ctx, sid, data):
    s = get_sub(sid)
    cap = (f"📥 *#{sid}*\n🕒 {fmt_full(s.get('created_at',''))}\n"
           f"👤 {data['first_name']} (@{data['username'] or 'N/A'})\n"
           f"🔗 {data.get('link') or 'None'}\n🐦 {data.get('twitter') or 'Skip'}\n📎 {data['media_type']}")
    fn = ctx.bot.send_photo if data['media_type'] == "photo" else ctx.bot.send_video
    admin_reply_active = (get_active_reply() == sid)
    user_reply_active = s.get('user_reply', 0)
    blocked = is_blocked(data['user_id'])
    msg = await fn(ADMIN_ID, data['media_file_id'], caption=cap, parse_mode="Markdown",
                   reply_markup=kb_review(sid, admin_reply_active, user_reply_active, blocked))
    set_sub_admin_msg(sid, ADMIN_ID, msg.message_id)

async def notify(ctx, uid, msg):
    if is_blocked(uid): return
    try: await ctx.bot.send_message(uid, msg)
    except Exception as e: logger.error(f"Notify {uid}: {e}")

# ================= MAINTENANCE MODE (BLOCK EVERYTHING) =================
async def maintenance_check_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message: return
    uid = update.effective_user.id
    if uid == ADMIN_ID: return
    if get_setting('maintenance_mode') == 'on':
        await update.message.reply_text("🔧 Currently I am in maintenance mode. Please try later.")
        return True  # stop further handlers

async def maintenance_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.callback_query: return
    uid = update.effective_user.id
    if uid == ADMIN_ID: return
    if get_setting('maintenance_mode') == 'on':
        await update.callback_query.answer("🔧 Bot is under maintenance. Try later.", show_alert=True)
        return True  # stop further handlers

# ================= REPLY MODE INTERCEPTOR =================
async def forward_to_admin(update, ctx, sid):
    msg = update.message
    if not msg: return
    caption = f"📩 *Chat from #{sid}*\n👤 @{update.effective_user.username or update.effective_user.id}"
    try:
        if msg.text:
            await ctx.bot.send_message(ADMIN_ID, f"{caption}\n\n{msg.text}", parse_mode="Markdown")
        elif msg.photo:
            await ctx.bot.send_photo(ADMIN_ID, msg.photo[-1].file_id, caption=caption)
        elif msg.video:
            await ctx.bot.send_video(ADMIN_ID, msg.video.file_id, caption=caption)
        else:
            await ctx.bot.send_message(ADMIN_ID, f"{caption}\n\n(unsupported media)", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"forward_to_admin error: {e}")

async def intercept_reply_mode(update, ctx):
    if not update.effective_user or not update.message: return
    uid = update.effective_user.id
    if uid == ADMIN_ID: return
    if is_blocked(uid): return
    with db() as c:
        row = c.execute("SELECT id FROM submissions WHERE user_id=? AND user_reply=1 LIMIT 1", (uid,)).fetchone()
    if row:
        await forward_to_admin(update, ctx, row['id'])
        log_act(uid, "chat_forwarded", f"user_reply #{row['id']}")
        raise Application.stop

# ================= HELLO RESET =================
async def hello_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message: return
    uid = update.effective_user.id
    if uid == ADMIN_ID: return
    if get_setting('maintenance_mode') == 'off':
        txt = update.message.text.lower()
        if txt in ('hello', 'hi'):
            context.user_data.clear()

# ================= USER CONVERSATION =================
async def conv_start(update, ctx):
    ctx.user_data.clear()
    u = update.effective_user
    if is_blocked(u.id): return ConversationHandler.END
    if not await check_rate(update, ctx): return ConversationHandler.END
    log_act(u.id, "start", "")
    await update.message.reply_text(
        get_setting('welcome_msg').format(username=u.username or u.first_name),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📸 Photo", callback_data="m_photo"),
                                            InlineKeyboardButton("🎥 Video", callback_data="m_video")]]))
    return SELECT_MEDIA

async def conv_media_sel(update, ctx):
    q = update.callback_query
    await q.answer()
    ctx.user_data["mtype"] = "photo" if "photo" in q.data else "video"
    await q.edit_message_text(f"Send {ctx.user_data['mtype']}:")
    return WAIT_MEDIA

async def conv_recv_media(update, ctx):
    u = update.effective_user
    if is_blocked(u.id) or not await check_rate(update, ctx): return ConversationHandler.END
    mt = ctx.user_data.get("mtype")
    if mt == "photo" and update.message.photo:
        fid = update.message.photo[-1].file_id
    elif mt == "video" and update.message.video:
        fid = update.message.video.file_id
    else:
        await update.message.reply_text(f"Send valid {mt}")
        return WAIT_MEDIA
    ctx.user_data["mfid"] = fid
    log_act(u.id, "media", mt)
    if get_setting('require_link') == 'yes':
        await update.message.reply_text("Send link:")
        return WAIT_LINK
    await update.message.reply_text("Special link?", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="l_y"), InlineKeyboardButton("No", callback_data="l_n")]]))
    return ASK_LINK

async def conv_ask_link(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "l_y":
        await q.edit_message_text("Send link:")
        return WAIT_LINK
    ctx.user_data["link"] = None
    return await _ask_twitter(update, ctx)

async def conv_recv_link(update, ctx):
    if not await check_rate(update, ctx): return ConversationHandler.END
    ctx.user_data["link"] = update.message.text
    log_act(update.effective_user.id, "link", "")
    return await _ask_twitter(update, ctx)

async def _ask_twitter(update, ctx):
    if get_setting('require_twitter') == 'yes':
        tgt = update.callback_query.message if update.callback_query else update.message
        if update.callback_query: await update.callback_query.answer()
        await tgt.reply_text("Twitter (without @):")
        return WAIT_TWITTER
    tgt = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    await tgt.reply_text("Twitter?", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="t_y"), InlineKeyboardButton("Skip", callback_data="t_n")]]))
    return ASK_TWITTER

async def conv_tw_choice(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "t_y":
        await q.edit_message_text("Username (without @):")
        return WAIT_TWITTER
    ctx.user_data["tw"] = None
    return await _finalize(update, ctx)

async def conv_recv_tw(update, ctx):
    if not await check_rate(update, ctx): return ConversationHandler.END
    ctx.user_data["tw"] = update.message.text
    log_act(update.effective_user.id, "twitter", "")
    return await _finalize(update, ctx)

async def _finalize(update, ctx):
    u = update.effective_user
    d = {"user_id": u.id, "username": u.username, "first_name": u.first_name,
         "link": ctx.user_data.get("link"), "twitter": ctx.user_data.get("tw"),
         "media_type": ctx.user_data["mtype"], "media_file_id": ctx.user_data["mfid"]}
    sid = add_sub(d["user_id"], d["username"], d["first_name"], d["link"], d["twitter"], d["media_type"], d["media_file_id"])
    await send_to_admin(ctx, sid, d)
    await update.effective_message.reply_text("✅ Submitted!\n/status to check")
    ctx.user_data.clear()
    return ConversationHandler.END

async def conv_cancel(update, ctx):
    await update.message.reply_text("❌ Cancelled")
    ctx.user_data.clear()
    return ConversationHandler.END

# ================= AUTO-REPLY =================
async def auto_reply(update, ctx):
    if not update.message or not update.message.text: return
    uid = update.effective_user.id
    if is_blocked(uid) or uid == ADMIN_ID: return
    if not await check_rate(update, ctx): return
    txt = update.message.text.lower()
    for trig, rep in get_auto_rules().items():
        if trig in txt:
            await update.message.reply_text(rep)
            log_act(uid, "autoreply", trig)
            break

# ================= ADMIN PANEL =================
async def admin_panel(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    if update.callback_query:
        await update.callback_query.edit_message_text("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=kb_admin())
    else:
        await update.message.reply_text("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=kb_admin())

async def admin_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id != ADMIN_ID: return
    d = q.data

    if d == "a_back": return await admin_panel(update, ctx)
    if d == "a_stats": await q.edit_message_text(fmt_stats(), parse_mode="Markdown", reply_markup=kb_back())
    elif d == "a_activity": await q.edit_message_text(fmt_activity(), parse_mode="Markdown", reply_markup=kb_back())
    elif d == "a_top": await q.edit_message_text(fmt_top(), parse_mode="Markdown", reply_markup=kb_back())
    elif d == "a_bcast": await q.edit_message_text("Use `/broadcast <msg>`", parse_mode="Markdown", reply_markup=kb_back())
    elif d == "a_blk":
        bl = get_blocked()
        await q.edit_message_text("🚫 *Blocked*\n" + ("\n".join(f"• `{u}`" for u in bl) if bl else "None"),
                                  parse_mode="Markdown", reply_markup=kb_back())
    elif d == "a_ar":
        rules = get_auto_rules()
        await q.edit_message_text("📝 *Auto-Reply*\n" + ("\n".join(f"• `{k}` → {v[:40]}..." for k,v in rules.items()) if rules else "None. /autoreply add"),
                                  parse_mode="Markdown", reply_markup=kb_back())
    elif d == "a_ping":
        t = time.time()
        await q.edit_message_text("Pong!")
        await q.edit_message_text(f"⚡ {(time.time()-t)*1000:.0f}ms\n🕒 {timedelta(seconds=int(time.time()-START_TIME))}", reply_markup=kb_back())
    elif d == "a_reset":
        await q.edit_message_text("⚠️ *Are you sure?* This will delete ALL data.", parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup([
                                      [InlineKeyboardButton("💣 Yes, reset everything", callback_data="a_doreset"),
                                       InlineKeyboardButton("🔙 No, go back", callback_data="a_back")]
                                  ]))
    elif d == "a_doreset":
        reset_db()
        await q.edit_message_text("✅ Database has been reset.", reply_markup=kb_admin())
    elif d == "a_set":
        tw, ln = get_setting('require_twitter'), get_setting('require_link')
        await q.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Twitter: {tw}", callback_data="s_tw"),
             InlineKeyboardButton(f"Link: {ln}", callback_data="s_ln")],
            [InlineKeyboardButton("🔙 Back", callback_data="a_back")]
        ]))
    elif d == "s_tw":
        n = 'yes' if get_setting('require_twitter') == 'no' else 'no'
        set_setting('require_twitter', n)
        tw, ln = get_setting('require_twitter'), get_setting('require_link')
        await q.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Twitter: {tw}", callback_data="s_tw"),
             InlineKeyboardButton(f"Link: {ln}", callback_data="s_ln")],
            [InlineKeyboardButton("🔙 Back", callback_data="a_back")]
        ]))
    elif d == "s_ln":
        n = 'yes' if get_setting('require_link') == 'no' else 'no'
        set_setting('require_link', n)
        tw, ln = get_setting('require_twitter'), get_setting('require_link')
        await q.edit_message_text("⚙️ *Settings*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Twitter: {tw}", callback_data="s_tw"),
             InlineKeyboardButton(f"Link: {ln}", callback_data="s_ln")],
            [InlineKeyboardButton("🔙 Back", callback_data="a_back")]
        ]))
    elif d == "a_maint":
        current = get_setting('maintenance_mode')
        status = "ON 🔧" if current == 'on' else "OFF ✅"
        await q.edit_message_text(
            f"🔧 *Maintenance Mode:* {status}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Toggle ON" if current == 'off' else "Toggle OFF",
                                     callback_data="maint_tog")],
                [InlineKeyboardButton("🔙 Back", callback_data="a_back")]
            ])
        )
    elif d == "maint_tog":
        current = get_setting('maintenance_mode')
        new = 'on' if current == 'off' else 'off'
        set_setting('maintenance_mode', new)
        if new == 'on':
            invalidate()
            cleanup_rates()
        else:
            await broadcast_to_all(ctx, "🎉 I am on now! Maintenance Done. Send your desires 😉")
        status = "ON 🔧" if new == 'on' else "OFF ✅"
        await q.edit_message_text(
            f"🔧 *Maintenance Mode:* {status}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Toggle ON" if new == 'off' else "Toggle OFF",
                                     callback_data="maint_tog")],
                [InlineKeyboardButton("🔙 Back", callback_data="a_back")]
            ])
        )
    elif d == "a_msg":
        await q.edit_message_text("📝 *Edit Messages*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Welcome", callback_data="e_welcome"),
             InlineKeyboardButton("✏️ Accept", callback_data="e_accept")],
            [InlineKeyboardButton("✏️ Reject", callback_data="e_reject")],
            [InlineKeyboardButton("🔙 Back", callback_data="a_back")]
        ]))
    elif d.startswith("e_"):
        ctx.user_data['editing'] = d[2:]
        await q.edit_message_text(f"Send new *{d[2:]}* msg. `{{username}}` placeholder.\n/cancel abort",
                                  parse_mode="Markdown", reply_markup=kb_back("a_msg"))

async def handle_edit(update, ctx):
    if update.effective_user.id != ADMIN_ID or 'editing' not in ctx.user_data: return
    t = ctx.user_data.pop('editing')
    set_setting(f'{t}_msg', update.message.text)
    await update.message.reply_text(f"✅ {t} updated!", reply_markup=kb_back("a_msg"))

# ================= REVIEW CALLBACKS =================
async def review_cb(update, ctx):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id != ADMIN_ID: return
    parts = q.data.split("_")
    act, sid = parts[1], int(parts[2])
    s = get_sub(sid)
    if not s: return await q.edit_message_text("Not found")
    cap = q.message.caption or ""
    admin_reply_active = (get_active_reply() == sid)
    user_reply_active = s.get('user_reply', 0)
    uid = s['user_id']

    if act == "acc":
        set_sub_status(sid, "accepted")
        await notify(ctx, uid, get_setting('accept_msg'))
        await q.edit_message_caption(
            caption=cap + "\n\n✅ Accepted", parse_mode="Markdown",
            reply_markup=kb_review(sid, admin_reply_active, user_reply_active, is_blocked(uid))
        )
    elif act == "rej":
        set_sub_status(sid, "rejected")
        await notify(ctx, uid, get_setting('reject_msg'))
        await q.edit_message_caption(
            caption=cap + "\n\n❌ Rejected", parse_mode="Markdown",
            reply_markup=kb_review(sid, admin_reply_active, user_reply_active, is_blocked(uid))
        )
    elif act == "blk":
        if is_blocked(uid):
            unblock_user(uid)
        else:
            block_user(uid)
        await q.edit_message_reply_markup(
            reply_markup=kb_review(sid, admin_reply_active, user_reply_active, is_blocked(uid))
        )
    elif act == "rep":
        cur = get_active_reply()
        if cur == sid:
            clear_active_reply()
            admin_reply_active = False
            await ctx.bot.send_message(ADMIN_ID, f"🛑 Reply stopped #{sid}")
        else:
            set_active_reply(sid)
            admin_reply_active = True
            await ctx.bot.send_message(ADMIN_ID, f"💬 Reply mode #{sid} (@{s['username'] or 'N/A'})")
        await q.edit_message_reply_markup(
            reply_markup=kb_review(sid, admin_reply_active, user_reply_active, is_blocked(uid))
        )

async def user_reply_toggle(update, ctx):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id != ADMIN_ID: return
    sid = int(q.data.split("_")[2])
    s = get_sub(sid)
    if not s: return await q.edit_message_text("Not found")
    new_val = 0 if s.get('user_reply', 0) else 1
    set_user_reply(sid, new_val)
    if new_val:
        await notify(ctx, s["user_id"], "🔊 Reply mode is ON. You can now send messages to admin.")
    else:
        await notify(ctx, s["user_id"], "🔇 Reply mode is OFF. Your messages will no longer be forwarded.")
    admin_reply_active = (get_active_reply() == sid)
    await q.edit_message_reply_markup(
        reply_markup=kb_review(sid, admin_reply_active, new_val, is_blocked(s['user_id']))
    )

# ================= ADMIN REPLY =================
async def admin_reply(update, ctx):
    if update.effective_user.id != ADMIN_ID: return
    active = get_active_reply()
    if not active: return
    s = get_sub(active)
    if not s:
        clear_active_reply()
        return await update.message.reply_text("❌ Sub not found")
    await notify(ctx, s["user_id"], f"📩 *Admin:*\n{update.message.text}")
    await update.message.reply_text(f"✅ Sent to @{s['username'] or s['first_name']}")

# ================= BACKGROUND CLEANUP =================
async def background_cleanup(app):
    while True:
        await asyncio.sleep(300)
        try:
            cleanup_rates()
            invalidate()
        except Exception as e:
            logger.error(f"Cleanup err: {e}")

# ================= MAIN =================
async def post_init(application):
    application.create_task(background_cleanup(application))

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    # --- Handler order (lowest group = highest priority) ---
    # Maintenance blockers must use block=True to stop all further handlers
    app.add_handler(MessageHandler(filters.ALL, maintenance_check_message, block=True), group=-100)
    app.add_handler(CallbackQueryHandler(maintenance_check_callback, block=True), group=-100)
    app.add_handler(MessageHandler(filters.ALL, intercept_reply_mode, block=False), group=-99)  # uses raise Application.stop
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, hello_reset, block=False), group=-98)
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", conv_start), MessageHandler(filters.Regex(r'(?i)^(hello|hi)$'), conv_start)],
        states={
            SELECT_MEDIA: [CallbackQueryHandler(conv_media_sel, pattern="^m_")],
            WAIT_MEDIA: [MessageHandler(filters.PHOTO | filters.VIDEO, conv_recv_media)],
            ASK_LINK: [CallbackQueryHandler(conv_ask_link, pattern="^l_")],
            WAIT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_recv_link)],
            ASK_TWITTER: [CallbackQueryHandler(conv_tw_choice, pattern="^t_")],
            WAIT_TWITTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, conv_recv_tw)],
        },
        fallbacks=[CommandHandler("cancel", conv_cancel)],
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(review_cb, pattern="^ar_"))
    app.add_handler(CallbackQueryHandler(user_reply_toggle, pattern="^ur_tog_"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern="^(a_|e_|s_|a_doreset|maint_tog)"))
    app.add_handler(CommandHandler("admin", admin_panel))
    for cmd, fn in [("stats", cmd_stats), ("activity", cmd_activity), ("topusers", cmd_top),
                    ("broadcast", cmd_broadcast), ("blacklist", cmd_blacklist), ("unblock", cmd_unblock),
                    ("autoreply", cmd_autoreply), ("ping", cmd_ping)]:
        app.add_handler(CommandHandler(cmd, fn))
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), handle_edit), group=1)
    app.add_handler(MessageHandler(filters.TEXT & filters.User(ADMIN_ID), admin_reply), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_reply), group=3)
    
    logger.info("🚀 Bot started – Maintenance mode now fully blocks all interactions")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
