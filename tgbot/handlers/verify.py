import asyncio
import socket
import ssl
import urllib.parse
import time
import requests

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters

from tgbot.database import upsert_user

WAITING_URL = 1
TIMEOUT = 8

DB_SCHEMES = {"postgresql", "postgres", "mysql", "mongodb", "mongo", "redis", "rediss"}
SCHEME_PORTS = {
    "postgresql": 5432, "postgres": 5432,
    "mysql": 3306,
    "mongodb": 27017, "mongo": 27017,
    "redis": 6379, "rediss": 6380,
}


def _parse_any_url(raw: str):
    """Parse URL into components. Returns dict with type, host, port, scheme, etc."""
    raw = raw.strip()

    # Remove markdown code wrapping if any
    raw = raw.strip("`")

    parsed = urllib.parse.urlparse(raw)
    scheme = parsed.scheme.lower()

    if scheme in DB_SCHEMES:
        host = parsed.hostname
        port = parsed.port or SCHEME_PORTS.get(scheme, 5432)
        return {
            "type":     "db",
            "scheme":   scheme,
            "host":     host,
            "port":     int(port),
            "user":     parsed.username or "",
            "db_name":  parsed.path.lstrip("/") if parsed.path else "",
            "raw":      raw,
            "tls":      scheme == "rediss",
        }

    if scheme in ("http", "https"):
        host = parsed.hostname
        port = parsed.port or (443 if scheme == "https" else 80)
        return {
            "type":   "http",
            "scheme": scheme,
            "host":   host,
            "port":   int(port),
            "url":    raw,
            "raw":    raw,
            "tls":    scheme == "https",
        }

    # host:port bare format
    if ":" in raw and not raw.startswith("http"):
        parts = raw.rsplit(":", 1)
        try:
            return {
                "type":   "tcp",
                "scheme": "tcp",
                "host":   parts[0],
                "port":   int(parts[1]),
                "raw":    raw,
                "tls":    False,
            }
        except ValueError:
            pass

    return None


async def _resolve_host(host: str):
    """Resolve hostname to IP."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, socket.gethostbyname, host)
        return result
    except Exception:
        return None


async def _tcp_check(host: str, port: int, tls: bool = False):
    """TCP connect check. Returns (ok, latency_ms, error)."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    try:
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx),
                timeout=TIMEOUT,
            )
        else:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=TIMEOUT,
            )
        ms = (loop.time() - t0) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True, ms, None
    except asyncio.TimeoutError:
        ms = (loop.time() - t0) * 1000
        return False, ms, "Timeout"
    except Exception as e:
        ms = (loop.time() - t0) * 1000
        return False, ms, str(e)[:60]


