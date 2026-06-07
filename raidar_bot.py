import logging
import asyncio
import sqlite3
import os
import secrets
import threading
import sys
import fcntl
import base64
import hashlib
import urllib.parse
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from flask import Flask, request, redirect
import requests as http_requests

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
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
    raise ValueError("BOT_TOKEN environment variable is required!")

DB_NAME = "raidar.db"

# ====================== DATABASE ======================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        twitter_id TEXT,
        twitter_username TEXT,
        wallet_address TEXT,
        xp INTEGER DEFAULT 0,
        disqualified BOOLEAN DEFAULT 0,
        is_pro BOOLEAN DEFAULT 0,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS oauth_states (
        state TEXT PRIMARY KEY,
        telegram_id INTEGER,
        code_verifier TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS raid_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tweet_link TEXT NOT NULL,
        target_likes INTEGER DEFAULT 10,
        target_retweets INTEGER DEFAULT 5,
        target_replies INTEGER DEFAULT 3,
        position INTEGER,
        added_by INTEGER,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS active_raid (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tweet_link TEXT,
        target_likes INTEGER DEFAULT 10,
        target_retweets INTEGER DEFAULT 5,
        target_replies INTEGER DEFAULT 3,
        live_message_id INTEGER,
        live_chat_id INTEGER,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        is_active BOOLEAN DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS raid_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raid_id INTEGER,
        telegram_id INTEGER,
        action_type TEXT,
        done_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect(DB_NAME)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_user_by_identifier(conn, identifier):
    c = conn.cursor()
    if identifier and identifier.startswith("@"):
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

# ====================== OAUTH ======================
def generate_pkce():
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
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
    return "https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(params)

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

# ====================== FLASK ======================
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
    conn.execute("INSERT OR REPLACE INTO oauth_states (state, telegram_id, code_verifier) VALUES (?, ?, ?)",
                 (state, telegram_id, code_verifier))
    conn.commit()
    conn.close()
    return redirect(build_twitter_oauth_url(state, code_challenge))

@flask_app.route("/callback")
def oauth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        return f"<html><body style=\"font-family:monospace;background:#0d0d0d;color:#ff4444;text-align:center;padding-top:100px;\"><h2>Authorization denied</h2><p>{error}</p></body></html>", 400
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id, code_verifier FROM oauth_states WHERE state = ?", (state,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "<html><body>Invalid or expired state. Return to Telegram.</body></html>", 400
    telegram_id, code_verifier = row
    conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
    conn.commit()
    token_data = exchange_code_for_token(code, code_verifier)
    access_token = token_data.get("access_token")
    if not access_token:
        conn.close()
        return f"<html><body>Token exchange failed: {token_data}</body></html>", 500
    user_data = get_twitter_user(access_token)
    twitter_id = user_data.get("data", {}).get("id")
    twitter_username = user_data.get("data", {}).get("username")
    if not twitter_id:
        conn.close()
        return "<html><body>Could not fetch Twitter user info.</body></html>", 500
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (telegram_id,))
    conn.execute("UPDATE users SET twitter_id = ?, twitter_username = ? WHERE telegram_id = ?",
                 (twitter_id, twitter_username, telegram_id))
    conn.commit()
    conn.close()
    if _bot_app and _bot_loop:
        async def notify():
            try:
                await _bot_app.bot.send_message(
                    chat_id=telegram_id,
                    text=f"Twitter connected! @{twitter_username} - You are all set to raid!",
                )
            except Exception as e:
                logger.error(f"Notify failed: {e}")
        asyncio.run_coroutine_threadsafe(notify(), _bot_loop)
    return f"<html><body style=\"font-family:monospace;background:#0d0d0d;color:#00ff88;text-align:center;padding-top:80px;\"><h1>Connected!</h1><p style=\"color:#aaa;font-size:18px;\">Twitter account @{twitter_username} linked.</p><p style=\"color:#555;margin-top:40px;\">Return to Telegram - you are ready to raid.</p></body></html>"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# ====================== RAID HELPERS ======================
def build_raid_options_text(tweet_link, tl, tr, trep):
    lines_out = []
    lines_out.append("\u2699\ufe0f *Raid Options*")
    lines_out.append("")
    lines_out.append("\U0001f517 Link: " + tweet_link)
    lines_out.append("\u2764\ufe0f Likes: " + str(tl))
    lines_out.append("\U0001f504 Retweets: " + str(tr))
    lines_out.append("\U0001f4ac Replies: " + str(trep))
    lines_out.append("\U0001f440 Views: 0")
    lines_out.append("\U0001f516 Bookmarks: 0")
    return "\n".join(lines_out)

def build_raid_options_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4a5 Start Raid \U0001f4a5", callback_data="raid_start")],
        [InlineKeyboardButton("\U0001f3af Targets", callback_data="raid_targets")],
        [InlineKeyboardButton("\U0001f512 Lock Chat \U0001f534", callback_data="raid_lock")],
        [InlineKeyboardButton("\U0001f4d3 Close", callback_data="raid_close")],
    ])

