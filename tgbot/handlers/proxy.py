"""
Proxy management for the Railway DB bot.
- /setproxy <proxy>   — store & verify a proxy (required for non-admins to get DBs)
- /checkproxy [proxy] — check any proxy in detail without storing it
"""
import asyncio
import logging
import time
import urllib.parse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from tgbot.database import save_user_proxy, get_user_proxy, upsert_user
from tgbot.config import ADMIN_ID

log = logging.getLogger(__name__)

HTTPBIN_URL   = "https://httpbin.org/get"
IPIFY_URL     = "https://api.ipify.org?format=json"
CHECK_TIMEOUT = 18  # seconds


# ── Proxy parsing ─────────────────────────────────────────────────────────────

def parse_proxy_str(proxy_str: str) -> dict | None:
    """
    Supports:
      ip:port
      ip:port:user:pass
      http://ip:port
      http://user:pass@ip:port
      socks5://ip:port
      socks5://user:pass@ip:port
    Returns dict {server, username, password} for Playwright / requests.
    """
    s = proxy_str.strip()

    if "://" in s:
        parsed = urllib.parse.urlparse(s)
        host   = parsed.hostname
        port   = parsed.port
        user   = parsed.username or ""
        passw  = parsed.password or ""
        scheme = parsed.scheme or "http"
        if not host or not port:
            return None
        return {
            "server":   f"{scheme}://{host}:{port}",
            "username": user,
            "password": passw,
            "scheme":   scheme,
        }

    # host:port[:user:pass]
    parts = s.split(":")
    if len(parts) == 2:
        host, port_str = parts
        try:
            int(port_str)
        except ValueError:
            return None
        return {
            "server":   f"http://{host}:{port_str}",
            "username": "",
            "password": "",
            "scheme":   "http",
        }
    if len(parts) == 4:
        host, port_str, user, passw = parts
        try:
            int(port_str)
        except ValueError:
            return None
        return {
            "server":   f"http://{host}:{port_str}",
            "username": user,
            "password": passw,
            "scheme":   "http",
        }
    return None


def _build_requests_proxies(parsed: dict) -> dict:
    server   = parsed["server"]
    username = parsed.get("username", "")
    password = parsed.get("password", "")
    if username:
        p = urllib.parse.urlparse(server)
        auth_url = f"{p.scheme}://{username}:{password}@{p.netloc}"
        return {"http": auth_url, "https": auth_url}
    return {"http": server, "https": server}


# ── Core proxy checker ────────────────────────────────────────────────────────

