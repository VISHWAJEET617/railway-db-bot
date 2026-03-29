import asyncio
import re
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from tgbot.database import get_latest_db, get_all_dbs, get_db_by_id, get_history, upsert_user, delete_db_record
from tgbot.config import DB_TYPES, ADMIN_ID
from tgbot.railway_api import fetch_railway_balance, format_balance_lines

IST = timezone(timedelta(hours=5, minutes=30))

PAGE_SIZE = 5


# ── helpers ──────────────────────────────────────────────────────────────────

def _short_project_id(project_url: str) -> str:
    """Extract short 8-char project UUID from Railway project URL."""
    m = re.search(r'/project/([a-f0-9\-]+)', project_url or "")
    if m:
        return m.group(1)[:8].upper()
    return "UNKNOWN"


def _days_since(created_at_str: str) -> int:
    try:
        dt = datetime.fromisoformat(created_at_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


def _db_type_emoji(db_type: str) -> str:
    return DB_TYPES.get(db_type, {}).get("emoji", "🗄️")


def _fmt_date(created_at_str: str) -> str:
    try:
        return datetime.fromisoformat(created_at_str).astimezone(IST).strftime("%d %b %Y")
    except Exception:
        return created_at_str[:10] if created_at_str else "?"


# ── credential detail card ────────────────────────────────────────────────────

async def _build_detail_card(row: dict) -> str:
    """Build the full detail card for a DB, fetching live Railway balance."""
    db_type = row.get("db_type", "postgresql")
    cfg     = DB_TYPES.get(db_type, DB_TYPES["postgresql"])
    label   = cfg["label"]
    pid     = _short_project_id(row.get("project_url", ""))
    created = row.get("created_at", "")

    try:
        date_fmt = datetime.fromisoformat(created).astimezone(IST).strftime("%d %b %Y %H:%M IST")
    except Exception:
        date_fmt = created[:16] if created else "?"

    days = _days_since(created)

    # Fetch real Railway balance (blocking HTTP call → run in executor)
    railway_token = row.get("railway_token", "")
    loop = asyncio.get_event_loop()
    balance_info  = await loop.run_in_executor(
        None, fetch_railway_balance, railway_token
    )
    bal_line, trial_line = format_balance_lines(balance_info, created)

    lines = [
        f"📦 <b>{label} Database</b>  <code>[{pid}]</code>",
        f"<i>Created: {date_fmt}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"📧 <b>Account:</b>  <code>{row.get('email', 'N/A')}</code>",
        "",
        f"🔗 <b>Public URL:</b>",
        f"<code>{row.get('db_url') or 'N/A'}</code>",
        "",
        f"🔒 <b>Private URL:</b>",
        f"<code>{row.get('private_url') or 'N/A'}</code>",
        "",
        f"🌐 <b>Host:</b>      <code>{row.get('host', '')}</code>",
        f"🔌 <b>Port:</b>      <code>{row.get('port', '')}</code>",
        *(
            [f"👤 <b>User:</b>      <code>{row['username_val']}</code>"]
            if row.get("username_val") else []
        ),
        f"🔑 <b>Password:</b>  <code>{row.get('password', '')}</code>",
        *(
            [f"🗄️ <b>DB Name:</b>   <code>{row['db_name']}</code>"]
            if row.get("db_name") else []
        ),
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>Age:</b> {days} day{'s' if days != 1 else ''} old",
        trial_line,
        bal_line,
        "",
        f"🔗 <a href=\"{row.get('project_url', '')}\">Open in Railway →</a>",
    ]
    return "\n".join(lines)


def _detail_keyboard(row: dict, show_back: bool = False) -> InlineKeyboardMarkup:
    db_id = row.get("id")
    rows = [
        [
            InlineKeyboardButton("📡 Ping Connection", callback_data="ping_recheck"),
            InlineKeyboardButton("✅ Verify URL",      callback_data="verify_another"),
        ],
        [InlineKeyboardButton("🔄 Get Another DB", callback_data="getdb_menu")],
        [InlineKeyboardButton("🗑️ Delete from my list", callback_data=f"mydb_delete_{db_id}")],
    ]
    if show_back:
        rows.append([InlineKeyboardButton("◀️ Back to List", callback_data="mydb_list")])
    return InlineKeyboardMarkup(rows)


# ── list view (with pagination) ───────────────────────────────────────────────

def _total_pages(total: int) -> int:
    return max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def _list_text(rows: list[dict], user_first_name: str, page: int, total_pages: int) -> str:
    total = len(rows)
    page_info = f"Page {page + 1}/{total_pages}" if total_pages > 1 else ""
    subtitle  = f"Total: {total} database{'s' if total != 1 else ''}"
    if page_info:
        subtitle += f"  •  {page_info}"
    lines = [
        f"🗄️ <b>{user_first_name}'s Databases</b>",
        f"<i>{subtitle}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "Tap a database to view credentials & details:",
    ]
    return "\n".join(lines)


def _list_keyboard(rows: list[dict], page: int = 0) -> InlineKeyboardMarkup:
    """
    Shows PAGE_SIZE entries for the given page.
    Adds Back / page-indicator / Next navigation row when total > PAGE_SIZE.
    """
    total       = len(rows)
    total_pages = _total_pages(total)
    start       = page * PAGE_SIZE
    page_rows   = rows[start : start + PAGE_SIZE]

    buttons = []
    for row in page_rows:
        db_type  = row.get("db_type", "postgresql")
        emoji    = _db_type_emoji(db_type)
        label    = DB_TYPES.get(db_type, {}).get("label", db_type).replace(emoji, "").strip()
        pid      = _short_project_id(row.get("project_url", ""))
        date     = _fmt_date(row.get("created_at", ""))
        days     = _days_since(row.get("created_at", ""))
        expired  = "⚠️ " if days >= 30 else ""
        btn_text = f"{emoji} {label} [{pid}] • {expired}{date}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"mydb_detail_{row['id']}")])

    # ── Pagination row (only when more than one page) ─────────────────────────
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Back", callback_data=f"mydb_page_{page - 1}"))
        # Centre button shows current page / total — tapping does nothing
        nav.append(InlineKeyboardButton(f"📄 {page + 1} / {total_pages}", callback_data="mydb_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"mydb_page_{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🚀 Get New DB", callback_data="getdb_menu")])
    return InlineKeyboardMarkup(buttons)


# ── command handlers ──────────────────────────────────────────────────────────

async def mydb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    rows = get_all_dbs(user.id)

    if not rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Get a Database", callback_data="getdb_menu")]])
        await update.message.reply_text(
            "📭 You don't have any databases yet. Use /getdb to create one!",
            reply_markup=kb,
        )
        return

    if len(rows) == 1:
        # Single DB → show loading → fetch live balance → edit in-place
        loading = await update.message.reply_text("⏳ Fetching live balance from Railway...")
        card = await _build_detail_card(rows[0])
        await loading.edit_text(
            card,
            parse_mode="HTML",
            reply_markup=_detail_keyboard(rows[0], show_back=False),
            disable_web_page_preview=True,
        )
        return

    # Multiple DBs → show list (page 0)
    tp = _total_pages(len(rows))
    await update.message.reply_text(
        _list_text(rows, user.first_name or user.username or "You", 0, tp),
        parse_mode="HTML",
        reply_markup=_list_keyboard(rows, page=0),
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    rows = get_history(user.id, limit=5)
    if not rows:
        await update.message.reply_text("📭 No database history found. Use /getdb to create one!")
        return

    text = "📜 <b>Your Last Databases</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, row in enumerate(rows, 1):
        db_type = row.get("db_type", "postgresql")
        cfg     = DB_TYPES.get(db_type, DB_TYPES["postgresql"])
        try:
            created = datetime.fromisoformat(row["created_at"]).astimezone(IST).strftime("%d %b %H:%M IST")
        except Exception:
            created = row.get("created_at", "")
        url = row.get("db_url") or row.get("private_url") or "N/A"
        pid = _short_project_id(row.get("project_url", ""))
        text += f"<b>{i}. {cfg['label']}</b> <code>[{pid}]</code> — {created}\n"
        text += f"<code>{url[:60]}{'...' if len(url) > 60 else ''}</code>\n\n"

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Get New DB", callback_data="getdb_menu")]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ── callback handlers ─────────────────────────────────────────────────────────

async def mydb_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mydb_detail_{id} callbacks — show full detail of a specific DB."""
    query = update.callback_query
    await query.answer()

    db_id = int(query.data.split("_")[-1])
    row   = get_db_by_id(db_id)

    if not row or row["user_id"] != update.effective_user.id:
        await query.edit_message_text("❌ Database not found or access denied.")
        return

    # Show loading state while fetching live Railway balance
    await query.edit_message_text("⏳ Fetching live balance from Railway...")

    card = await _build_detail_card(row)
    await query.edit_message_text(
        card,
        parse_mode="HTML",
        reply_markup=_detail_keyboard(row, show_back=True),
        disable_web_page_preview=True,
    )


async def mydb_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mydb_list callback — go back to the DB list (page 0)."""
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    rows = get_all_dbs(user.id)

    if not rows:
        await query.edit_message_text("📭 No databases found.")
        return

    tp = _total_pages(len(rows))
    await query.edit_message_text(
        _list_text(rows, user.first_name or user.username or "You", 0, tp),
        parse_mode="HTML",
        reply_markup=_list_keyboard(rows, page=0),
    )


async def mydb_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mydb_page_{n} callbacks — navigate to page n of the DB list."""
    query = update.callback_query
    await query.answer()

    try:
        page = int(query.data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0

    user = update.effective_user
    rows = get_all_dbs(user.id)

    if not rows:
        await query.edit_message_text("📭 No databases found.")
        return

    tp    = _total_pages(len(rows))
    page  = max(0, min(page, tp - 1))   # clamp to valid range
    name  = user.first_name or user.username or "You"

    await query.edit_message_text(
        _list_text(rows, name, page, tp),
        parse_mode="HTML",
        reply_markup=_list_keyboard(rows, page=page),
    )


async def mydb_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mydb_noop — page-indicator button tap, do nothing."""
    await update.callback_query.answer()


async def mydb_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mydb_delete_{id} — ask user to confirm deletion."""
    query = update.callback_query
    await query.answer()

    db_id = int(query.data.split("_")[-1])
    row   = get_db_by_id(db_id)

    if not row or row["user_id"] != update.effective_user.id:
        await query.edit_message_text("❌ Database not found or access denied.")
        return

    db_type = row.get("db_type", "postgresql")
    cfg     = DB_TYPES.get(db_type, DB_TYPES["postgresql"])
    pid     = _short_project_id(row.get("project_url", ""))

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, delete",   callback_data=f"mydb_confirm_delete_{db_id}"),
            InlineKeyboardButton("❌ Cancel",         callback_data=f"mydb_detail_{db_id}"),
        ],
    ])
    await query.edit_message_text(
        f"🗑️ <b>Delete this database from your list?</b>\n\n"
        f"{cfg.get('emoji', '🗄️')} <b>{cfg['label']}</b>  <code>[{pid}]</code>\n\n"
        f"<i>This only removes it from your bot list — the Railway project stays intact.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def mydb_confirm_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle mydb_confirm_delete_{id} — actually delete and return to list."""
    query = update.callback_query
    await query.answer()

    user  = update.effective_user
    db_id = int(query.data.split("_")[-1])

    deleted = delete_db_record(db_id, user.id)
    if not deleted:
        await query.edit_message_text("❌ Could not delete — record not found or access denied.")
        return

    # Return to updated list (or empty message)
    rows = get_all_dbs(user.id)
    if not rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Get a Database", callback_data="getdb_menu")]])
        await query.edit_message_text(
            "🗑️ Deleted. You have no more databases in your list.\n\nUse /getdb to create one!",
            reply_markup=kb,
        )
        return

    tp   = _total_pages(len(rows))
    name = user.first_name or user.username or "You"
    await query.edit_message_text(
        "✅ Deleted from your list.\n\n" + _list_text(rows, name, 0, tp),
        parse_mode="HTML",
        reply_markup=_list_keyboard(rows, page=0),
    )


async def history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rows = get_history(update.effective_user.id, limit=5)
    if not rows:
        await query.edit_message_text("No history yet.")
        return
    text = "📜 <b>Your Last Databases</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    for i, row in enumerate(rows, 1):
        db_type = row.get("db_type", "postgresql")
        cfg     = DB_TYPES.get(db_type, DB_TYPES["postgresql"])
        try:
            created = datetime.fromisoformat(row["created_at"]).astimezone(IST).strftime("%d %b %H:%M IST")
        except Exception:
            created = row.get("created_at", "")
        url = row.get("db_url") or row.get("private_url") or "N/A"
        pid = _short_project_id(row.get("project_url", ""))
        text += f"<b>{i}. {cfg['label']}</b> <code>[{pid}]</code> — {created}\n"
        text += f"<code>{url[:60]}{'...' if len(url) > 60 else ''}</code>\n\n"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Get New DB", callback_data="getdb_menu")]])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