def build_targets_text(tl, tr, trep):
    lines_out = []
    lines_out.append("\u2699\ufe0f *Raid Options > Targets*")
    lines_out.append("")
    lines_out.append("You can specify the number of likes, retweets, replies, views and bookmarks that a tweet must have to be considered a valid target below.")
    return "\n".join(lines_out)

def build_targets_keyboard(tl, tr, trep):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"\u2764\ufe0f Likes {tl}", callback_data="tgt_likes")],
        [InlineKeyboardButton(f"\U0001f504 Retweets {tr}", callback_data="tgt_retweets")],
        [InlineKeyboardButton(f"\U0001f4ac Replies {trep}", callback_data="tgt_replies")],
        [InlineKeyboardButton("\U0001f440 Views 0", callback_data="tgt_views")],
        [InlineKeyboardButton("\U0001f516 Bookmarks 0", callback_data="tgt_bookmarks")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="tgt_back")],
    ])

def build_raid_live_text(tweet_link, tl, tr, trep, cur_likes=0, cur_rt=0, cur_rep=0):
    pct_l = int((cur_likes / tl) * 100) if tl > 0 else 0
    pct_r = int((cur_rt / tr) * 100) if tr > 0 else 0
    pct_p = int((cur_rep / trep) * 100) if trep > 0 else 0
    lines_out = []
    lines_out.append("\u26a1 *Raid Started!*")
    lines_out.append("")
    lines_out.append(f"\U0001f7e5 Likes  `{cur_likes} | {tl}` [{pct_l}%]")
    lines_out.append(f"\U0001f7e5 Retweets  `{cur_rt} | {tr}` [{pct_r}%]")
    lines_out.append(f"\U0001f7e5 Replies  `{cur_rep} | {trep}` [{pct_p}%]")
    lines_out.append("")
    lines_out.append(tweet_link)
    lines_out.append("")
    lines_out.append("\U0001f525 *Trending*")
    return "\n".join(lines_out)

def build_raid_live_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f4ac Comment", callback_data="raid_action_reply"),
            InlineKeyboardButton("\U0001f504 Retweet", callback_data="raid_action_rt"),
            InlineKeyboardButton("\u2764\ufe0f Like", callback_data="raid_action_like"),
        ],
        [
            InlineKeyboardButton("\U0001f4dd Quote", callback_data="raid_action_quote"),
            InlineKeyboardButton("\U0001f44a Boost", callback_data="raid_action_boost"),
        ],
    ])

# ====================== STATE ======================
_user_state = {}

