"""
Proxy management for the Railway DB bot.
- /setproxy <proxy>   — store & verify a proxy (only saved if pass + not transparent)
- /checkproxy [proxy] — check any proxy; auto-deletes stored proxy if dead
- /myproxy            — view, re-check, or delete stored proxy
"""
import asyncio
import logging
import time
import urllib.parse

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from tgbot.database import save_user_proxy, get_user_proxy, upsert_user, delete_user_proxy
from tgbot.config import ADMIN_ID

log = logging.getLogger(__name__)

HTTPBIN_URL   = "https://httpbin.org/get"
IPIFY_URL     = "https://api.ipify.org?format=json"
CHECK_TIMEOUT = 18


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
    Returns dict {server, username, password, scheme} for Playwright / requests.
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

    except requests.exceptions.ProxyError:
        return {
            "status":     "fail",
            "error":      "proxy_refused",
            "latency_ms": int((time.time() - start) * 1000),
        }
    except requests.exceptions.ConnectTimeout:
        return {
            "status":     "fail",
            "error":      "timeout",
            "latency_ms": int((time.time() - start) * 1000),
        }
    except requests.exceptions.SSLError as e:
        return {
            "status":     "fail",
            "error":      f"ssl:{str(e)[:60]}",
            "latency_ms": int((time.time() - start) * 1000),
        }
    except Exception as e:
        return {
            "status":     "fail",
            "error":      str(e)[:80],
            "latency_ms": int((time.time() - start) * 1000),
        }


# ── Specific error tips ───────────────────────────────────────────────────────

def _specific_error_tip(result: dict) -> str:
    """Return a specific, actionable error message based on the failure type."""
    err = result.get("error", "") or ""
    err_l = err.lower()

    if err in ("proxy_refused",) or "refused" in err_l or "proxy connection" in err_l:
        return (
            "🔌 <b>Reason:</b> Proxy is offline or rejecting connections.\n"
            "<i>The proxy server is not responding. Try a different proxy.</i>"
        )
    if err == "timeout" or "timed out" in err_l or "timeout" in err_l:
        return (
            f"⏱️ <b>Reason:</b> Connection timed out after {CHECK_TIMEOUT}s.\n"
            "<i>The proxy is too slow or unreachable. Try a faster one.</i>"
        )
    if err.startswith("ssl:") or "ssl" in err_l:
        return (
            "🔒 <b>Reason:</b> SSL handshake failed.\n"
            "<i>Try using an <code>http://</code> proxy instead of <code>https://</code>.</i>"
        )
    if "407" in err or "auth" in err_l or "username" in err_l or "password" in err_l:
        return (
            "🔑 <b>Reason:</b> Authentication failed.\n"
            "<i>Your username or password is wrong. Check the proxy credentials.</i>"
        )
    if "http 4" in err_l or "http 5" in err_l or "returned http" in err_l:
        return (
            f"🌐 <b>Reason:</b> {err}\n"
            "<i>The proxy returned an unexpected HTTP response.</i>"
        )
    if err:
        return f"⚠️ <b>Reason:</b> {err[:100]}"
    return "⚠️ <b>Reason:</b> Unknown error. Please try a different proxy."


# ── Report formatter ──────────────────────────────────────────────────────────

def _format_report(proxy_str: str, result: dict, stored: bool = False) -> str:
    status     = result.get("status", "fail")
    latency    = result.get("latency_ms", 0)
    anon_label = result.get("anonymity_label", "")
    proxy_ip   = result.get("proxy_ip", "")
    real_ip    = result.get("real_ip", "")
    exposed    = result.get("headers_exposed", [])

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
        tip = _specific_error_tip(result)
        lines = [
            "🛡️ <b>Proxy Check Result</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"❌ <b>Status:</b>     FAIL",
            f"📡 <b>Proxy:</b>      <code>{short_proxy}</code>",
            f"⏱️ <b>Time:</b>       {latency} ms",
            "",
            tip,
            "",
            "Supported formats:",
            "<code>ip:port</code>  <code>ip:port:user:pass</code>",
            "<code>http://host:port</code>  <code>socks5://user:pass@host:port</code>",
        ]

    return "\n".join(lines)


# ── Proxy Guide ───────────────────────────────────────────────────────────────

