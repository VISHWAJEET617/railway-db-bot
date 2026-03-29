import asyncio
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from tgbot.config import DB_TYPES, ADMIN_ID
from tgbot.database import upsert_user, is_banned, record_request, save_db_record, get_user_proxy
from tgbot.progress import ProgressTracker, run_edit_loop
from tgbot.queue_manager import queue_manager
from tgbot.railway_adapter import create_railway_db
from tgbot.log_channel import lc


def _db_menu_keyboard(prefix: str = "db"):
    """
    prefix='db'    → regular /getdb flow (fresh account by default, no explicit label)
    prefix='newdb' → /newdb flow (explicitly a brand-new account, communicated to user)
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐘 PostgreSQL", callback_data=f"{prefix}_postgresql"),
            InlineKeyboardButton("🐬 MySQL",      callback_data=f"{prefix}_mysql"),
        ],
        [
            InlineKeyboardButton("🍃 MongoDB",    callback_data=f"{prefix}_mongodb"),
            InlineKeyboardButton("⚡ Redis",       callback_data=f"{prefix}_redis"),
        ],
        [InlineKeyboardButton("❌ Cancel",        callback_data="cancel_menu")],
    ])


def _cred_card(db_type: str, creds: dict) -> str:
    cfg   = DB_TYPES[db_type]
    label = cfg["label"]
    public_url = creds.get("db_url") or "N/A"
    # Warn if the URL is still Railway-internal (TCP proxy likely failed)
    internal_note = ""
    if ".railway.internal" in (public_url or ""):
        internal_note = "\n⚠️ <i>This is a private URL (internal Railway network only).</i>"

    if creds.get("public_url_blocked"):
        internal_note += "\n⚠️ <i>Public URL (TCP proxy) is blocked for this temporary Railway account. Try again later or use MySQL/Postgres for a public URL.</i>"

    lines = [
        f"✅ <b>{label} Database Ready!</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📦 <b>Type:</b>    {label}",
        f"📧 <b>Account:</b> <code>{creds.get('email','')}</code>",
        "",
        f"🔗 <b>Public URL:</b>",
        f"<code>{public_url}</code>{internal_note}",
        "",
        f"🔒 <b>Private URL:</b>",
        f"<code>{creds.get('private_url') or 'N/A'}</code>",
        "",
        f"🌐 <b>Host:</b>     <code>{creds.get('host','')}</code>",
        f"🔌 <b>Port:</b>     <code>{creds.get('port','')}</code>",
    ]
    # Only show User if applicable (not Redis)
    if creds.get("username"):
        lines.append(f"👤 <b>User:</b>     <code>{creds['username']}</code>")
    lines.append(f"🔑 <b>Password:</b> <code>{creds.get('password','')}</code>")
    # Only show DB Name if applicable (not Redis)
    if creds.get("db_name"):
        lines.append(f"🗄️ <b>DB Name:</b>  <code>{creds['db_name']}</code>")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🔗 <a href=\"{creds.get('project_url','')}\">Open in Railway</a>",
    ]
    return "\n".join(lines)


def _cred_keyboard(db_url: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Copy URL", switch_inline_query=db_url),
            InlineKeyboardButton("🔄 Get Another DB", callback_data="getdb_menu"),
        ],
        [InlineKeyboardButton("📖 How to Use", callback_data="how_to_use")],
    ])


HOW_TO_USE = """
📖 <b>How to Use Your Database</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Node.js (pg):</b>
<pre>const { Pool } = require('pg')
const pool = new Pool({ connectionString: 'YOUR_URL' })
const res = await pool.query('SELECT NOW()')</pre>

<b>Python (psycopg2):</b>
<pre>import psycopg2
conn = psycopg2.connect('YOUR_URL')
cur = conn.cursor()
cur.execute('SELECT version()')</pre>

<b>Prisma (.env):</b>
<pre>DATABASE_URL="YOUR_URL"</pre>

<b>Django (settings.py):</b>
<pre>import dj_database_url
DATABASES = {'default': dj_database_url.parse('YOUR_URL')}</pre>