async def _http_check(url: str):
    """HTTP GET check. Returns (status_code, latency_ms, redirect_url, error)."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    try:
        def _req():
            return requests.get(url, timeout=TIMEOUT, allow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 RailwayVerifyBot/1.0"},
                                verify=False)
        resp = await loop.run_in_executor(None, _req)
        ms = (loop.time() - t0) * 1000
        final_url = resp.url if resp.url != url else None
        return resp.status_code, ms, final_url, None
    except requests.exceptions.ConnectionError as e:
        ms = (loop.time() - t0) * 1000
        return None, ms, None, "Connection refused"
    except requests.exceptions.Timeout:
        ms = (loop.time() - t0) * 1000
        return None, ms, None, "Timeout"
    except Exception as e:
        ms = (loop.time() - t0) * 1000
        return None, ms, None, str(e)[:60]


def _http_status_icon(code):
    if code is None:
        return "🔴"
    if 200 <= code < 300:
        return "🟢"
    if 300 <= code < 400:
        return "🟡"
    return "🔴"


async def _build_report(info: dict) -> str:
    lines = ["🔍 <b>Railway URL Verify Report</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]

    host = info.get("host", "")
    port = info.get("port", 0)
    url_type = info["type"]
    scheme = info["scheme"]
    tls = info.get("tls", False)

    # URL info
    lines.append(f"🔗 <b>URL:</b> <code>{info['raw'][:80]}{'…' if len(info['raw']) > 80 else ''}</code>")
    lines.append(f"📌 <b>Type:</b> {scheme.upper()} {'(TLS/SSL)' if tls else ''}")
    lines.append(f"🌐 <b>Host:</b> <code>{host}</code>")
    lines.append(f"🔌 <b>Port:</b> <code>{port}</code>")
    lines.append("")

    # DNS resolution
    ip = await _resolve_host(host)
    if ip:
        lines.append(f"✅ <b>DNS Resolved:</b> <code>{ip}</code>")
    else:
        lines.append(f"❌ <b>DNS Resolution:</b> Failed — host not found")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("<b>Result: UNREACHABLE</b> — DNS lookup failed")
        return "\n".join(lines)

    lines.append("")

    # TCP check
    tcp_ok, tcp_ms, tcp_err = await _tcp_check(host, port, tls)
    tcp_icon = "🟢" if tcp_ok else "🔴"
    if tcp_ok:
        lines.append(f"{tcp_icon} <b>TCP Connection:</b> <b>UP</b> ({tcp_ms:.0f} ms)")
    else:
        lines.append(f"{tcp_icon} <b>TCP Connection:</b> <b>DOWN</b>")
        if tcp_err:
            lines.append(f"   ↳ Error: <code>{tcp_err}</code>")

    # HTTP check (only for http/https)
    if url_type == "http":
        lines.append("")
        code, http_ms, redirect, err = await _http_check(info["url"])
        http_icon = _http_status_icon(code)
        if code:
            lines.append(f"{http_icon} <b>HTTP Status:</b> <code>{code}</code> ({http_ms:.0f} ms)")
            if redirect:
                lines.append(f"   ↳ Redirected to: <code>{str(redirect)[:60]}</code>")
        else:
            lines.append(f"🔴 <b>HTTP Check:</b> Failed")
            if err:
                lines.append(f"   ↳ Error: <code>{err}</code>")

    # DB-specific info
    if url_type == "db":
        lines.append("")
        if info.get("user"):
            lines.append(f"👤 <b>User:</b> <code>{info['user']}</code>")
        if info.get("db_name"):
            lines.append(f"🗄️ <b>Database:</b> <code>{info['db_name']}</code>")
        if not tcp_ok:
            lines.append("")
            lines.append("⚠️ <i>Private Railway URLs only work inside Railway's internal network. "
                         "If this is a private URL, it will always fail from outside.</i>")

    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Overall verdict
    if tcp_ok:
        if url_type == "http":
            code_val = locals().get("code")
            verdict = "✅ <b>REACHABLE & RESPONDING</b>" if code_val and code_val < 400 else "⚠️ <b>REACHABLE but returned error</b>"
        else:
            verdict = "✅ <b>REACHABLE — Port is open</b>"
    else:
        verdict = "❌ <b>NOT REACHABLE</b>"

    lines.append(verdict)
    return "\n".join(lines)


async def verify_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update.effective_user.id, update.effective_user.username, update.effective_user.first_name)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="verify_cancel")]])

    text = (
        "🔗 <b>URL Verify</b>\n\n"
        "Send me any Railway URL to check:\n\n"
        "• <code>postgresql://user:pass@host:port/db</code>\n"
        "• <code>redis://default:pass@host:port</code>\n"
        "• <code>https://yourapp.railway.app</code>\n"
        "• <code>host.railway.internal:5432</code>\n\n"
        "<i>I'll check DNS, TCP reachability, HTTP status & report back.</i>"
    )

    # Called from either /verify command OR "Check Another" inline button.
    # update.message is None for callback queries — use effective_message instead.
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)

    return WAITING_URL


async def verify_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()

    info = _parse_any_url(raw)
    if not info:
        await update.message.reply_text(
            "❓ Couldn't parse that URL. Please send a valid URL like:\n"
            "<code>postgresql://user:pass@host:port/db</code>\n"
            "<code>https://myapp.railway.app</code>\n"
            "<code>host:port</code>",
            parse_mode="HTML",
        )
        return WAITING_URL

    checking = await update.message.reply_text(
        f"⏳ Checking <code>{raw[:60]}{'…' if len(raw) > 60 else ''}</code>…",
        parse_mode="HTML",
    )

    report = await _build_report(info)

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔁 Check Another", callback_data="verify_another"),
        InlineKeyboardButton("📡 Ping My DB", callback_data="ping_recheck"),
    ]])

    await checking.edit_text(report, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    return ConversationHandler.END


async def verify_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Verify cancelled.")
    else:
        await update.message.reply_text("❌ Verify cancelled.")
    return ConversationHandler.END


def build_verify_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("verify", verify_start)],
        states={
            WAITING_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_check),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", verify_cancel),
        ],
        per_message=False,
        allow_reentry=True,
    )