# ====================== BOT COMMANDS ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("\U0001f680 Welcome to *Raidar*!\nUse /help for all commands.", parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Raidar Commands*\n\n"
        "/settings - Customize Raidar for your group\n"
        "/raid - Launch a raid instantly\n"
        "/stop - Stop an active raid\n"
        "/next - Queue up your next raid\n"
        "/gxp - Show trending XP\n"
        "/lb - Show current leaderboard\n"
        "/reward - Send token rewards\n"
        "/profile - Manage your profile\n"
        "/connect - Set Solana wallet\n"
        "/login - Link Twitter account\n"
        "/logout - Unlink Twitter\n"
        "/xp - Check XP\n"
        "/givexp - Give XP (admin)\n"
        "/remxp - Remove XP (admin)\n"
        "/disqualify - Disqualify user (admin)",
        parse_mode=ParseMode.MARKDOWN
    )

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
    twitter_line = f"\U0001f426 @{tw_user}" if tw_user else "\u2757 No Twitter connected"
    wallet_line = f"\U0001f4bc `{wallet[:6]}...{wallet[-4:]}`" if wallet else "\u2757 No Solana wallet set"
    status = "\u26d4 Disqualified" if disq else ("\u2b50 Pro" if is_pro else "\u2705 Active")
    text = (
        f"\U0001f464 *Your Profile*\n\n"
        f"Welcome back, {user.first_name}!\n\n"
        f"{twitter_line}\n"
        f"{wallet_line}\n\n"
        f"\U0001f48e XP: `{xp}`\n"
        f"Status: {status}"
    )
    base_url = CALLBACK_URL.rsplit("/callback", 1)[0]
    buttons = []
    if not tw_user:
        buttons.append([InlineKeyboardButton("\U0001f426 Connect Twitter", url=f"{base_url}/connect/{tg_id}")])
    else:
        buttons.append([InlineKeyboardButton("\U0001f504 Reconnect Twitter", url=f"{base_url}/connect/{tg_id}")])
    if not wallet:
        buttons.append([InlineKeyboardButton("\U0001f4bc Connect Wallet", callback_data="wallet_connect")])
    else:
        buttons.append([InlineKeyboardButton("\U0001f504 Change Wallet", callback_data="wallet_connect")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN)

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
    await update.message.reply_text(f"\u2705 Wallet connected: `{wallet[:6]}...{wallet[-4:]}`", parse_mode=ParseMode.MARKDOWN)

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    base_url = CALLBACK_URL.rsplit("/callback", 1)[0]
    buttons = [[InlineKeyboardButton("\U0001f426 Connect Twitter", url=f"{base_url}/connect/{user.id}")]]
    await update.message.reply_text("Tap below to connect your Twitter account via OAuth.", reply_markup=InlineKeyboardMarkup(buttons))

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    conn.execute("UPDATE users SET twitter_id = NULL, twitter_username = NULL WHERE telegram_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("\U0001f44b Twitter account unlinked.")

async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if context.args:
        link = context.args[0]
        await _show_raid_options(update, context, link)
    else:
        _user_state[update.effective_user.id] = {
            "step": "raid_link",
            "chat_id": update.effective_chat.id,
        }
        await update.message.reply_text("\U0001f517 Paste the tweet link to raid:")

async def _show_raid_options(update_or_msg, context, link, edit_msg=None):
    chat_id = update_or_msg.effective_chat.id if hasattr(update_or_msg, "effective_chat") else update_or_msg.chat.id
    pending = context.bot_data.get(f"pending_raid_{chat_id}", {})
    pending["link"] = link
    if "target_likes" not in pending:
        pending["target_likes"] = 10
    if "target_retweets" not in pending:
        pending["target_retweets"] = 5
    if "target_replies" not in pending:
        pending["target_replies"] = 3
    context.bot_data[f"pending_raid_{chat_id}"] = pending
    text = build_raid_options_text(link, pending["target_likes"], pending["target_retweets"], pending["target_replies"])
    keyboard = build_raid_options_keyboard()
    if edit_msg:
        await edit_msg.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    else:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT live_message_id, live_chat_id FROM active_raid WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
    active = c.fetchone()
    conn.execute("UPDATE active_raid SET is_active = 0")
    conn.commit()
    conn.close()
    if active and active[0] and active[1]:
        try:
            await context.bot.edit_message_text(
                chat_id=active[1], message_id=active[0],
                text="\U0001f6d1 *Raid Ended!*\n\nThanks for raiding!",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Could not edit raid message: {e}")
    await update.message.reply_text("\U0001f6d1 Raid stopped.")

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/next <link>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM raid_queue")
    count = c.fetchone()[0]
    conn.execute("INSERT INTO raid_queue (tweet_link, position, added_by) VALUES (?, ?, ?)", (link, count + 1, update.effective_user.id))
    conn.commit()
    c.execute("SELECT tweet_link FROM raid_queue ORDER BY position")
    rows = c.fetchall()
    conn.close()
    queue_text = "\n".join([f"{i+1}. {r[0]}" for i, r in enumerate(rows)])
    await update.message.reply_text(f"\u2795 Added!\n\n*Queue:*\n{queue_text}", parse_mode=ParseMode.MARKDOWN)

async def delnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/delnext <index>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        idx = int(context.args[0])
    except ValueError:
        await update.message.reply_text("\u274c Index must be a number.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM raid_queue ORDER BY position LIMIT 1 OFFSET ?", (idx - 1,))
    row = c.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("\u274c No item at that index.")
        return
    conn.execute("DELETE FROM raid_queue WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"\U0001f5d1 Removed #{idx}.")

async def clearnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    conn = get_db()
    conn.execute("DELETE FROM raid_queue")
    conn.commit()
    conn.close()
    await update.message.reply_text("\U0001f9f9 Queue cleared.")

async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id, twitter_username, xp FROM users WHERE disqualified = 0 ORDER BY xp DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("\U0001f4ca No data yet.")
        return
    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
    lb_lines = ["\U0001f3c6 *Leaderboard*\n"]
    for i, (tg_id, tw, xp) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        name = f"@{tw}" if tw else f"#{tg_id}"
        lb_lines.append(f"{prefix} {name} - `{xp} XP`")
    await update.message.reply_text("\n".join(lb_lines), parse_mode=ParseMode.MARKDOWN)

async def xp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    identifier = context.args[0] if context.args else str(update.effective_user.id)
    conn = get_db()
    row = get_user_by_identifier(conn, identifier)
    conn.close()
    if not row:
        await update.message.reply_text("\u274c User not found.")
        return
    tg_id, tw_id, tw_user, wallet, xp, disq, is_pro, joined = row
    name = f"@{tw_user}" if tw_user else f"#{tg_id}"
    status = "\u26d4 Disqualified" if disq else ("\u2b50 Pro" if is_pro else "\u2705 Active")
    await update.message.reply_text(f"\U0001f464 *{name}*\n\U0001f48e XP: `{xp}`\n{status}", parse_mode=ParseMode.MARKDOWN)

async def givexp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/givexp <user> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("\u274c Amount must be a number.")
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("\u274c User not found.")
        return
    conn.execute("UPDATE users SET xp = xp + ? WHERE telegram_id = ?", (amount, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"\u2705 Gave `{amount}` XP.", parse_mode=ParseMode.MARKDOWN)

async def remxp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/remxp <user> <amount>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("\u274c Amount must be a number.")
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("\u274c User not found.")
        return
    conn.execute("UPDATE users SET xp = MAX(0, xp - ?) WHERE telegram_id = ?", (amount, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"\u2705 Removed `{amount}` XP.", parse_mode=ParseMode.MARKDOWN)

async def disqualify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/disqualify <user>`", parse_mode=ParseMode.MARKDOWN)
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("\u274c User not found.")
        return
    conn.execute("UPDATE users SET disqualified = 1 WHERE telegram_id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text("\u26d4 User disqualified.")

async def undisqualify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/undisqualify <user>`", parse_mode=ParseMode.MARKDOWN)
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    if not row:
        conn.close()
        await update.message.reply_text("\u274c User not found.")
        return
    conn.execute("UPDATE users SET disqualified = 0 WHERE telegram_id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text("\u2705 User re-qualified.")

async def disqualified_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT telegram_id, twitter_username FROM users WHERE disqualified = 1")
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("\u2705 No disqualified users.")
        return
    dq_lines = ["\u26d4 *Disqualified Users:*\n"]
    for tid, tw in rows:
        dq_lines.append(f"- {'@' + tw if tw else '#' + str(tid)}")
    await update.message.reply_text("\n".join(dq_lines), parse_mode=ParseMode.MARKDOWN)

async def lbreset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    conn = get_db()
    conn.execute("UPDATE users SET xp = 0")
    conn.commit()
    conn.close()
    await update.message.reply_text("\U0001f504 Leaderboard reset.")

async def gxp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT SUM(xp), COUNT(*) FROM users WHERE disqualified = 0")
    total_xp, total_users = c.fetchone()
    conn.close()
    await update.message.reply_text(f"\U0001f4ca *Group XP*\n\U0001f465 Raiders: `{total_users}`\n\U0001f48e Total XP: `{total_xp or 0}`", parse_mode=ParseMode.MARKDOWN)

async def reward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/reward <user> <amount> <symbol>`", parse_mode=ParseMode.MARKDOWN)
        return
    conn = get_db()
    row = get_user_by_identifier(conn, context.args[0])
    conn.close()
    if not row:
        await update.message.reply_text("\u274c User not found.")
        return
    name = f"@{row[2]}" if row[2] else f"#{row[0]}"
    await update.message.reply_text(f"\U0001f381 *Reward Sent!*\n\U0001f464 {name}\n\U0001f4b0 `{context.args[1]} {context.args[2]}`", parse_mode=ParseMode.MARKDOWN)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("\u274c Admins only.")
        return
    keyboard = [
        [InlineKeyboardButton("\U0001f4ca View Stats", callback_data="settings_stats")],
        [InlineKeyboardButton("\U0001f9f9 Clear Queue", callback_data="settings_clearqueue")],
        [InlineKeyboardButton("\U0001f504 Reset LB", callback_data="settings_resetlb")],
    ]
    await update.message.reply_text("\u2699\ufe0f *Settings*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("\u2b50 Upgrade to Pro", callback_data="pro_upgrade")]]
    await update.message.reply_text("\u2b50 *Raidar Pro*\n\n- Priority queue\n- XP multipliers\n- Custom rewards\n- Multiple raids\n\nContact admin to upgrade.", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# ====================== BUTTON HANDLER ======================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    if data == "raid_start":
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT twitter_username FROM users WHERE telegram_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if not row or not row[0]:
            await query.answer("You have to connect your Twitter account first. DM Raidar with /login.", show_alert=True)
            return
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return

        pending = context.bot_data.get(f"pending_raid_{chat_id}", {})
        link = pending.get("link", "")
        if not link:
            await query.answer("No raid link set. Use /raid <link> first.", show_alert=True)
            return

        tl = pending.get("target_likes", 10)
        tr = pending.get("target_retweets", 5)
        trep = pending.get("target_replies", 3)

        conn = get_db()
        conn.execute("UPDATE active_raid SET is_active = 0")
        conn.execute("INSERT INTO active_raid (tweet_link, target_likes, target_retweets, target_replies, is_active) VALUES (?, ?, ?, ?, 1)", 
                     (link, tl, tr, trep))
        conn.commit()
        c = conn.cursor()
        c.execute("SELECT id FROM active_raid WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
        raid_id_row = c.fetchone()
        raid_db_id = raid_id_row[0] if raid_id_row else None
        conn.close()

        text = build_raid_live_text(link, tl, tr, trep)
        try:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=build_raid_live_keyboard(),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send live raid message: {e}")
            await query.answer("Failed to start raid. Check logs.", show_alert=True)
            return

        if raid_db_id:
            conn = get_db()
            conn.execute("UPDATE active_raid SET live_message_id = ?, live_chat_id = ? WHERE id = ?", 
                         (msg.message_id, chat_id, raid_db_id))
            conn.commit()
            conn.close()

        try:
            await query.edit_message_text("\u26a1 Raid started! Live progress below.")
        except Exception as e:
            logger.error(f"Could not edit options panel: {e}")
            try:
                await query.message.delete()
            except:
                pass

    elif data == "raid_targets":
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        pending = context.bot_data.get(f"pending_raid_{chat_id}", {})
        tl = pending.get("target_likes", 10)
        tr = pending.get("target_retweets", 5)
        trep = pending.get("target_replies", 3)
        await query.edit_message_text(
            build_targets_text(tl, tr, trep),
            reply_markup=build_targets_keyboard(tl, tr, trep),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data in ("tgt_likes", "tgt_retweets", "tgt_replies", "tgt_views", "tgt_bookmarks"):
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        label_map = {
            "tgt_likes": "likes",
            "tgt_retweets": "retweets",
            "tgt_replies": "replies",
            "tgt_views": "views",
            "tgt_bookmarks": "bookmarks",
        }
        field = data
        label = label_map[data]
        _user_state[user_id] = {
            "step": "tgt_input",
            "field": field,
            "chat_id": chat_id,
            "msg_id": query.message.message_id,
        }
        await query.message.reply_text(f"\U0001f522 How many *{label}* should be the target? Reply with a number:", parse_mode=ParseMode.MARKDOWN)

    elif data == "tgt_back":
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        pending = context.bot_data.get(f"pending_raid_{chat_id}", {})
        link = pending.get("link", "")
        tl = pending.get("target_likes", 10)
        tr = pending.get("target_retweets", 5)
        trep = pending.get("target_replies", 3)
        await query.edit_message_text(
            build_raid_options_text(link, tl, tr, trep),
            reply_markup=build_raid_options_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "raid_lock":
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.answer("Lock Chat requires bot admin permissions in group settings.", show_alert=True)

    elif data == "raid_close":
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        await query.edit_message_text("\U0001f4d3 Raid options closed.")

    elif data.startswith("raid_action_"):
        action = data.replace("raid_action_", "")
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT twitter_username FROM users WHERE telegram_id = ?", (user_id,))
        row = c.fetchone()
        if not row or not row[0]:
            conn.close()
            await query.answer("You have to connect your Twitter account first. DM Raidar with /login.", show_alert=True)
            return
        c.execute("SELECT id FROM active_raid WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
        raid_row = c.fetchone()
        if not raid_row:
            conn.close()
            await query.answer("No active raid.", show_alert=True)
            return
        raid_id = raid_row[0]
        c.execute("SELECT id FROM raid_actions WHERE raid_id = ? AND telegram_id = ? AND action_type = ?", (raid_id, user_id, action))
        already = c.fetchone()
        if already:
            conn.close()
            await query.answer("Already logged this action!", show_alert=False)
            return
        conn.execute("INSERT INTO raid_actions (raid_id, telegram_id, action_type) VALUES (?, ?, ?)", (raid_id, user_id, action))
        xp_map = {"like": 5, "rt": 10, "reply": 8, "quote": 12, "boost": 3}
        xp_earn = xp_map.get(action, 5)
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
        conn.execute("UPDATE users SET xp = xp + ? WHERE telegram_id = ?", (xp_earn, user_id))
        conn.commit()
        c.execute("SELECT COUNT(*) FROM raid_actions WHERE raid_id = ? AND action_type = ?", (raid_id, "like"))
        cur_likes = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM raid_actions WHERE raid_id = ? AND action_type = ?", (raid_id, "rt"))
        cur_rt = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM raid_actions WHERE raid_id = ? AND action_type = ?", (raid_id, "reply"))
        cur_rep = c.fetchone()[0]
        c.execute("SELECT tweet_link, target_likes, target_retweets, target_replies, live_message_id, live_chat_id FROM active_raid WHERE id = ?", (raid_id,))
        raid_data = c.fetchone()
        conn.close()
        if raid_data and raid_data[4] and raid_data[5]:
            new_text = build_raid_live_text(raid_data[0], raid_data[1], raid_data[2], raid_data[3], cur_likes, cur_rt, cur_rep)
            try:
                await context.bot.edit_message_text(
                    chat_id=raid_data[5], message_id=raid_data[4],
                    text=new_text, reply_markup=build_raid_live_keyboard(), parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Edit live message failed: {e}")
        await query.answer(f"+{xp_earn} XP earned! Keep raiding!", show_alert=False)

    elif data == "wallet_connect":
        _user_state[user_id] = {"step": "wallet", "chat_id": chat_id}
        await query.message.reply_text("\U0001f4bc Send your Solana wallet address now:")

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
        await query.edit_message_text(f"\U0001f4ca *Stats*\n\U0001f465 Users: `{users}`\n\u23f3 Queued: `{queued}`\n\U0001f525 Active: {active[0] if active else 'None'}", parse_mode=ParseMode.MARKDOWN)

    elif data == "settings_clearqueue":
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        conn = get_db()
        conn.execute("DELETE FROM raid_queue")
        conn.commit()
        conn.close()
        await query.edit_message_text("\U0001f9f9 Queue cleared.")

    elif data == "settings_resetlb":
        if not is_admin(user_id):
            await query.answer("Admins only.", show_alert=True)
            return
        conn = get_db()
        conn.execute("UPDATE users SET xp = 0")
        conn.commit()
        conn.close()
        await query.edit_message_text("\U0001f504 Leaderboard reset.")

    elif data == "pro_upgrade":
        await query.answer("Contact an admin to upgrade to Pro!", show_alert=True)

# ====================== MESSAGE HANDLER ======================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip() if update.message.text else ""
    state = _user_state.get(user_id)

    if not state:
        return

    step = state.get("step")

    if step == "raid_link":
        del _user_state[user_id]
        if not text.startswith("http"):
            await update.message.reply_text("\u274c That does not look like a valid link. Try /raid again.")
            return
        await _show_raid_options(update, context, text)

    elif step == "tgt_input":
        try:
            val = int(text)
            if val < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("\u274c Please send a valid positive number.")
            return
        del _user_state[user_id]
        field = state["field"]
        chat_id = state["chat_id"]
        msg_id = state["msg_id"]
        pending = context.bot_data.get(f"pending_raid_{chat_id}", {})
        field_map = {
            "tgt_likes": "target_likes",
            "tgt_retweets": "target_retweets",
            "tgt_replies": "target_replies",
            "tgt_views": "target_views",
            "tgt_bookmarks": "target_bookmarks",
        }
        pending[field_map[field]] = val
        context.bot_data[f"pending_raid_{chat_id}"] = pending
        tl = pending.get("target_likes", 10)
        tr = pending.get("target_retweets", 5)
        trep = pending.get("target_replies", 3)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=build_targets_text(tl, tr, trep),
                reply_markup=build_targets_keyboard(tl, tr, trep),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Edit targets msg failed: {e}")
        label_map = {"tgt_likes": "Likes", "tgt_retweets": "Retweets", "tgt_replies": "Replies", "tgt_views": "Views", "tgt_bookmarks": "Bookmarks"}
        await update.message.reply_text(f"\u2705 {label_map.get(field, field)} set to *{val}*", parse_mode=ParseMode.MARKDOWN)

    elif step == "wallet":
        del _user_state[user_id]
        wallet = text
        if len(wallet) < 32 or len(wallet) > 44 or " " in wallet:
            await update.message.reply_text("\u274c Not a valid Solana address. Try `/connect <address>`", parse_mode=ParseMode.MARKDOWN)
            return
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user_id,))
        conn.execute("UPDATE users SET wallet_address = ? WHERE telegram_id = ?", (wallet, user_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"\u2705 Wallet saved!\n`{wallet[:6]}...{wallet[-4:]}`", parse_mode=ParseMode.MARKDOWN)

# ====================== ERROR HANDLER ======================
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}")

# ====================== MAIN ======================
def main():
    global _bot_app, _bot_loop
    lock_file = open("/tmp/raidar_bot.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    _bot_app = app

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("raid", raid_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("delnext", delnext_cmd))
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
    app.add_handler(CommandHandler("pro", pro_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_error_handler(error_handler)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    _bot_loop = asyncio.get_event_loop()
    logger.info("Starting Raidar Bot (polling)...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