PROXY_GUIDE_TEXT = """
📡 <b>Proxy Guide — How to Get &amp; Set a Proxy</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔹 <b>Free Sources</b> <i>(no account needed)</i>
• <a href="https://free-proxy-list.net">free-proxy-list.net</a>
• <a href="https://spys.one">spys.one</a>
• <a href="https://proxyscrape.com/free-proxy-list">proxyscrape.com</a>
→ Copy <b>IP + Port</b> from the table
→ Format: <code>/setproxy ip:port</code>

🔹 <b>Paid Sources</b> <i>(better quality &amp; speed)</i>
• <a href="https://webshare.io">webshare.io</a> — gives <code>ip:port:user:pass</code>
• <a href="https://proxy-cheap.com">proxy-cheap.com</a> — gives SOCKS5 URL
• <a href="https://922proxy.com">922proxy.com</a> — gives <code>ip:port:user:pass</code>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 <b>All Supported Formats</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Basic</b> (free proxy sites):
<code>/setproxy 1.2.3.4:8080</code>

<b>With login</b> (paid proxies):
<code>/setproxy 1.2.3.4:8080:myuser:mypass</code>

<b>SOCKS5</b>:
<code>/setproxy socks5://user:pass@1.2.3.4:1080</code>

<b>HTTP URL</b>:
<code>/setproxy http://user:pass@1.2.3.4:8080</code>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ <b>Remember</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ <b>Elite &amp; Anonymous</b> → Saved &amp; accepted
🔴 <b>Transparent</b> → Rejected (leaks your real IP to Railway)

Use <code>/checkproxy ip:port</code> to test before saving.
""".strip()


# ── Command handlers ──────────────────────────────────────────────────────────

USAGE_SETPROXY = (
    "⚙️ <b>Usage:</b> <code>/setproxy ip:port</code>\n"
    "or <code>/setproxy ip:port:user:pass</code>\n"
    "or <code>/setproxy protocol://user:pass@host:port</code>\n\n"
    "<i>Example:</i> <code>/setproxy 123.45.67.89:8080</code>"
)

_KB_USAGE = InlineKeyboardMarkup([[
    InlineKeyboardButton("📖 Proxy Guide", callback_data="show_proxy_guide"),
]])

_KB_FAIL = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🔄 Try Another Proxy", callback_data="setproxy_help"),
        InlineKeyboardButton("📖 Proxy Guide",       callback_data="show_proxy_guide"),
    ],
])


async def proxy_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        PROXY_GUIDE_TEXT,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def setproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    args = context.args
    if not args:
        await update.message.reply_text(
            USAGE_SETPROXY, parse_mode="HTML", reply_markup=_KB_USAGE
        )
        return

    proxy_str = args[0].strip()
    if not parse_proxy_str(proxy_str):
        await update.message.reply_text(
            f"❌ Invalid proxy format.\n\n{USAGE_SETPROXY}",
            parse_mode="HTML",
            reply_markup=_KB_USAGE,
        )
        return

    checking = await update.message.reply_text(
        f"🔍 Checking proxy <code>{proxy_str[:50]}</code>...", parse_mode="HTML"
    )

    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, check_proxy, proxy_str)

    if result["status"] == "pass" and result.get("anonymity") != "transparent":
        save_user_proxy(user.id, proxy_str, result)
        report = _format_report(proxy_str, result, stored=True)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Get Database Now", callback_data="getdb_menu"),
        ]])

    elif result.get("anonymity") == "transparent":
        report = (
            "🔴 <b>Transparent Proxy Rejected</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📡 <b>Proxy:</b> <code>{proxy_str[:40]}</code>\n\n"
            "⚠️ This proxy leaks your real IP address.\n"
            "Railway would detect and flag the account.\n\n"
            "Please use an <b>Anonymous</b> or <b>Elite</b> proxy.\n\n"
            "<i>Proxy not saved.</i>"
        )
        kb = _KB_FAIL

    else:
        tip = _specific_error_tip(result)
        latency = result.get("latency_ms", 0)
        short_proxy = proxy_str if len(proxy_str) <= 40 else proxy_str[:37] + "..."
        report = (
            "🛡️ <b>Proxy Check Result</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"❌ <b>Status:</b> FAIL\n"
            f"📡 <b>Proxy:</b>  <code>{short_proxy}</code>\n"
            f"⏱️ <b>Time:</b>   {latency} ms\n\n"
            f"{tip}\n\n"
            "<i>Proxy not saved. Fix the issue and try again.</i>"
        )
        kb = _KB_FAIL

    await checking.edit_text(report, parse_mode="HTML", reply_markup=kb)