def check_proxy(proxy_str: str) -> dict:
    """
    Tests the proxy thoroughly.
    Returns:
        status        – "pass" | "fail"
        proxy_ip      – IP seen by remote server
        real_ip       – our IP without proxy
        latency_ms    – round-trip via proxy
        anonymity     – "elite" | "anonymous" | "transparent"
        anonymity_label – human-readable with icon
        headers_exposed – list of proxy-related headers found
        error         – error message (if failed)
    """
    parsed = parse_proxy_str(proxy_str)
    if not parsed:
        return {
            "status": "fail",
            "error":  "Invalid proxy format. Use ip:port, ip:port:user:pass, or protocol://[user:pass@]host:port",
        }

    req_proxies = _build_requests_proxies(parsed)

    # Get our real IP (no proxy)
    real_ip = ""
    try:
        r = requests.get(IPIFY_URL, timeout=6)
        real_ip = r.json().get("ip", "")
    except Exception:
        pass

    start = time.time()
    try:
        resp = requests.get(
            HTTPBIN_URL,
            proxies=req_proxies,
            timeout=CHECK_TIMEOUT,
        )
        latency_ms = int((time.time() - start) * 1000)

        if resp.status_code != 200:
            return {
                "status":     "fail",
                "error":      f"Remote returned HTTP {resp.status_code}",
                "latency_ms": latency_ms,
            }

        data    = resp.json()
        origin  = data.get("origin", "")
        headers = data.get("headers", {})

        proxy_ip = origin.split(",")[0].strip()

        # Detect anonymity level
        proxy_header_names = {
            "X-Forwarded-For", "X-Forwarded", "X-Forwarded-Proto",
            "Forwarded", "Via", "Proxy-Connection", "Proxy-Authorization",
            "X-Proxy-Id", "X-Real-Ip",
        }
        found_proxy_headers = [h for h in headers if h in proxy_header_names]
        real_ip_leaked = bool(real_ip and real_ip in origin)

        if real_ip_leaked:
            anonymity       = "transparent"
            anonymity_label = "🔴 Transparent (Low) — real IP visible!"
        elif found_proxy_headers:
            anonymity       = "anonymous"
            anonymity_label = "🟡 Anonymous (Medium) — proxy detected, IP hidden"
        else:
            anonymity       = "elite"
            anonymity_label = "🟢 Elite (High) — no proxy headers, fully hidden"

        return {
            "status":           "pass",
            "proxy_ip":         proxy_ip,
            "real_ip":          real_ip,
            "latency_ms":       latency_ms,
            "anonymity":        anonymity,
            "anonymity_label":  anonymity_label,
            "headers_exposed":  found_proxy_headers,
            "error":            None,
        }

    except requests.exceptions.ProxyError as e:
        return {
            "status":     "fail",
            "error":      f"Proxy connection refused or auth failed",
            "latency_ms": int((time.time() - start) * 1000),
        }
    except requests.exceptions.ConnectTimeout:
        return {
            "status":     "fail",
            "error":      f"Connection timed out after {CHECK_TIMEOUT}s",
            "latency_ms": int((time.time() - start) * 1000),
        }
    except requests.exceptions.SSLError as e:
        return {
            "status":     "fail",
            "error":      f"SSL error: {str(e)[:60]}",
            "latency_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        return {
            "status":     "fail",
            "error":      str(e)[:80],
            "latency_ms": int((time.time() - start) * 1000),
        }


# ── Report formatter ──────────────────────────────────────────────────────────

def _format_report(proxy_str: str, result: dict, stored: bool = False) -> str:
    status     = result.get("status", "fail")
    latency    = result.get("latency_ms", 0)
    error      = result.get("error", "")
    anon_label = result.get("anonymity_label", "")
    proxy_ip   = result.get("proxy_ip", "")
    real_ip    = result.get("real_ip", "")
    exposed    = result.get("headers_exposed", [])

    # Shorten proxy display
    short_proxy = proxy_str if len(proxy_str) <= 40 else proxy_str[:37] + "..."

    if status == "pass":
        latency_icon = "🟢" if latency < 800 else ("🟡" if latency < 2000 else "🔴")
        lines = [
            "🛡️ <b>Proxy Check Result</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"✅ <b>Status:</b>     PASS",
            f"📡 <b>Proxy:</b>      <code>{short_proxy}</code>",
            f"🌐 <b>Proxy IP:</b>   <code>{proxy_ip}</code>",
            f"🔍 <b>Your IP:</b>    <code>{real_ip or 'N/A'}</code>",
            f"{latency_icon} <b>Latency:</b>   {latency} ms",
            "",
            f"🔒 <b>Anonymity:</b>  {anon_label}",
        ]
        if exposed:
            lines.append(f"⚠️ <b>Headers seen:</b> <code>{', '.join(exposed)}</code>")
        if stored:
            lines += ["", "💾 <i>Proxy saved. You can now use /getdb!</i>"]
    else:
        lines = [
            "🛡️ <b>Proxy Check Result</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"❌ <b>Status:</b>     FAIL",
            f"📡 <b>Proxy:</b>      <code>{short_proxy}</code>",
            f"⏱️ <b>Time:</b>       {latency} ms",
            f"⚠️ <b>Reason:</b>     {error}",
            "",
            "Make sure the proxy is active and the format is correct.",
            "Supported: <code>ip:port</code>  <code>ip:port:user:pass</code>",
            "           <code>http://host:port</code>  <code>socks5://user:pass@host:port</code>",
        ]

    return "\n".join(lines)


# ── Command handlers ──────────────────────────────────────────────────────────

USAGE_SETPROXY = (
    "⚙️ <b>Usage:</b> <code>/setproxy ip:port</code>\n"
    "or <code>/setproxy ip:port:user:pass</code>\n"
    "or <code>/setproxy protocol://user:pass@host:port</code>\n\n"
    "<i>Example:</i> <code>/setproxy 123.45.67.89:8080</code>"
)


