import logging
import asyncio
import sqlite3
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
ADMIN_IDS = [7761011341]
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is required!")

DB_NAME = 'raidar.db'

# ====================== DATABASE ======================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        twitter_username TEXT,
        wallet_address TEXT,
        xp INTEGER DEFAULT 0,
        disqualified BOOLEAN DEFAULT 0,
        is_pro BOOLEAN DEFAULT 0,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP
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
        id INTEGER PRIMARY KEY,
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
    else:
        try:
            tg_id = int(identifier)
            c.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
        except:
            c.execute("SELECT * FROM users WHERE twitter_username = ?", (identifier,))
    return c.fetchone()

# ====================== COMMANDS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🚀 Welcome to **Raidar**!\nUse /help for all commands.", parse_mode=ParseMode.MARKDOWN)

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

# Add all other command functions here (they are already implemented in your file)

# ====================== MAIN ======================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register all handlers
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

    logger.info("🚀 Raidar Bot is running with ALL commands!")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
