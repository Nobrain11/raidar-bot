import logging
import asyncio
import sqlite3
import os
import secrets
import threading
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode

from flask import Flask, request, redirect
import requests as http_requests

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
ADMIN_IDS = [7761011341]
BOT_TOKEN = os.getenv("BOT_TOKEN")
TWITTER_CLIENT_ID = os.getenv("TWITTER_CLIENT_ID")
TWITTER_CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET")
CALLBACK_URL = os.getenv("CALLBACK_URL", "http://localhost:8080/callback").strip()
FLASK_SECRET = os.getenv("FLASK_SECRET", secrets.token_hex(32))
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is required!")

DB_NAME = 'raidar.db'

# ====================== DATABASE ======================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        twitter_id TEXT,
        twitter_username TEXT,
        wallet_address TEXT,
        xp INTEGER DEFAULT 0,
        disqualified BOOLEAN DEFAULT 0,
        is_pro BOOLEAN DEFAULT 0,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS oauth_states (
        state TEXT PRIMARY KEY,
        telegram_id INTEGER,
        code_verifier TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS raid_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tweet_link TEXT NOT NULL,
        targets TEXT,
        position INTEGER,
        added_by INTEGER,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_raid (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tweet_link TEXT,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT 1
    )''')
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_NAME)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_user_by_identifier(conn, identifier):
    c = conn.cursor()
    if identifier and identifier.startswith('@'):
        identifier = identifier[1:]
    c.execute("SELECT * FROM users WHERE twitter_username = ?", (identifier,))
    row = c.fetchone()
    if row:
        return row
    try:
        tg_id = int(identifier)
        c.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
        return c.fetchone()
    except:
        return None

# ====================== OAUTH HELPERS ======================
import base64
import hashlib
import urllib.parse

def generate_pkce():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b'=').decode()
    return code_verifier, code_challenge

def build_twitter_oauth_url(state, code_challenge):
    params = {
        "response_type": "code",
        "client_id": TWITTER_CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "scope": "tweet.read tweet.write users.read offline.access",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
    }
    url = "https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(params)
    logger.info(f"OAuth URL: {url}")
    return url

def exchange_code_for_token(code, code_verifier):
    resp = http_requests.post(
        "https://api.twitter.com/2/oauth2/token",
        data={
            "code": code,
            "grant_type": "authorization_code",
            "client_id": TWITTER_CLIENT_ID,
            "redirect_uri": CALLBACK_URL,
            "code_verifier": code_verifier,
        },
        auth=(TWITTER_CLIENT_ID, TWITTER_CLIENT_SECRET),
    )
    return resp.json()

def get_twitter_user(access_token):
    resp = http_requests.get(
        "https://api.twitter.com/2/users/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return resp.json()

# ====================== FLASK APP ======================
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

_bot_app = None
_bot_loop = None

@flask_app.route("/health")
def health():
    return "OK", 200

@flask_app.route("/connect/<int:telegram_id>")
def connect_twitter(telegram_id):
    if not TWITTER_CLIENT_ID:
        return "Twitter OAuth not configured.", 500

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce()

    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO oauth_states (state, telegram_id, code_verifier) VALUES (?, ?, ?)",
        (state, telegram_id, code_verifier)
    )
    conn.commit()
    conn.close()

    url = build_twitter_oauth_url(state, code_challenge)
    return redirect(url)

@flask_app.route("/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return f"""
        <html><body style="font-family:monospace;background:#0d0d0d;color:#ff4444;text-align:center;padding-top:100px;">
        <h2>❌ Authorization denied</h2><p>{error}</p>
        <p>Return to Telegram and try again.</p>
        </body></html>
        """, 400

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id, code_verifier FROM oauth_states WHERE state = ?", (state,))
    row = c.fetchone()

    if not row:
        conn.close()
        return "<html><body>❌ Invalid or expired state. Return to Telegram.</body></html>", 400

    telegram_id, code_verifier = row
    conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    conn.commit()

    token_data = exchange_code_for_token(code, code_verifier)
    access_token = token_data.get("access_token")

    if not access_token:
        conn.close()
        return f"<html><body>❌ Token exchange failed: {token_data}</body></html>", 500

    user_data = get_twitter_user(access_token)
    twitter_id = user_data.get("data", {}).get("id")
    twitter_username = user_data.get("data", {}).get("username")

    if not twitter_id:
        conn.close()
        return "<html><body>❌ Could not fetch Twitter user info.</body></html>", 500

    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))
    conn.execute(
        "UPDATE users SET twitter_id = ?, twitter_username = ? WHERE telegram_id = ?",
        (twitter_id, twitter_username, telegram_id)
    )
    conn.commit()
    conn.close()

    if _bot_app and _bot_loop:
        async def notify():
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=f"✅ Twitter connected!\n\n🐦 @{twitter_username}\n\nYou're all set to raid!",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Failed to notify user {telegram_id}: {e}")
        asyncio.run_coroutine_threadsafe(notify(), _bot_loop)

    return f"""
    <html><body style="font-family:monospace;background:#0d0d0d;color:#00ff88;text-align:center;padding-top:80px;">
    <h1>✅ Connected!</h1>
    <p style="color:#aaa;font-size:18px;">Twitter account <strong>@{twitter_username}</strong> linked.</p>
    <p style="color:#555;margin-top:40px;">Return to Telegram — you're ready to raid.</p>
    </body></html>
    """

def run_flask():
    from waitress import serve
    logger.info(f"🌐 OAuth server running on port {PORT}")
    serve(flask_app, host="0.0.0.0", port=PORT)

# ====================== BOT COMMANDS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        "🚀 Welcome to **Raidar**!\nUse /help for all commands.",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""
📓 **Raidar Commands**

**🛠 General**
`/raid <link>` - Start raid
`/stop` - Stop raid
`/settings` - Settings
`/reward <user> <amount> <symbol>`
`/gxp` - Group XP
`/trend` - Boost group
`/pro` - Upgrade to Pro

**⏳ Queue**
`/next <link> [targets]`
`/delnext <index>`
`/setnext <link> [targets]`
`/switchnext <i1> <i2>`
`/clearnext`

**🏆 XP & LB**
`/lb [1D|7D|30D|ALL]`
`/xp <user>`
`/givexp <user> <amount>`
`/remxp <user> <amount>`
`/disqualify <user>`
`/undisqualify <user>`
`/disqualified`
`/lbreset`

**🌎 Everyone**
`/profile` `/connect <wallet>` `/login` `/logout`
""", parse_mode=ParseMode.MARKDOWN)

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user.id,))
    conn.commit()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = ?", (user.id,))
    row = c.fetchone()
    conn.close()

    tg_id, tw_id, tw_user, wallet, xp, disq, is_pro, joined = row

    twitter_line = f"🐦 @{tw_user}" if tw_user else "❗️ No Twitter connected"
    wallet_line = f"💼 `{wallet[:6]}...{wallet[-4:]}`" if wallet else "❗️ No Solana wallet set"
    status = "⛔ Disqualified" if disq else ("⭐ Pro" if is_pro else "✅ Active")

    text = (
        f"👤 **Your Profile**\n\n"
        f"Welcome back, {user.first_name}!\n\n"
        f"{'—' * 20}\n\n"
        f"{twitter_line}\n"
        f"{wallet_line}\n\n"
        f"💎 XP: `{xp}`\n"
        f"Status: {status}"
    )

    base_url = CALLBACK_URL.rsplit('/callback', 1)[0]
    buttons = []
    if not tw_user:
        buttons.append([InlineKeyboardButton("🐦 Connect Twitter", url=f"{base_url}/connect/{tg_id}")])
    else:
        buttons.append([InlineKeyboardButton("🔄 Reconnect Twitter", url=f"{base_url}/connect/{tg_id}")])

    if not wallet:
        buttons.append([InlineKeyboardButton("💼 Connect Wallet", callback_data="wallet_connect")])
    else:
        buttons.append([InlineKeyboardButton("🔄 Change Wallet", callback_data="wallet_connect")])

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN
    )

async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/connect <wallet_address>`", parse_mode=ParseMode.MARKDOWN)
        return
    wallet = context.args[0]
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (update.effective_user.id,))
    conn.execute("UPDATE users SET wallet_address = ? WHERE telegram_id = ?", (wallet, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ Wallet connected: `{wallet[:6]}...{wallet[-4:]}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use /profile and tap **Connect Twitter** to link your account via OAuth.",
        parse_mode=ParseMode.MARKDOWN
    )

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    conn.execute(
        "UPDATE users SET twitter_id = NULL, twitter_username = NULL WHERE telegram_id = ?",
        (update.effective_user.id,)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("👋 Twitter account unlinked.")

async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/raid <tweet_link>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    conn = get_db()
    conn.execute("UPDATE active_raid SET is_active = 0")
    conn.execute("INSERT INTO active_raid (tweet_link, is_active) VALUES (?, 1)", (link,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🔥 **RAID STARTED!**\n\n🐦 {link}\n\nLike, RT & comment! Earn XP for participating.",
        parse_mode=ParseMode.MARKDOWN
    )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    conn = get_db()
    conn.execute("UPDATE active_raid SET is_active = 0")
    conn.commit()
    conn.close()
    await update.message.reply_text("🛑 Raid stopped.")

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/next <link> [targets]`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    targets = " ".join(context.args[1:]) if len(context.args) > 1 else None
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM raid_queue")
    count = c.fetchone()[0]
    conn.execute(
        "INSERT INTO raid_queue (tweet_link, targets, position, added_by) VALUES (?, ?, ?, ?)",
        (link, targets, count + 1, update.effective_user.id)
    )
    conn.commit()
    c.execute("SELECT tweet_link, targets FROM raid_queue ORDER BY position")
    rows = c.fetchall()
    conn.close()
    queue_text = "\n".join([f"{i+1}. {r[0]}{' — ' + r[1] if r[1] else ''}" for i, r in enumerate(rows)])
    await update.message.reply_text(f"➕ Added!\n\n**Queue:**\n{queue_text}", parse_mode=ParseMode.MARKDOWN)

async def delnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/delnext <index>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Index must be a number.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM raid_queue ORDER BY position LIMIT 1 OFFSET ?", (idx - 1,))
    row = c.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("❌ No item at that index.")
        return
    conn.execute("DELETE FROM raid_queue WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑 Removed #{idx}.")

async def setnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setnext <link> [targets]`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    targets = " ".join(context.args[1:]) if len(context.args) > 1 else None
    conn = get_db()
    conn.execute("DELETE FROM raid_queue")
    conn.execute(
        "INSERT INTO raid_queue (tweet_link, targets, position, added_by) VALUES (?, ?, 1, ?)",
        (link, targets, update.effective_user.id)
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"📌 Queue reset. Next: {link}")

async def switchnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/switchnext <i1> <i2>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        i1, i2 = int(context.args[0]), int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Indices must be numbers.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, position FROM raid_queue ORDER BY position")
    rows = c.fetchall()
    if i1 < 1 or i2 < 1 or i1 > len(rows) or i2 > len(rows):
        conn.close()
        await update.message.reply_text("❌ Index out of range.")
        return
    id1, pos1 = rows[i1 - 1]
    id2, pos2 = rows[i2 - 1]
    conn.execute("UPDATE raid_queue SET position = ? WHERE id = ?", (pos2, id1))
    conn.execute("UPDATE raid_queue SET position = ? WHERE id = ?", (pos1, id2))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🔄 Swapped #{i1} and #{i2}.")

async def clearnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    conn = get_db()
    conn.execute("DELETE FROM raid_queue")
    conn.commit()
    conn.close()
    await update.message.reply_text("🧹 Queue cleared.")

async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = context.args[0].upper() if context.args else "ALL"
    labels = {"1D": "Last 24h", "7D": "Last 7 Days", "30D": "Last 30 Days"}
    label = labels.get(period, "All Time")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id, twitter_username, xp FROM users WHERE disqualified = 0 ORDER BY xp DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📊 No data yet.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 **Leaderboard — {label}**\n"]
    for i, (tg_id, tw, xp) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        name = f"@{tw}" if tw else f"#{tg_id}"
        lines.append(f"{prefix} {name} — `{xp} XP`")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def xp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    identifier = context.args[0] if context.args else str(update.effective_user.id)
    conn = get_db()
    row = get_user_by_identifier(conn, identifier)
    conn.close()
    if not row:
        await update.message.reply_text("❌ User not found.")
        return
    tg_id, tw_id, tw_user, wallet, xp, disq, is_pro, joined = row
    name = f"@{tw_user}" if tw_user else f"#{tg_id}"
    status = "⛔ Disqualified" if disq else ("⭐ Pro" if is_pro else "✅ Active")
    await update.message.reply_text(f"👤 **{name}**\n💎 XP: `{xp}`\n{status}", parse_mode=ParseMode.MARKDOWN)

async def givexp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/givexp <user> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a number.")
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("❌ User not found.")
        return
    conn.execute("UPDATE users SET xp = xp + ? WHERE telegram_id = ?", (amount, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Gave `{amount}` XP.", parse_mode=ParseMode.MARKDOWN)

async def remxp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/remxp <user> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Amount must be a number.")
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("❌ User not found.")
        return
    conn.execute("UPDATE users SET xp = MAX(0, xp - ?) WHERE telegram_id = ?", (amount, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Removed `{amount}` XP.", parse_mode=ParseMode.MARKDOWN)

async def disqualify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/disqualify <user>`", parse_mode=ParseMode.MARKDOWN)
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("❌ User not found.")
        return
    conn.execute("UPDATE users SET disqualified = 1 WHERE telegram_id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text("⛔ User disqualified.")

async def undisqualify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/undisqualify <user>`", parse_mode=ParseMode.MARKDOWN)
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("❌ User not found.")
        return
    conn.execute("UPDATE users SET disqualified = 0 WHERE telegram_id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ User re-qualified.")

async def disqualified_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id, twitter_username FROM users WHERE disqualified = 1")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("✅ No disqualified users.")
        return
    lines = ["⛔ **Disqualified Users:**\n"] + [f"• {'@' + tw if tw else '#' + str(tid)}" for tid, tw in rows]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def lbreset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    conn = get_db()
    conn.execute("UPDATE users SET xp = 0")
    conn.commit()
    conn.close()
    await update.message.reply_text("🔄 Leaderboard reset.")

async def reward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/reward <user> <amount> <symbol>`", parse_mode=ParseMode.MARKDOWN)
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    conn.close()
    if not row:
        await update.message.reply_text("❌ User not found.")
        return
    name = f"@{row[2]}" if row[2] else f"#{row[0]}"
    await update.message.reply_text(
        f"🎁 **Reward Sent!**\n👤 {name}\n💰 `{context.args[1]} {context.args[2]}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 View Stats", callback_data="settings_stats")],
        [InlineKeyboardButton("🧹 Clear Queue", callback_data="settings_clearqueue")],
        [InlineKeyboardButton("🔄 Reset LB", callback_data="settings_resetlb")],
    ]
    await update.message.reply_text("⚙️ **Settings**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def gxp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT SUM(xp), COUNT(*) FROM users WHERE disqualified = 0")
    total_xp, total_users = c.fetchone()
    conn.close()
    await update.message.reply_text(
        f"📊 **Group XP**\n👥 Raiders: `{total_users}`\n💎 Total XP: `{total_xp or 0}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def trend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 **Trend Boost**\n\nKeep raiding to climb the ranks! 🚀", parse_mode=ParseMode.MARKDOWN)

async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("⭐ Upgrade to Pro", callback_data="pro_upgrade")]]
    await update.message.reply_text(
        "⭐ **Raidar Pro**\n\n• Priority queue\n• XP multipliers\n• Custom rewards\n\nContact admin to upgrade.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# ── WALLET CONNECT FLOW ───────────────────────────────
_awaiting_wallet = set()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "wallet_connect":
        _awaiting_wallet.add(query.from_user.id)
        await query.message.reply_text("💼 Send your Solana wallet address now:")

    elif data == "settings_stats":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM raid_queue")
        queued = c.fetchone()[0]
        c.execute("SELECT tweet_link FROM active_raid WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
        active = c.fetchone()
        conn.close()
        await query.edit_message_text(
            f"📊 **Stats**\n👥 Users: `{users}`\n⏳ Queued: `{queued}`\n🔥 Active: {active[0] if active else 'None'}",
            parse_mode=ParseMode.MARKDOWN
        )
    elif data == "settings_clearqueue":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Admins only.", show_alert=True)
            return
        conn = get_db()
        conn.execute("DELETE FROM raid_queue")
        conn.commit()
        conn.close()
        await query.edit_message_text("🧹 Queue cleared.")
    elif data == "settings_resetlb":
        if not is_admin(query.from_user.id):
            await query.answer("❌ Admins only.", show_alert=True)
            return
        conn = get_db()
        conn.execute("UPDATE users SET xp = 0")
        conn.commit()
        conn.close()
        await query.edit_message_text("🔄 Leaderboard reset.")
    elif data == "pro_upgrade":
        await query.answer("Contact an admin to upgrade to Pro!", show_alert=True)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in _awaiting_wallet:
        _awaiting_wallet.discard(user_id)
        wallet = update.message.text.strip()
        if len(wallet) < 32 or len(wallet) > 44 or ' ' in wallet:
            await update.message.reply_text(
                "❌ That doesn't look like a valid Solana address. Try again with `/connect <address>`.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
        conn.execute("UPDATE users SET wallet_address = ? WHERE telegram_id = ?", (wallet, user_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(
            f"✅ Wallet saved!\n`{wallet[:6]}...{wallet[-4:]}`\n\nUse /profile to view your profile.",
            parse_mode=ParseMode.MARKDOWN
        )

# ====================== ERROR HANDLER ======================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception while handling an update: {context.error}")
    if update:
        logger.error(f"Update: {update}")

# ====================== MAIN ======================

def main():
    global _bot_app, _bot_loop

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    _bot_app = app

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("raid", raid_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("delnext", delnext_cmd))
    app.add_handler(CommandHandler("setnext", setnext_cmd))
    app.add_handler(CommandHandler("switchnext", switchnext_cmd))
    app.add_handler(CommandHandler("clearnext", clearnext_cmd))
    app.add_handler(CommandHandler("lb", lb_cmd))
    app.add_handler(CommandHandler("leaderboard", lb_cmd))
    app.add_handler(CommandHandler("xp", xp_cmd))
    app.add_handler(CommandHandler("givexp", givexp_cmd))
    app.add_handler(CommandHandler("remxp", remxp_cmd))
    app.add_handler(CommandHandler("disqualify", disqualify_cmd))
    app.add_handler(CommandHandler("undisqualify", undisqualify_cmd))
    app.add_handler(CommandHandler("disqualified", disqualified_cmd))
    app.add_handler(CommandHandler("lbreset", lbreset_cmd))
    app.add_handler(CommandHandler("reward", reward_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("connect", connect_cmd))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(CommandHandler("gxp", gxp_cmd))
    app.add_handler(CommandHandler("trend", trend_cmd))
    app.add_handler(CommandHandler("pro", pro_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Store event loop for OAuth callback notifications
    _bot_loop = asyncio.get_event_loop()

    logger.info("🚀 Starting Raidar Bot (polling)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
