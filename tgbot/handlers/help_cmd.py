from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from tgbot.config import COOLDOWN_SECS

HELP_TEXT = f"""
📖 <b>Railway DB Bot — All Commands</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>🗃️ Database Commands:</b>
/getdb — Get a free database (choose type)
/cancel — Cancel your active or queued request
/mydb — View all your databases
/history — View your last 5 databases

<b>Supported types:</b>  🐘 PostgreSQL  |  🐬 MySQL  |  🍃 MongoDB  |  ⚡ Redis

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>🛡️ Proxy Commands:</b>
/setproxy — Set your proxy (required to get a DB)
/checkproxy — Check your proxy anonymity and speed

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>⚙️ Info:</b>
• <b>{COOLDOWN_SECS // 60}-minute cooldown</b> between requests
• Queue system: only one provisioning job at a time
• Takes 60–120 seconds — live progress tracker updates every 3s

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>ℹ️ General:</b>
/start — Welcome message
/ping — Check liveness of your DB connections
/verify — Verify any Railway URL in real-time
/help — This help page
""".strip()


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Get a Database Now", callback_data="getdb_menu")]
    ])
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML", reply_markup=kb)