Replace <code>YOUR_URL</code> with the Public URL from your credential card.
""".strip()


async def _run_job(bot, user, db_type, chat_id, message_id, cancel_event, is_admin_user, fresh=True, proxy=None):
    """Core provisioning logic shared by /getdb and /newdb."""
    cfg     = DB_TYPES[db_type]
    tracker = ProgressTracker(db_type, cfg["label"])
    t_start = time.monotonic()

    await bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=tracker.render(),
        parse_mode="HTML",
    )

    async def progress_cb(step_id: str, status: str, meta: str | None = None):
        await tracker.update(step_id, status, meta)

    edit_task = asyncio.create_task(
        run_edit_loop(bot, chat_id, message_id, tracker)
    )

    try:
        creds = await create_railway_db(db_type, progress_cb, cancel_event, fresh=fresh, proxy=proxy)
        tracker.done = True
        await edit_task

        save_db_record(user.id, db_type, creds)
        elapsed = time.monotonic() - t_start

        await bot.send_message(
            chat_id=chat_id,
            text=_cred_card(db_type, creds),
            parse_mode="HTML",
            reply_markup=_cred_keyboard(creds.get("db_url", "")),
            disable_web_page_preview=True,
        )

        asyncio.create_task(lc.db_created(bot, user, db_type, creds, elapsed))

    except RuntimeError as e:
        err = str(e)
        elapsed = time.monotonic() - t_start
        if "Cancelled" in err:
            tracker.error = "Cancelled by user."
            tracker.done  = True
            await edit_task
            asyncio.create_task(lc.db_cancelled(bot, user, db_type))
        else:
            tracker.error = err
            tracker.done  = True
            await edit_task
            asyncio.create_task(lc.db_failed(bot, user, db_type, err, elapsed))


async def _start_db_flow(
    update_or_query, context, db_type: str,
    fresh: bool = False, force_no_proxy: bool = False,
):
    """Shared entry point for /getdb, /newdb, and the DB type button callback.

    force_no_proxy=True is only allowed for admin — skips the proxy requirement.
    """
    if hasattr(update_or_query, "effective_user"):
        user     = update_or_query.effective_user
        send_msg = update_or_query.message.reply_text
        edit_msg = None
    else:
        user     = update_or_query.from_user
        send_msg = None
        edit_msg = update_or_query.edit_message_text

    is_admin_user = (user.id == ADMIN_ID)

    if is_banned(user.id):
        msg = "🚫 You are banned from using this bot."
        if edit_msg:
            await edit_msg(msg)
        else:
            await send_msg(msg)
        return

    if queue_manager.is_queued(user.id):
        msg = "⏳ You already have a database being created. Please wait or use /cancel."
        if edit_msg:
            await edit_msg(msg)
        else:
            await send_msg(msg)
        return

    # ── Proxy check — mandatory for EVERYONE ─────────────────────────────────
    # Admin may skip by tapping the "⚡ Skip (Admin)" button (force_no_proxy).
    from tgbot.handlers.proxy import parse_proxy_str
    proxy_row = get_user_proxy(user.id)
    has_proxy = proxy_row and proxy_row.get("is_valid")

    if not force_no_proxy and not has_proxy:
        prefix = "newdb" if fresh else "db"
        if is_admin_user:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛡️ How to Set Proxy", callback_data="setproxy_help")],
                [InlineKeyboardButton("⚡ Skip Proxy (Admin)", callback_data=f"skip_proxy_{prefix}_{db_type}")],
            ])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🛡️ How to Set Proxy", callback_data="setproxy_help"),
            ]])
        msg = (
            "🔒 <b>Proxy Required</b>\n\n"
            "You need a verified proxy to get a free database.\n\n"
            "Use <code>/setproxy ip:port</code> to set one.\n"
            "Or <code>/setproxy ip:port:user:pass</code> with auth.\n\n"
            "Once set, run /getdb again."
        )
        if edit_msg:
            await edit_msg(msg, parse_mode="HTML", reply_markup=kb)
        else:
            await send_msg(msg, parse_mode="HTML", reply_markup=kb)
        return

    if force_no_proxy:
        proxy_cfg = None
    elif has_proxy:
        proxy_cfg = parse_proxy_str(proxy_row["proxy_str"])
    else:
        proxy_cfg = None

    note = " — fresh account, no session reuse" if fresh else ""
    init_text = (
        f"🗃️ Preparing your <b>{DB_TYPES[db_type]['label']}</b>{note}...\n"
        f"<i>You'll be placed in the provisioning queue.</i>"
    )
    if edit_msg:
        sent = await edit_msg(init_text, parse_mode="HTML")
        chat_id    = update_or_query.message.chat_id
        message_id = update_or_query.message.message_id
    else:
        sent       = await send_msg(init_text, parse_mode="HTML")
        chat_id    = sent.chat_id
        message_id = sent.message_id

    if not is_admin_user:
        record_request(user.id)

    cancel_event = asyncio.Event()

    try:
        future = await queue_manager.submit(user.id, cancel_event, chat_id, message_id)
    except ValueError:
        return

    if future is not None:
        pos = queue_manager.queue_size()
        from datetime import datetime, timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(IST).strftime("%H:%M:%S")
        cpu = queue_manager.current_cpu()
        reason = f"CPU {cpu:.0f}%" if cpu >= 80 else f"{queue_manager.active_count()} active jobs"
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"⏳ <b>You're #{pos} in queue</b>\n\n"
                    f"Hang tight! Your database will start provisioning soon.\n\n"
                    f"<i>Updated: {now} IST</i>\n\n"
                    f"Use /cancel to leave the queue."
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass

        asyncio.create_task(lc.queued(context.bot, user, db_type, pos, reason))

        try:
            await future
        except asyncio.CancelledError:
            return

    asyncio.create_task(lc.db_started(context.bot, user, db_type))

    try:
        await _run_job(
            context.bot, user, db_type, chat_id, message_id, cancel_event, is_admin_user,
            fresh=fresh, proxy=proxy_cfg,
        )
    finally:
        await queue_manager.done(user.id)


async def getdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /getdb — shows DB type selection menu."""
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    if is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    await update.message.reply_text(
        "🗃️ <b>Choose a database type:</b>\n\nAll databases are hosted on Railway's free tier.",
        parse_mode="HTML",
        reply_markup=_db_menu_keyboard(),
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for /cancel — cancels any active/queued job and always force-clears state."""
    user = update.effective_user
    cancelled = await queue_manager.cancel(user.id)

    # Force-remove from active even if cancel() didn't find it
    # (handles bot-restart edge case where in-memory state was lost)
    await queue_manager.force_clear(user.id)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Get New DB", callback_data="getdb_menu"),
    ]])

    if cancelled:
        await update.message.reply_text(
            "✅ <b>Cancelled.</b>\n\nYour request has been stopped. Ready to start fresh!",
            parse_mode="HTML",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            "✅ <b>All clear!</b>\n\nNo active request found — you're free to create a new database.",
            parse_mode="HTML",
            reply_markup=kb,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    data = query.data

    if data == "getdb_menu":
        upsert_user(user.id, user.username, user.first_name)
        await query.edit_message_text(
            "🗃️ <b>Choose a database type:</b>\n\nAll databases are hosted on Railway's free tier.",
            parse_mode="HTML",
            reply_markup=_db_menu_keyboard(),
        )
        return

    if data == "cancel_menu":
        await query.edit_message_text("❌ Cancelled.")
        return

    if data == "cancel_job":
        cancelled = await queue_manager.cancel(user.id)
        if not cancelled:
            await query.answer("No active job to cancel.", show_alert=True)
        return

    if data == "how_to_use":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="getdb_menu")]])
        await query.edit_message_text(HOW_TO_USE, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("skip_proxy_"):
        # Only admin may use this — silently reject anyone else
        if user.id != ADMIN_ID:
            await query.answer("Not authorised.", show_alert=True)
            return
        # data format: skip_proxy_{prefix}_{db_type}
        # prefix is "db" or "newdb"; db_type may contain underscores (none currently)
        parts  = data.split("_", 3)   # ["skip","proxy","db","postgresql"]
        prefix  = parts[2]             # "db" or "newdb"
        db_type = parts[3] if len(parts) > 3 else ""
        fresh   = (prefix == "newdb")
        if db_type not in DB_TYPES:
            await query.edit_message_text("Unknown database type.")
            return
        upsert_user(user.id, user.username, user.first_name)
        asyncio.create_task(_start_db_flow(query, context, db_type, fresh=fresh, force_no_proxy=True))
        return

    if data.startswith("db_") or data.startswith("newdb_"):
        fresh   = data.startswith("newdb_")
        db_type = data[6:] if fresh else data[3:]
        if db_type not in DB_TYPES:
            await query.edit_message_text("Unknown database type.")
            return
        upsert_user(user.id, user.username, user.first_name)
        asyncio.create_task(_start_db_flow(query, context, db_type, fresh=fresh))
