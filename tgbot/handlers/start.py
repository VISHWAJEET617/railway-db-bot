import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from tgbot.database import upsert_user
from tgbot.log_channel import lc


START_TEXT = """
👋 <b>Welcome to Railway DB Bot!</b>

Get a free database in under 2 minutes — fully automated.

<b>Available databases:</b>
🐘 <b>PostgreSQL</b>  |  🐬 <b>MySQL</b>
🍃 <b>MongoDB</b>    |  ⚡ <b>Redis</b>

Each database runs on Railway's free tier:
✅ 512 MB Storage
✅ $5 Free Credit
✅ Ready in ~60–90 seconds

<b>Commands:</b>
/getdb — Get a new database
/mydb — View your latest database
/history — View your last 5 databases
/help — All commands

<i>Press the button below to get started!</i>
""".strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user   = update.effective_user
    is_new = upsert_user(user.id, user.username, user.first_name)

    if is_new:
        asyncio.create_task(lc.new_user(context.bot, user))

    kb = [[InlineKeyboardButton("🚀 Get Free Database", callback_data="getdb_menu")]]
    await update.message.reply_text(
        START_TEXT,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )
