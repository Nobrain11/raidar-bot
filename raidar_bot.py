import logging
import asyncio
import sqlite3
import os
from datetime import datetime, timedelta
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
    row = c.fetchone()
    if row:
        return row
    try:
        tg_id = int(identifier)
        c.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
        return c.fetchone()
    except:
        return None

# ====================== COMMANDS ======================

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

# ── RAID ──────────────────────────────────────────────

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

# ── QUEUE ─────────────────────────────────────────────

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

    c.execute("SELECT id, tweet_link, targets, position FROM raid_queue ORDER BY position")
    rows = c.fetchall()
    conn.close()

    queue_text = "\n".join([f"{i+1}. {r[1]}{' — ' + r[2] if r[2] else ''}" for i, r in enumerate(rows)])
    await update.message.reply_text(f"➕ Added to queue!\n\n**Queue:**\n{queue_text}", parse_mode=ParseMode.MARKDOWN)

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
        await update.message.reply_text("❌ No item at that index.")
        conn.close()
        return
    conn.execute("DELETE FROM raid_queue WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🗑 Removed item #{idx} from queue.")

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
    await update.message.reply_text(f"📌 Queue reset. Next raid: {link}")

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
        await update.message.reply_text("❌ Index out of range.")
        conn.close()
        return
    id1, pos1 = rows[i1 - 1]
    id2, pos2 = rows[i2 - 1]
    conn.execute("UPDATE raid_queue SET position = ? WHERE id = ?", (pos2, id1))
    conn.execute("UPDATE raid_queue SET position = ? WHERE id = ?", (pos1, id2))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"🔄 Swapped positions #{i1} and #{i2}.")

async def clearnext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    conn = get_db()
    conn.execute("DELETE FROM raid_queue")
    conn.commit()
    conn.close()
    await update.message.reply_text("🧹 Queue cleared.")

# ── XP & LEADERBOARD ──────────────────────────────────

async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = context.args[0].upper() if context.args else "ALL"
    conn = get_db()
    c = conn.cursor()

    if period == "1D":
        since = (datetime.utcnow() - timedelta(days=1)).isoformat()
        label = "Last 24h"
    elif period == "7D":
        since = (datetime.utcnow() - timedelta(days=7)).isoformat()
        label = "Last 7 Days"
    elif period == "30D":
        since = (datetime.utcnow() - timedelta(days=30)).isoformat()
        label = "Last 30 Days"
    else:
        since = None
        label = "All Time"

    c.execute(
        "SELECT telegram_id, twitter_username, xp FROM users WHERE disqualified = 0 ORDER BY xp DESC LIMIT 10"
    )
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
    if context.args:
        identifier = context.args[0]
    else:
        identifier = str(update.effective_user.id)

    conn = get_db()
    row = get_user_by_identifier(conn, identifier)
    conn.close()

    if not row:
        await update.message.reply_text("❌ User not found.")
        return

    tg_id, tw, wallet, xp, disq, is_pro, joined = row
    name = f"@{tw}" if tw else f"#{tg_id}"
    status = "⛔ Disqualified" if disq else ("⭐ Pro" if is_pro else "✅ Active")
    await update.message.reply_text(
        f"👤 **{name}**\n💎 XP: `{xp}`\n{status}",
        parse_mode=ParseMode.MARKDOWN
    )

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
        await update.message.reply_text("❌ User not found.")
        conn.close()
        return
    conn.execute("UPDATE users SET xp = xp + ? WHERE telegram_id = ?", (amount, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Gave `{amount}` XP to user.", parse_mode=ParseMode.MARKDOWN)

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
        await update.message.reply_text("❌ User not found.")
        conn.close()
        return
    conn.execute("UPDATE users SET xp = MAX(0, xp - ?) WHERE telegram_id = ?", (amount, row[0]))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Removed `{amount}` XP from user.", parse_mode=ParseMode.MARKDOWN)

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
        await update.message.reply_text("❌ User not found.")
        conn.close()
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
        await update.message.reply_text("❌ User not found.")
        conn.close()
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
    lines = ["⛔ **Disqualified Users:**\n"]
    for tg_id, tw in rows:
        lines.append(f"• {'@' + tw if tw else '#' + str(tg_id)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def lbreset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    conn = get_db()
    conn.execute("UPDATE users SET xp = 0")
    conn.commit()
    conn.close()
    await update.message.reply_text("🔄 Leaderboard reset. All XP cleared.")

# ── REWARDS / SETTINGS ────────────────────────────────

async def reward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/reward <user> <amount> <symbol>`", parse_mode=ParseMode.MARKDOWN)
        return
    identifier, amount, symbol = context.args[0], context.args[1], context.args[2]
    conn = get_db()
    row = get_user_by_identifier(conn, identifier)
    conn.close()
    if not row:
        await update.message.reply_text("❌ User not found.")
        return
    name = f"@{row[1]}" if row[1] else f"#{row[0]}"
    await update.message.reply_text(
        f"🎁 **Reward Sent!**\n👤 {name}\n💰 `{amount} {symbol}`",
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
    await update.message.reply_text(
        "⚙️ **Settings**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# ── USER PROFILE ──────────────────────────────────────

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (user.id,))
    conn.commit()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE telegram_id = ?", (user.id,))
    row = c.fetchone()
    conn.close()
    tg_id, tw, wallet, xp, disq, is_pro, joined = row
    lines = [
        f"👤 **Profile**",
        f"🆔 Telegram: `{tg_id}`",
        f"🐦 Twitter: {'@' + tw if tw else '—'}",
        f"💼 Wallet: `{wallet[:6]}...{wallet[-4:]}` " if wallet else "💼 Wallet: —",
        f"💎 XP: `{xp}`",
        f"⭐ Pro: {'Yes' if is_pro else 'No'}",
        f"⛔ Disqualified: {'Yes' if disq else 'No'}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

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
    await update.message.reply_text(f"✅ Wallet connected: `{wallet[:6]}...{wallet[-4:]}`", parse_mode=ParseMode.MARKDOWN)

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/login <twitter_username>`", parse_mode=ParseMode.MARKDOWN)
        return
    tw = context.args[0].lstrip('@')
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (?)", (update.effective_user.id,))
    conn.execute("UPDATE users SET twitter_username = ? WHERE telegram_id = ?", (tw, update.effective_user.id))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Linked Twitter: `@{tw}`", parse_mode=ParseMode.MARKDOWN)

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    conn.execute("UPDATE users SET twitter_username = NULL WHERE telegram_id = ?", (update.effective_user.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("👋 Twitter account unlinked.")

# ── GROUP / PRO ───────────────────────────────────────

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
    await update.message.reply_text(
        "📈 **Trend Boost**\n\nBoost your group's visibility by raiding consistently!\n\n"
        "Keep raiding to climb the ranks. 🚀",
        parse_mode=ParseMode.MARKDOWN
    )

async def pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("⭐ Upgrade to Pro", callback_data="pro_upgrade")]]
    await update.message.reply_text(
        "⭐ **Raidar Pro**\n\nUnlock advanced features:\n• Priority queue\n• XP multipliers\n• Custom rewards\n\nContact admin to upgrade.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

# ── CALLBACK HANDLER ──────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "settings_stats":
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

    logger.info("🚀 Raidar Bot is running!")
    await app.run_polling()

if __name__ == '__main__':
    asyncio.run(main())
