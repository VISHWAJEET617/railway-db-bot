import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from telegram.ext import ContextTypes

from tgbot.database import get_latest_db, upsert_user
from tgbot.config import DB_TYPES

IST = timezone(timedelta(hours=5, minutes=30))
TIMEOUT = 6


def _parse_url(url: str):
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port
        if not host:
            return None
        if not port:
            scheme_ports = {
                "postgresql": 5432, "postgres": 5432,
                "mysql": 3306, "mongodb": 27017, "redis": 6379,
            }
            port = scheme_ports.get(parsed.scheme, 5432)
        return host, int(port)
    except Exception:
        return None


async def _tcp_check(host: str, port: int, timeout: float = TIMEOUT):
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        latency = (loop.time() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, latency
    except Exception:
        latency = (loop.time() - t0) * 1000
        return False, latency


def _icon(ok: bool) -> str:
    return "🟢" if ok else "🔴"


async def do_ping(user: User, message: Message, edit_msg: Message = None):
    """
    Run ping check.
    - edit_msg: if provided (recheck), edit that message in-place instead of sending new ones.
    """
    row = get_latest_db(user.id)
    if not row:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Get a Database", callback_data="getdb_menu")]])
        if edit_msg:
            await edit_msg.edit_text("📭 No database found. Use /getdb to create one first!", reply_markup=kb)
        else:
            await message.reply_text("📭 No database found. Use /getdb to create one first!", reply_markup=kb)
        return

    db_type = row.get("db_type", "postgresql")
    cfg     = DB_TYPES.get(db_type, DB_TYPES["postgresql"])
    label   = cfg["label"]

    pub_url  = row.get("db_url") or ""
    priv_url = row.get("private_url") or ""
    host     = row.get("host") or ""
    port_str = row.get("port") or ""

    # Show "Checking…" — edit in-place for recheck, send new for fresh call
    if edit_msg:
        status_msg = edit_msg
        await status_msg.edit_text(
            f"🔍 Checking <b>{label}</b> connection liveness…",
            parse_mode="HTML",
        )
    else:
        status_msg = await message.reply_text(
            f"🔍 Checking <b>{label}</b> connection liveness…",
            parse_mode="HTML",
        )

    pub_parsed  = _parse_url(pub_url)
    priv_parsed = _parse_url(priv_url)

    pub_result  = None
    priv_result = None

    if pub_parsed:
        ok, ms = await _tcp_check(*pub_parsed)
        pub_result = (ok, ms, pub_parsed[0], pub_parsed[1])
    elif host and port_str:
        ok, ms = await _tcp_check(host, int(port_str))
        pub_result = (ok, ms, host, int(port_str))

    if priv_parsed:
        ok, ms = await _tcp_check(*priv_parsed)
        priv_result = (ok, ms, priv_parsed[0], priv_parsed[1])

    now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    lines = [
        f"📡 <b>{label} — Liveness Check</b>",
        f"<i>Checked at {now}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if pub_result:
        ok, ms, h, p = pub_result
        status = f"<b>UP</b> ({ms:.0f} ms)" if ok else "<b>DOWN / Unreachable</b>"
        short_url = pub_url[:70] + ("…" if len(pub_url) > 70 else "")
        lines += [
            f"🌐 <b>Public URL</b>",
            f"   {_icon(ok)} {status}",
            f"   Host: <code>{h}:{p}</code>",
            f"   <code>{short_url}</code>",
            "",
        ]
    else:
        lines += ["🌐 <b>Public URL</b>", "   ⚪ Not available", ""]

    if priv_result:
        ok, ms, h, p = priv_result
        detail = f"({ms:.0f} ms)" if ok else "(Railway-internal only)"
        status = f"<b>UP</b> {detail}" if ok else f"<b>DOWN</b> {detail}"
        short_url = priv_url[:70] + ("…" if len(priv_url) > 70 else "")
        lines += [
            f"🔒 <b>Private URL</b>",
            f"   {_icon(ok)} {status}",
            f"   Host: <code>{h}:{p}</code>",
            f"   <code>{short_url}</code>",
            "",
        ]
    else:
        lines += ["🔒 <b>Private URL</b>", "   ⚪ Not available", ""]

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "<i>ℹ️ Private URL is only accessible inside Railway's network.</i>",
    ]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Re-check", callback_data="ping_recheck"),
        InlineKeyboardButton("📋 My DB", callback_data="mydb_view"),
    ]])

    await status_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)
    await do_ping(user, update.message)