async def checkproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    args = context.args

    if args:
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

        if result["status"] == "pass" and result.get("anonymity") != "transparent":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💾 Save & Use This Proxy", callback_data=f"saveproxy_{proxy_str[:64]}"),
            ]])
        else:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Check Another", callback_data="setproxy_help"),
            ]])
        await checking.edit_text(report, parse_mode="HTML", reply_markup=kb)

    else:
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

        if result["status"] == "fail" or result.get("anonymity") == "transparent":
            delete_user_proxy(user.id)
            if result.get("anonymity") == "transparent":
                msg = (
                    "🔴 <b>Proxy is Transparent — removed.</b>\n\n"
                    "This proxy leaks your real IP. It has been deleted from your account.\n"
                    "Please set an Anonymous or Elite proxy with <code>/setproxy</code>."
                )
            else:
                tip = _specific_error_tip(result)
                msg = (
                    f"❌ <b>Stored proxy is dead — removed.</b>\n\n"
                    f"{tip}\n\n"
                    f"Set a new one with <code>/setproxy ip:port</code>."
                )
            await checking.edit_text(msg, parse_mode="HTML")
            return

        save_user_proxy(user.id, proxy_str, result)
        report = _format_report(proxy_str, result, stored=False)
        last_checked = row.get("last_checked", "")
        report += f"\n\n<i>Last stored check: {last_checked[:16] if last_checked else 'N/A'}</i>"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Check Again", callback_data="recheck_proxy"),
            InlineKeyboardButton("🚀 Get DB",      callback_data="getdb_menu"),
        ]])
        await checking.edit_text(report, parse_mode="HTML", reply_markup=kb)


