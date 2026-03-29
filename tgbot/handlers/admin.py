import asyncio
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from tgbot.config import ADMIN_ID
from tgbot.database import get_stats, get_all_users, get_all_user_ids, ban_user, unban_user
from tgbot.log_channel import lc

IST = timezone(timedelta(hours=5, minutes=30))


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def _admin_main_text() -> str:
    s   = get_stats()
    now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    return (
        f"🛠️ <b>Admin Panel</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 <b>Live Stats</b>\n"
        f"👤 Total Users:  <b>{s['total_users']}</b>\n"
        f"🗄️ Total DBs:    <b>{s['total_dbs']}</b>\n"
        f"📅 DBs Today:   <b>{s['dbs_today']}</b>\n\n"
        f"<i>Updated: {now}</i>"
    )


def _admin_main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 View All Users",  callback_data="admin_users"),
            InlineKeyboardButton("📅 Today's Stats",   callback_data="admin_today_stats"),
        ],
        [
            InlineKeyboardButton("🚫 Ban User",        callback_data="admin_ban_prompt"),
            InlineKeyboardButton("📢 Broadcast",       callback_data="admin_broadcast_prompt"),
        ],
        [InlineKeyboardButton("🚀 Create DB (Unlimited)", callback_data="getdb_menu")],
    ])


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text(
        _admin_main_text(),
        parse_mode="HTML",
        reply_markup=_admin_main_keyboard(),
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    await update.message.reply_text(_admin_main_text(), parse_mode="HTML")


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    rows  = get_all_users(limit=20)
    lines = ["👥 <b>All Users</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
    for r in rows:
        banned = " 🚫" if r["is_banned"] else ""
        name   = r.get("first_name") or r.get("username") or str(r["user_id"])
        lines.append(
            f"<code>{r['user_id']}</code> — <b>{name}</b>{banned}  [{r['db_count']} DBs]"
        )
    lines.append("\n<i>/ban &lt;user_id&gt; · /unban &lt;user_id&gt;</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
        ban_user(uid)
        await update.message.reply_text(f"✅ User <code>{uid}</code> banned.", parse_mode="HTML")
        asyncio.create_task(lc.user_banned(context.bot, update.effective_user, uid))
    except ValueError:
        await update.message.reply_text("Invalid user_id — must be a number.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
        unban_user(uid)
        await update.message.reply_text(f"✅ User <code>{uid}</code> unbanned.", parse_mode="HTML")
        asyncio.create_task(lc.user_unbanned(context.bot, update.effective_user, uid))
    except ValueError:
        await update.message.reply_text("Invalid user_id — must be a number.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /broadcast &lt;message text&gt;\n\n"
            "Supports HTML formatting (bold, italic, code, links).",
            parse_mode="HTML",
        )
        return

    text     = " ".join(context.args)
    user_ids = get_all_user_ids()

    status_msg = await update.message.reply_text(
        f"📢 Broadcasting to {len(user_ids)} users..."
    )
    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ <b>Broadcast complete</b>\n\n"
        f"✉️ Sent:   <b>{sent}</b>\n"
        f"❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
    )
    asyncio.create_task(lc.broadcast_sent(context.bot, update.effective_user, sent, text))


def _back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]])


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _is_admin(query.from_user.id):
        return

    data = query.data

    if data in ("admin_back", "admin_refresh"):
        await query.edit_message_text(
            _admin_main_text(), parse_mode="HTML", reply_markup=_admin_main_keyboard()
        )

    elif data == "admin_today_stats":
        s   = get_stats()
        now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
        await query.edit_message_text(
            f"📅 <b>Today's Statistics</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🗄️ DBs created today: <b>{s['dbs_today']}</b>\n"
            f"👤 Total users:       <b>{s['total_users']}</b>\n"
            f"📦 All-time DBs:      <b>{s['total_dbs']}</b>\n\n"
            f"<i>Updated: {now}</i>",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    elif data == "admin_users":
        rows  = get_all_users(limit=15)
        lines = ["👥 <b>All Users</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"]
        for r in rows:
            banned = " 🚫" if r["is_banned"] else ""
            name   = r.get("first_name") or r.get("username") or str(r["user_id"])
            lines.append(
                f"<code>{r['user_id']}</code> — <b>{name}</b>{banned}  [{r['db_count']} DBs]"
            )
        lines.append("\n<i>Use 🚫 Ban User button or /ban &lt;id&gt; to ban.</i>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb()
        )

    elif data == "admin_ban_prompt":
        rows = get_all_users(limit=10)
        lines = [
            "🚫 <b>Ban / Unban User</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            "Tap a user below to toggle their ban status,",
            "or use: <code>/ban &lt;user_id&gt;</code> / <code>/unban &lt;user_id&gt;</code>",
            "",
        ]
        buttons = []
        for r in rows:
            name   = r.get("first_name") or r.get("username") or str(r["user_id"])
            action = "✅ Unban" if r["is_banned"] else "🚫 Ban"
            cb     = f"admin_unban_{r['user_id']}" if r["is_banned"] else f"admin_ban_{r['user_id']}"
            lines.append(f"<code>{r['user_id']}</code>  {name} {'🚫 banned' if r['is_banned'] else ''}")
            buttons.append([InlineKeyboardButton(f"{action} {name[:18]}", callback_data=cb)])
        buttons.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "admin_broadcast_prompt":
        await query.edit_message_text(
            "📢 <b>Broadcast Message</b>\n\n"
            "To send a message to all non-banned users, run:\n\n"
            "<code>/broadcast Your message here</code>\n\n"
            "Supports HTML: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, "
            "&lt;code&gt;code&lt;/code&gt;, &lt;a href='...'&gt;link&lt;/a&gt;",
            parse_mode="HTML",
            reply_markup=_back_kb(),
        )

    elif data.startswith("admin_ban_"):
        try:
            uid = int(data.split("_", 3)[-1])
            ban_user(uid)
            await query.answer(f"✅ User {uid} banned.", show_alert=True)
            asyncio.create_task(lc.user_banned(query.bot, query.from_user, uid))
        except Exception:
            await query.answer("Failed.", show_alert=True)

    elif data.startswith("admin_unban_"):
        try:
            uid = int(data.split("_", 3)[-1])
            unban_user(uid)
            await query.answer(f"✅ User {uid} unbanned.", show_alert=True)
            asyncio.create_task(lc.user_unbanned(query.bot, query.from_user, uid))
        except Exception:
            await query.answer("Failed.", show_alert=True)
