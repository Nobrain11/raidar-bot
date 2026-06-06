import logging
import asyncio
import sqlite3
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
ADMIN_IDS = [7761011341]  
BOT_TOKEN = "8923917756:AAHPt3WxqMZmwsC65MN_Fl7YmU1HnxfIHA8"
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
    """Flexible lookup by @username or ID"""
    c = conn.cursor()
    if identifier.startswith('@'):
        identifier = identifier[1:]
        c.execute("SELECT * FROM users WHERE twitter_username = ?", (identifier,))
    else:
        try:
            tg_id = int(identifier)
            c.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
        except ValueError:
            c.execute("SELECT * FROM users WHERE twitter_username = ?", (identifier,))
    return c.fetchone()

# ====================== COMMANDS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("🚀 Welcome to **Raidar**!\nUse /help to see all commands.", parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📓 **Raidar Commands**

**🛠 General**
`/raid <link>` - Start a raid
`/stop` - Stop raid
`/settings` - Settings menu
`/reward <user> <amount> <symbol>` - Reward
`/gxp` - Group XP
`/trend` - Boost group
`/pro` - Upgrade to Pro

**⏳ Queue**
`/next <link> [targets]` - Add to queue
`/delnext <index>` - Remove
`/setnext <link> [targets]` - Set next
`/switchnext <i1> <i2>` - Swap
`/clearnext` - Clear queue

**🏆 XP & Leaderboard**
`/lb [1D|7D|30D|ALL]` - Leaderboard
`/xp <user>` - User XP
`/givexp <user> <amount>` - Give XP
`/remxp <user> <amount>` - Remove XP
`/disqualify <user>` - Disqualify
`/undisqualify <user>` - Undisqualify
`/disqualified` - Disqualified list
`/lbreset` - Reset LB

**🌎 Everyone**
`/profile`, `/connect <wallet>`, `/login`, `/logout`
    """
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# RAID
async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/raid <tweet_link>`", parse_mode=ParseMode.MARKDOWN)
        return
    link = context.args[0]
    conn = get_db()
    conn.execute("DELETE FROM active_raid")
    conn.execute("INSERT INTO active_raid (tweet_link, is_active) VALUES (?, 1)", (link,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🚀 **RAID STARTED!**\n{link}", parse_mode=ParseMode.MARKDOWN)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    conn = get_db()
    conn.execute("UPDATE active_raid SET is_active = 0")
    conn.commit()
    conn.close()
    await update.message.reply_text("🛑 Raid stopped.")

# QUEUE COMMANDS
async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/next <link> [targets]`")
        return
    link = context.args[0]
    targets = " ".join(context.args[1:]) or "Everyone"
    conn = get_db()
    max_pos = conn.execute("SELECT MAX(position) FROM raid_queue").fetchone()[0] or 0
    new_pos = max_pos + 1
    conn.execute("INSERT INTO raid_queue (tweet_link, targets, position, added_by) VALUES (?, ?, ?, ?)",
                 (link, targets, new_pos, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Added to queue **#{new_pos}**\n{link}")

async def delnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/delnext <index>`")
        return
    try:
        idx = int(context.args[0])
        conn = get_db()
        conn.execute("DELETE FROM raid_queue WHERE position = ?", (idx,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"🗑 Removed #{idx}")
    except:
        await update.message.reply_text("Invalid index.")

async def setnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/setnext <link> [targets]`")
        return
    link = context.args[0]
    targets = " ".join(context.args[1:]) or "Everyone"
    conn = get_db()
    conn.execute("DELETE FROM raid_queue WHERE position = 1")
    conn.execute("INSERT INTO raid_queue (tweet_link, targets, position, added_by) VALUES (?, ?, 1, ?)",
                 (link, targets, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Set as next (pos 1)")

async def switchnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/switchnext <i1> <i2>`")
        return
    try:
        i1, i2 = int(context.args[0]), int(context.args[1])
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE raid_queue SET position = 9999 WHERE position = ?", (i1,))
        c.execute("UPDATE raid_queue SET position = ? WHERE position = ?", (i1, i2))
        c.execute("UPDATE raid_queue SET position = ? WHERE position = 9999", (i2,))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"🔄 Swapped {i1} ↔ {i2}")
    except:
        await update.message.reply_text("Invalid indices.")

async def clearnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    conn.execute("DELETE FROM raid_queue")
    conn.commit()
    conn.close()
    await update.message.reply_text("🧹 Queue cleared.")

# XP & LEADERBOARD
async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    rows = conn.execute("SELECT twitter_username, xp FROM users WHERE disqualified=0 ORDER BY xp DESC LIMIT 15").fetchall()
    conn.close()
    text = "🏆 **Leaderboard**\n\n"
    for i, (name, xp) in enumerate(rows, 1):
        text += f"{i}. {name or 'Anonymous'} — {xp} XP\n"
    await update.message.reply_text(text or "No data.", parse_mode=ParseMode.MARKDOWN)

async def xp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/xp <user>`")
        return
    conn = get_db()
    user = get_user_by_identifier(conn, context.args[0])
    conn.close()
    if user:
        await update.message.reply_text(f"📊 **{user[1] or 'User'}**: {user[3]} XP")
    else:
        await update.message.reply_text("User not found.")

async def givexp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/givexp <user> <amount>`")
        return
    try:
        amount = int(context.args[1])
        conn = get_db()
        user = get_user_by_identifier(conn, context.args[0])
        if user:
            conn.execute("UPDATE users SET xp = xp + ? WHERE telegram_id = ?", (amount, user[0]))
            conn.commit()
            await update.message.reply_text(f"✅ +{amount} XP given.")
        conn.close()
    except:
        await update.message.reply_text("Invalid amount.")

async def remxp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/remxp <user> <amount>`")
        return
    try:
        amount = int(context.args[1])
        conn = get_db()
        user = get_user_by_identifier(conn, context.args[0])
        if user:
            conn.execute("UPDATE users SET xp = xp - ? WHERE telegram_id = ?", (amount, user[0]))
            conn.commit()
            await update.message.reply_text(f"✅ -{amount} XP removed.")
        conn.close()
    except:
        await update.message.reply_text("Invalid amount.")

async def disqualify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/disqualify <user>`")
        return
    conn = get_db()
    user = get_user_by_identifier(conn, context.args[0])
    if user:
        conn.execute("UPDATE users SET disqualified = 1 WHERE telegram_id = ?", (user[0],))
        conn.commit()
        await update.message.reply_text("🚫 User disqualified.")
    conn.close()

async def undisqualify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/undisqualify <user>`")
        return
    conn = get_db()
    user = get_user_by_identifier(conn, context.args[0])
    if user:
        conn.execute("UPDATE users SET disqualified = 0 WHERE telegram_id = ?", (user[0],))
        conn.commit()
        await update.message.reply_text("✅ User undisqualified.")
    conn.close()

async def disqualified_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    conn = get_db()
    rows = conn.execute("SELECT telegram_id, twitter_username FROM users WHERE disqualified=1").fetchall()
    conn.close()
    text = "**Disqualified Users:**\n" + "\n".join([f"• {r[1] or r[0]}" for r in rows]) or "None"
    await update.message.reply_text(text)

async def lbreset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    conn = get_db()
    conn.execute("UPDATE users SET xp = 0")
    conn.commit()
    conn.close()
    await update.message.reply_text("🔄 Leaderboard reset.")

async def reward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/reward <user> <amount> <symbol>`")
        return
    try:
        amount = int(context.args[1])
        symbol = context.args[2]
        conn = get_db()
        user = get_user_by_identifier(conn, context.args[0])
        if user:
            await update.message.reply_text(f"💰 Rewarded **{amount} {symbol}** to user.")
        conn.close()
    except:
        await update.message.reply_text("Invalid command.")

# SETTINGS + PROFILE
async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("View Queue", callback_data='view_queue')],
        [InlineKeyboardButton("Active Raid", callback_data='active_raid')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ **Settings Menu**", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'view_queue':
        conn = get_db()
        rows = conn.execute("SELECT position, tweet_link FROM raid_queue ORDER BY position").fetchall()
        conn.close()
        text = "**Current Queue:**\n" + "\n".join([f"{p}. {l}" for p,l in rows]) or "Empty"
        await query.edit_message_text(text)

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    conn.close()
    if user:
        await update.message.reply_text(f"👤 **Profile**\nXP: {user[3]}\nWallet: {user[2] or 'None'}\nPro: {'Yes' if user[5] else 'No'}")
    else:
        await update.message.reply_text("Use /start first.")

async def connect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/connect <wallet>`")
        return
    conn = get_db()
    conn.execute("UPDATE users SET wallet_address=? WHERE telegram_id=?", (context.args[0], update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Wallet connected!")

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔗 Twitter login (simulation - full OAuth in v2)")

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    conn.execute("UPDATE users SET twitter_username=NULL WHERE telegram_id=?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Logged out.")

async def gxp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 Group Trending XP: **Active**")

async def trend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text("🔥 Group boosted to Trending!")

async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    conn.execute("UPDATE users SET is_pro=1 WHERE telegram_id=?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("⭐ You are now **Raidar Pro**!")

# ====================== MAIN ======================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

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

    logger.info("🚀 Full Raidar Bot started with ALL commands!")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
