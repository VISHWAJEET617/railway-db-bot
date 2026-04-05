import asyncio
import time
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from tgbot.config import DB_TYPES, ADMIN_ID
from tgbot.database import (
    upsert_user, is_banned, record_request, save_db_record,
    get_user_proxy, save_user_proxy, delete_user_proxy,
)
from tgbot.progress import ProgressTracker, run_edit_loop
from tgbot.queue_manager import queue_manager
from tgbot.railway_adapter import create_railway_db
from tgbot.log_channel import lc

IST = timezone(timedelta(hours=5, minutes=30))


def _db_menu_keyboard(prefix: str = "db"):
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
    if creds.get("username"):
        lines.append(f"👤 <b>User:</b>     <code>{creds['username']}</code>")
    lines.append(f"🔑 <b>Password:</b> <code>{creds.get('password','')}</code>")
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


# ── Core job runner ───────────────────────────────────────────────────────────

async def _run_job(bot, user, db_type, chat_id, message_id, cancel_event, is_admin_user, fresh=True, proxy=None):
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


async def _launch_job(bot, user, db_type, chat_id, message_id, fresh, proxy_cfg, context):
    """Submit to queue and run the provisioning job."""
    is_admin_user = (user.id == ADMIN_ID)
    cancel_event  = asyncio.Event()

    try:
        future = await queue_manager.submit(user.id, cancel_event, chat_id, message_id)
    except ValueError:
        return

    if future is not None:
        pos    = queue_manager.queue_size()
        now    = datetime.now(IST).strftime("%H:%M:%S")
        cpu    = queue_manager.current_cpu()
        reason = f"CPU {cpu:.0f}%" if cpu >= 80 else f"{queue_manager.active_count()} active jobs"
        try:
            await bot.edit_message_text(
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

        asyncio.create_task(lc.queued(bot, user, db_type, pos, reason))

        try:
            await future
        except asyncio.CancelledError:
            return

    asyncio.create_task(lc.db_started(bot, user, db_type))

    try:
        await _run_job(bot, user, db_type, chat_id, message_id, cancel_event, is_admin_user,
                       fresh=fresh, proxy=proxy_cfg)
    finally:
        await queue_manager.done(user.id)


# ── Inline proxy collection ───────────────────────────────────────────────────

_PROXY_PROMPT = (
    "📡 <b>Send your proxy now:</b>\n\n"
    "<code>ip:port</code>\n"
    "<code>ip:port:user:pass</code>\n"
    "<code>socks5://user:pass@host:port</code>\n\n"
    "<i>Send /cancel to abort.</i>"
)


async def _ask_proxy_inline(user, db_type: str, fresh: bool, edit_fn, send_fn, context, is_admin: bool = False):
    """Edit/send the proxy prompt and set awaiting_proxy state."""
    prefix = "newdb" if fresh else "db"
    label  = DB_TYPES[db_type]["label"]

    text = (
        f"📡 <b>No proxy set.</b>\n\n"
        f"Send your proxy now to proceed with <b>{label}</b>:\n\n"
        f"<code>ip:port</code>\n"
        f"<code>ip:port:user:pass</code>\n"
        f"<code>socks5://user:pass@host:port</code>\n\n"
        f"<i>Send /cancel to abort.</i>"
    )

    kb = None
    if is_admin:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Skip Proxy (Admin)", callback_data=f"skip_proxy_{prefix}_{db_type}"),
        ]])

    if edit_fn:
        await edit_fn(text, parse_mode="HTML", reply_markup=kb)
    else:
        await send_fn(text, parse_mode="HTML", reply_markup=kb)

    context.user_data["awaiting_proxy"] = {
        "db_type":  db_type,
        "fresh":    fresh,
        "attempts": 0,
    }