async def myproxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    row = get_user_proxy(user.id)
    if not row:
        await update.message.reply_text(
            "📭 <b>No proxy stored.</b>\n\n"
            "Use <code>/setproxy ip:port</code> to save one,\n"
            "or use /getdb — the bot will ask you inline.",
            parse_mode="HTML",
        )
        return

    proxy_str    = row["proxy_str"]
    is_valid     = row.get("is_valid", 0)
    anonymity    = row.get("anonymity", "") or "unknown"
    latency      = row.get("latency_ms", 0)
    proxy_ip     = row.get("proxy_ip", "") or "N/A"
    last_checked = row.get("last_checked", "")

    status_icon  = "✅" if is_valid else "❌"
    anon_icon    = "🟢" if anonymity == "elite" else ("🟡" if anonymity == "anonymous" else "🔴")
    latency_icon = "🟢" if latency < 800 else ("🟡" if latency < 2000 else "🔴")
    short_proxy  = proxy_str if len(proxy_str) <= 40 else proxy_str[:37] + "..."
    last_chk     = last_checked[:16] if last_checked else "Never"

    text = (
        f"🛡️ <b>Your Stored Proxy</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 <b>Proxy:</b>        <code>{short_proxy}</code>\n"
        f"{status_icon} <b>Status:</b>       {'Valid ✓' if is_valid else 'Invalid ✗'}\n"
        f"🌐 <b>IP:</b>           <code>{proxy_ip}</code>\n"
        f"{anon_icon} <b>Anonymity:</b>   {anonymity.capitalize()}\n"
        f"{latency_icon} <b>Latency:</b>    {latency} ms\n"
        f"🕐 <b>Last Checked:</b> {last_chk}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Re-check", callback_data="myproxy_recheck"),
            InlineKeyboardButton("🗑️ Delete",   callback_data="myproxy_delete"),
        ],
        [InlineKeyboardButton("🚀 Get DB", callback_data="getdb_menu")],
    ])

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def proxy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user  = update.effective_user

    if query.data == "show_proxy_guide":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Back", callback_data="setproxy_help"),
        ]])
        await query.edit_message_text(
            PROXY_GUIDE_TEXT,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )

    elif query.data == "setproxy_help":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📖 Proxy Guide", callback_data="show_proxy_guide"),
        ]])
        await query.edit_message_text(
            "⚙️ <b>How to set your proxy:</b>\n\n"
            "Send: <code>/setproxy ip:port</code>\n"
            "or: <code>/setproxy ip:port:user:pass</code>\n"
            "or: <code>/setproxy socks5://user:pass@ip:port</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )

    elif query.data == "recheck_proxy":
        row = get_user_proxy(user.id)
        if not row:
            await query.edit_message_text("No proxy stored. Use /setproxy to add one.")
            return
        proxy_str = row["proxy_str"]
        await query.edit_message_text(
            f"🔍 Re-checking <code>{proxy_str[:50]}</code>...", parse_mode="HTML"
        )
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, check_proxy, proxy_str)

        if result["status"] == "fail" or result.get("anonymity") == "transparent":
            delete_user_proxy(user.id)
            if result.get("anonymity") == "transparent":
                msg = "🔴 Proxy is Transparent (removed). Use /setproxy to set a new one."
            else:
                tip = _specific_error_tip(result)
                msg = f"❌ Proxy is dead (removed).\n\n{tip}\n\nUse /setproxy to set a new one."
            await query.edit_message_text(msg, parse_mode="HTML")
        else:
            save_user_proxy(user.id, proxy_str, result)
            report = _format_report(proxy_str, result, stored=False)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Check Again", callback_data="recheck_proxy"),
                InlineKeyboardButton("🚀 Get DB",      callback_data="getdb_menu"),
            ]])
            await query.edit_message_text(report, parse_mode="HTML", reply_markup=kb)

    elif query.data == "myproxy_recheck":
        row = get_user_proxy(user.id)
        if not row:
            await query.edit_message_text("No proxy stored. Use /setproxy to add one.")
            return
        proxy_str = row["proxy_str"]
        await query.edit_message_text(
            f"🔍 Re-checking <code>{proxy_str[:50]}</code>...", parse_mode="HTML"
        )
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, check_proxy, proxy_str)

        if result["status"] == "fail" or result.get("anonymity") == "transparent":
            delete_user_proxy(user.id)
            if result.get("anonymity") == "transparent":
                msg = (
                    "🔴 <b>Proxy is Transparent — removed.</b>\n\n"
                    "This proxy leaks your real IP. Use /setproxy to set a new one."
                )
            else:
                tip = _specific_error_tip(result)
                msg = f"❌ <b>Proxy is dead — removed.</b>\n\n{tip}\n\nUse /setproxy to set a new one."
            await query.edit_message_text(msg, parse_mode="HTML")
        else:
            save_user_proxy(user.id, proxy_str, result)
            anon     = result.get("anonymity", "unknown")
            anon_icon = "🟢" if anon == "elite" else "🟡"
            latency  = result.get("latency_ms", 0)
            lat_icon = "🟢" if latency < 800 else ("🟡" if latency < 2000 else "🔴")
            proxy_ip = result.get("proxy_ip", "N/A")
            short_proxy = proxy_str if len(proxy_str) <= 40 else proxy_str[:37] + "..."
            text = (
                f"🛡️ <b>Your Stored Proxy</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📡 <b>Proxy:</b>        <code>{short_proxy}</code>\n"
                f"✅ <b>Status:</b>       Valid ✓\n"
                f"🌐 <b>IP:</b>           <code>{proxy_ip}</code>\n"
                f"{anon_icon} <b>Anonymity:</b>   {anon.capitalize()}\n"
                f"{lat_icon} <b>Latency:</b>    {latency} ms"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔄 Re-check", callback_data="myproxy_recheck"),
                    InlineKeyboardButton("🗑️ Delete",   callback_data="myproxy_delete"),
                ],
                [InlineKeyboardButton("🚀 Get DB", callback_data="getdb_menu")],
            ])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)

    elif query.data == "myproxy_delete":
        delete_user_proxy(user.id)
        await query.edit_message_text(
            "🗑️ <b>Proxy deleted.</b>\n\n"
            "Use <code>/setproxy ip:port</code> to set a new one,\n"
            "or use /getdb — the bot will ask you inline.",
            parse_mode="HTML",
        )