async def setproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    args = context.args
    if not args:
        await update.message.reply_text(USAGE_SETPROXY, parse_mode="HTML")
        return

    proxy_str = args[0].strip()
    if not parse_proxy_str(proxy_str):
        await update.message.reply_text(
            f"❌ Invalid proxy format.\n\n{USAGE_SETPROXY}", parse_mode="HTML"
        )
        return

    checking = await update.message.reply_text(
        f"🔍 Checking proxy <code>{proxy_str[:50]}</code>...", parse_mode="HTML"
    )

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, check_proxy, proxy_str)

    save_user_proxy(user.id, proxy_str, result)

    report = _format_report(proxy_str, result, stored=True)

    kb = None
    if result["status"] == "pass":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Get Database Now", callback_data="getdb_menu"),
        ]])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Try Another Proxy", callback_data="setproxy_help"),
        ]])

    await checking.edit_text(report, parse_mode="HTML", reply_markup=kb)


async def checkproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    args = context.args

    if args:
        # Check the given proxy without storing
        proxy_str = args[0].strip()
        if not parse_proxy_str(proxy_str):
            await update.message.reply_text(
                f"❌ Invalid proxy format.\n\n{USAGE_SETPROXY}", parse_mode="HTML"
            )
            return

        checking = await update.message.reply_text(
            f"🔍 Checking proxy <code>{proxy_str[:50]}</code>...", parse_mode="HTML"
        )
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, check_proxy, proxy_str)
        report = _format_report(proxy_str, result, stored=False)

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "💾 Save & Use This Proxy",
                callback_data=f"saveproxy_{proxy_str[:64]}",
            ) if result["status"] == "pass" else
            InlineKeyboardButton("🔄 Check Another", callback_data="setproxy_help"),
        ]])
        await checking.edit_text(report, parse_mode="HTML", reply_markup=kb)

    else:
        # Check the stored proxy
        row = get_user_proxy(user.id)
        if not row:
            await update.message.reply_text(
                "📭 You don't have a proxy saved.\n\n"
                "Use <code>/setproxy ip:port</code> to save one.",
                parse_mode="HTML",
            )
            return

        proxy_str = row["proxy_str"]
        checking  = await update.message.reply_text(
            f"🔍 Re-checking your stored proxy <code>{proxy_str[:50]}</code>...",
            parse_mode="HTML",
        )
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, check_proxy, proxy_str)

        save_user_proxy(user.id, proxy_str, result)

        report = _format_report(proxy_str, result, stored=False)
        last_checked = row.get("last_checked", "")
        report += f"\n\n<i>Last stored check: {last_checked[:16] if last_checked else 'N/A'}</i>"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Check Again", callback_data="recheck_proxy"),
            InlineKeyboardButton("🚀 Get DB", callback_data="getdb_menu"),
        ]])
        await checking.edit_text(report, parse_mode="HTML", reply_markup=kb)


async def proxy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "setproxy_help":
        await query.edit_message_text(
            "⚙️ <b>How to set your proxy:</b>\n\n"
            "Send: <code>/setproxy ip:port</code>\n"
            "or: <code>/setproxy ip:port:user:pass</code>\n"
            "or: <code>/setproxy socks5://user:pass@ip:port</code>",
            parse_mode="HTML",
        )

    elif query.data == "recheck_proxy":
        user = update.effective_user
        row  = get_user_proxy(user.id)
        if not row:
            await query.edit_message_text("No proxy stored.")
            return
        proxy_str = row["proxy_str"]
        await query.edit_message_text(
            f"🔍 Re-checking <code>{proxy_str[:50]}</code>...", parse_mode="HTML"
        )
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, check_proxy, proxy_str)
        save_user_proxy(user.id, proxy_str, result)
        report = _format_report(proxy_str, result, stored=False)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Check Again", callback_data="recheck_proxy"),
            InlineKeyboardButton("🚀 Get DB",       callback_data="getdb_menu"),
        ]])
        await query.edit_message_text(report, parse_mode="HTML", reply_markup=kb)