async def handle_proxy_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle proxy text message during the inline /getdb proxy collection flow."""
    state = context.user_data.get("awaiting_proxy")
    if not state:
        return

    user      = update.effective_user
    text      = update.message.text.strip()
    db_type   = state["db_type"]
    fresh     = state.get("fresh", False)

    from tgbot.handlers.proxy import parse_proxy_str, check_proxy, _specific_error_tip

    if not parse_proxy_str(text):
        await update.message.reply_text(
            "❌ Invalid proxy format.\n\n"
            "Supported:\n"
            "<code>ip:port</code>\n"
            "<code>ip:port:user:pass</code>\n"
            "<code>http://user:pass@host:port</code>\n"
            "<code>socks5://user:pass@host:port</code>",
            parse_mode="HTML",
        )
        return

    state["attempts"] = state.get("attempts", 0) + 1
    remaining = 3 - state["attempts"]

    checking = await update.message.reply_text(
        f"🔍 Checking proxy <code>{text[:50]}</code>...", parse_mode="HTML"
    )

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, check_proxy, text)

    if result["status"] == "pass" and result.get("anonymity") != "transparent":
        save_user_proxy(user.id, text, result)
        context.user_data.pop("awaiting_proxy", None)

        proxy_cfg = parse_proxy_str(text)
        anon      = result.get("anonymity", "unknown")
        anon_icon = "🟢" if anon == "elite" else "🟡"
        latency   = result.get("latency_ms", 0)

        await checking.edit_text(
            f"✅ <b>Proxy Verified!</b> {anon_icon} {anon.capitalize()} — {latency}ms\n\n"
            f"🚀 Starting <b>{DB_TYPES[db_type]['label']}</b> provisioning...",
            parse_mode="HTML",
        )

        if user.id != ADMIN_ID:
            record_request(user.id)

        asyncio.create_task(_launch_job(
            context.bot, user, db_type,
            checking.chat_id, checking.message_id,
            fresh, proxy_cfg, context,
        ))

    elif result.get("anonymity") == "transparent":
        base = (
            "🔴 <b>Transparent Proxy Rejected</b>\n\n"
            "⚠️ This proxy leaks your real IP — Railway would detect and block the account.\n"
            "Please send an <b>Anonymous</b> or <b>Elite</b> proxy."
        )
        if remaining <= 0:
            context.user_data.pop("awaiting_proxy", None)
            await checking.edit_text(
                f"{base}\n\n❌ Too many attempts. Use /getdb to start over.",
                parse_mode="HTML",
            )
        else:
            await checking.edit_text(
                f"{base}\n\n<i>Send another proxy ({remaining} attempt(s) left):</i>",
                parse_mode="HTML",
            )

    else:
        tip = _specific_error_tip(result)
        if remaining <= 0:
            context.user_data.pop("awaiting_proxy", None)
            await checking.edit_text(
                f"❌ <b>Proxy Failed</b>\n\n{tip}\n\n❌ Too many attempts. Use /getdb to start over.",
                parse_mode="HTML",
            )
        else:
            await checking.edit_text(
                f"❌ <b>Proxy Failed</b>\n\n{tip}\n\n<i>Send another proxy ({remaining} attempt(s) left):</i>",
                parse_mode="HTML",
            )


# ── Main flow ─────────────────────────────────────────────────────────────────

async def _start_db_flow(
    update_or_query, context, db_type: str,
    fresh: bool = False, force_no_proxy: bool = False,
):
    """Shared entry point for /getdb, /newdb, and the DB type button callback."""
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

    prefix = "newdb" if fresh else "db"

    # ── Admin bypass ──────────────────────────────────────────────────────────
    if force_no_proxy:
        note      = " — fresh account, no session reuse" if fresh else ""
        init_text = (
            f"🗃️ Preparing your <b>{DB_TYPES[db_type]['label']}</b>{note}...\n"
            f"<i>You'll be placed in the provisioning queue.</i>"
        )
        if edit_msg:
            sent       = await edit_msg(init_text, parse_mode="HTML")
            chat_id    = update_or_query.message.chat_id
            message_id = update_or_query.message.message_id
        else:
            sent       = await send_msg(init_text, parse_mode="HTML")
            chat_id    = sent.chat_id
            message_id = sent.message_id

        if not is_admin_user:
            record_request(user.id)

        asyncio.create_task(_launch_job(
            context.bot, user, db_type, chat_id, message_id, fresh, None, context,
        ))
        return

    # ── No proxy stored → ask inline ─────────────────────────────────────────
    proxy_row = get_user_proxy(user.id)
    if not proxy_row:
        await _ask_proxy_inline(user, db_type, fresh, edit_msg, send_msg, context, is_admin=is_admin_user)
        return

    # ── Proxy stored → silent health check ───────────────────────────────────
    from tgbot.handlers.proxy import check_proxy as _check_proxy, parse_proxy_str, _specific_error_tip

    checking_text = "🔍 Verifying your proxy before starting..."
    if edit_msg:
        await edit_msg(checking_text, parse_mode="HTML")
        chat_id    = update_or_query.message.chat_id
        message_id = update_or_query.message.message_id
    else:
        sent       = await send_msg(checking_text, parse_mode="HTML")
        chat_id    = sent.chat_id
        message_id = sent.message_id

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _check_proxy, proxy_row["proxy_str"])

    # Dead proxy → delete + ask inline
    if result["status"] == "fail":
        delete_user_proxy(user.id)
        tip = _specific_error_tip(result)
        context.user_data["awaiting_proxy"] = {"db_type": db_type, "fresh": fresh, "attempts": 0}
        admin_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Skip Proxy (Admin)", callback_data=f"skip_proxy_{prefix}_{db_type}"),
        ]]) if is_admin_user else None
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"⚠️ <b>Stored proxy is dead — removed.</b>\n\n"
                f"{tip}\n\n"
                f"📡 <b>Send a new proxy now:</b>\n"
                f"<code>ip:port</code>  <code>ip:port:user:pass</code>\n\n"
                f"<i>Send /cancel to abort.</i>"
            ),
            parse_mode="HTML",
            reply_markup=admin_kb,
        )
        return

    # Transparent proxy → delete + ask inline
    if result.get("anonymity") == "transparent":
        delete_user_proxy(user.id)
        context.user_data["awaiting_proxy"] = {"db_type": db_type, "fresh": fresh, "attempts": 0}
        admin_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Skip Proxy (Admin)", callback_data=f"skip_proxy_{prefix}_{db_type}"),
        ]]) if is_admin_user else None
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "🔴 <b>Stored proxy is Transparent (leaks real IP) — removed.</b>\n\n"
                "Please send an Anonymous or Elite proxy:\n"
                "<code>ip:port</code>  <code>ip:port:user:pass</code>\n\n"
                "<i>Send /cancel to abort.</i>"
            ),
            parse_mode="HTML",
            reply_markup=admin_kb,
        )
        return

    # Proxy healthy → ask use / replace
    anon      = result.get("anonymity", "unknown")
    anon_icon = "🟢" if anon == "elite" else "🟡"
    latency   = result.get("latency_ms", 0)
    lat_icon  = "🟢" if latency < 800 else ("🟡" if latency < 2000 else "🔴")
    proxy_ip  = result.get("proxy_ip", "N/A")

    rows = [
        [InlineKeyboardButton("✅ Use Stored Proxy",         callback_data=f"use_stored_{prefix}_{db_type}")],
        [InlineKeyboardButton("🔄 Enter a Different Proxy",  callback_data=f"new_proxy_{prefix}_{db_type}")],
    ]
    if is_admin_user:
        rows.append([InlineKeyboardButton("⚡ Skip Proxy (Admin)", callback_data=f"skip_proxy_{prefix}_{db_type}")])
    kb = InlineKeyboardMarkup(rows)

    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=(
            f"🛡️ <b>Proxy Verified</b>\n\n"
            f"🌐 IP: <code>{proxy_ip}</code>\n"
            f"{anon_icon} Anonymity: {anon.capitalize()}\n"
            f"{lat_icon} Latency: {latency} ms\n\n"
            f"Use this proxy for <b>{DB_TYPES[db_type]['label']}</b>?"
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Command handlers ──────────────────────────────────────────────────────────

async def getdb_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    user = update.effective_user
    cancelled = await queue_manager.cancel(user.id)
    await queue_manager.force_clear(user.id)

    context.user_data.pop("awaiting_proxy", None)

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


# ── Callback handler ──────────────────────────────────────────────────────────

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
        context.user_data.pop("awaiting_proxy", None)
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

    # ── use stored proxy → launch immediately ─────────────────────────────────
    if data.startswith("use_stored_"):
        parts   = data.split("_", 3)          # ["use","stored","db","postgresql"]
        prefix  = parts[2]
        db_type = parts[3] if len(parts) > 3 else ""
        fresh   = (prefix == "newdb")
        if db_type not in DB_TYPES:
            await query.edit_message_text("Unknown database type.")
            return

        upsert_user(user.id, user.username, user.first_name)
        proxy_row = get_user_proxy(user.id)
        if not proxy_row:
            await query.edit_message_text(
                "⚠️ Proxy no longer found. Use /getdb to start again.", parse_mode="HTML"
            )
            return

        from tgbot.handlers.proxy import parse_proxy_str
        proxy_cfg = parse_proxy_str(proxy_row["proxy_str"])

        note      = " — fresh account" if fresh else ""
        init_text = (
            f"🗃️ Preparing your <b>{DB_TYPES[db_type]['label']}</b>{note}...\n"
            f"<i>You'll be placed in the provisioning queue.</i>"
        )
        await query.edit_message_text(init_text, parse_mode="HTML")
        chat_id    = query.message.chat_id
        message_id = query.message.message_id

        if user.id != ADMIN_ID:
            record_request(user.id)

        asyncio.create_task(_launch_job(
            context.bot, user, db_type, chat_id, message_id, fresh, proxy_cfg, context,
        ))
        return

    # ── enter a different proxy inline ────────────────────────────────────────
    if data.startswith("new_proxy_"):
        parts   = data.split("_", 3)          # ["new","proxy","db","postgresql"]
        prefix  = parts[2]
        db_type = parts[3] if len(parts) > 3 else ""
        fresh   = (prefix == "newdb")
        if db_type not in DB_TYPES:
            await query.edit_message_text("Unknown database type.")
            return

        context.user_data["awaiting_proxy"] = {"db_type": db_type, "fresh": fresh, "attempts": 0}
        await query.edit_message_text(
            f"📡 <b>Send your new proxy for {DB_TYPES[db_type]['label']}:</b>\n\n"
            f"<code>ip:port</code>\n"
            f"<code>ip:port:user:pass</code>\n"
            f"<code>socks5://user:pass@host:port</code>\n\n"
            f"<i>Send /cancel to abort.</i>",
            parse_mode="HTML",
        )
        return

    # ── admin: skip proxy ─────────────────────────────────────────────────────
    if data.startswith("skip_proxy_"):
        if user.id != ADMIN_ID:
            await query.answer("Not authorised.", show_alert=True)
            return
        parts   = data.split("_", 3)
        prefix  = parts[2]
        db_type = parts[3] if len(parts) > 3 else ""
        fresh   = (prefix == "newdb")
        if db_type not in DB_TYPES:
            await query.edit_message_text("Unknown database type.")
            return
        upsert_user(user.id, user.username, user.first_name)
        asyncio.create_task(_start_db_flow(query, context, db_type, fresh=fresh, force_no_proxy=True))
        return

    # ── db type selection ─────────────────────────────────────────────────────
    if data.startswith("db_") or data.startswith("newdb_"):
        fresh   = data.startswith("newdb_")
        db_type = data[6:] if fresh else data[3:]
        if db_type not in DB_TYPES:
            await query.edit_message_text("Unknown database type.")
            return
        upsert_user(user.id, user.username, user.first_name)
        asyncio.create_task(_start_db_flow(query, context, db_type, fresh=fresh))
