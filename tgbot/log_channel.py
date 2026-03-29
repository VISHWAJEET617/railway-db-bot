"""
Telegram log channel broadcaster.

Sends structured event notifications to LOG_CHANNEL_ID (if configured).
Every call is fire-and-forget — errors are silently swallowed so channel
issues never affect normal bot operation.

Usage
-----
    from tgbot.log_channel import lc
    await lc.send(bot, "✅ something happened")
    await lc.db_created(bot, user, db_type, creds, elapsed_secs)
    # etc.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_CHANNEL_ID: int | None = None

def _load():
    global _CHANNEL_ID
    raw = os.environ.get("LOG_CHANNEL_ID", "").strip()
    if raw:
        try:
            _CHANNEL_ID = int(raw)
            log.info("[LogChannel] Enabled → channel_id=%d", _CHANNEL_ID)
        except ValueError:
            log.warning("[LogChannel] LOG_CHANNEL_ID=%r is not a valid integer — disabled", raw)

_load()


def _now() -> str:
    return datetime.now(IST).strftime("%d %b %Y  %H:%M:%S IST")


def _user_tag(user) -> str:
    """Format a Telegram user object into a readable tag."""
    if user is None:
        return "Unknown"
    parts = []
    name = (user.first_name or "") + (" " + user.last_name if getattr(user, "last_name", None) else "")
    name = name.strip()
    if name:
        parts.append(name)
    if getattr(user, "username", None):
        parts.append(f"@{user.username}")
    parts.append(f"<code>{user.id}</code>")
    return " · ".join(parts)


def _user_tag_plain(user_id: int, username: str | None = None, first_name: str | None = None) -> str:
    parts = []
    if first_name:
        parts.append(first_name)
    if username:
        parts.append(f"@{username}")
    parts.append(f"<code>{user_id}</code>")
    return " · ".join(parts)


class _LogChannel:

    async def send(self, bot, text: str):
        """Send a raw HTML message to the log channel."""
        if not _CHANNEL_ID or not bot:
            return
        try:
            await bot.send_message(
                chat_id=_CHANNEL_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.debug("[LogChannel] send error: %s", e)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def bot_started(self, bot, max_workers: int, cpu_pct: float,
                          cores: int, ram_gb: float):
        await self.send(bot, (
            f"🚀 <b>Bot Started</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"🖥 CPU cores: <b>{cores}</b>  |  CPU: <b>{cpu_pct:.0f}%</b>\n"
            f"💾 RAM available: <b>{ram_gb:.1f} GB</b>\n"
            f"⚙️ MAX_WORKERS auto-set: <b>{max_workers}</b>"
        ))

    # ── user events ───────────────────────────────────────────────────────────

    async def new_user(self, bot, user):
        await self.send(bot, (
            f"👤 <b>New User</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👤 {_user_tag(user)}"
        ))

    # ── DB provisioning ───────────────────────────────────────────────────────

    async def db_started(self, bot, user, db_type: str, queue_pos: int | None = None):
        pos_line = f"\n📋 Queue position: <b>#{queue_pos}</b>" if queue_pos else ""
        await self.send(bot, (
            f"⏳ <b>DB Creation Started</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👤 {_user_tag(user)}\n"
            f"🗄 Type: <b>{db_type.upper()}</b>"
            f"{pos_line}"
        ))

    async def db_created(self, bot, user, db_type: str, creds: dict, elapsed: float):
        email      = creds.get("email", "?")
        proj_url   = creds.get("project_url", "")
        pub_url    = creds.get("db_url") or creds.get("private_url") or "N/A"
        mins, secs = divmod(int(elapsed), 60)
        await self.send(bot, (
            f"✅ <b>DB Created</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👤 {_user_tag(user)}\n"
            f"🗄 Type: <b>{db_type.upper()}</b>\n"
            f"📧 Email: <code>{email}</code>\n"
            f"🔗 URL: <code>{pub_url[:80]}</code>\n"
            f"⏱ Time: <b>{mins:02d}:{secs:02d}</b>\n"
            + (f"🌐 <a href=\"{proj_url}\">Railway Project</a>" if proj_url else "")
        ))

    async def db_failed(self, bot, user, db_type: str, error: str, elapsed: float):
        mins, secs = divmod(int(elapsed), 60)
        await self.send(bot, (
            f"❌ <b>DB Failed</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👤 {_user_tag(user)}\n"
            f"🗄 Type: <b>{db_type.upper()}</b>\n"
            f"⏱ After: <b>{mins:02d}:{secs:02d}</b>\n"
            f"⚠️ Error: <code>{error[:300]}</code>"
        ))

    async def db_cancelled(self, bot, user, db_type: str):
        await self.send(bot, (
            f"🚫 <b>DB Cancelled</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👤 {_user_tag(user)}\n"
            f"🗄 Type: <b>{db_type.upper()}</b>"
        ))

    # ── queue events ──────────────────────────────────────────────────────────

    async def queued(self, bot, user, db_type: str, position: int, reason: str):
        await self.send(bot, (
            f"⏳ <b>User Queued</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👤 {_user_tag(user)}\n"
            f"🗄 Type: <b>{db_type.upper()}</b>\n"
            f"📋 Position: <b>#{position}</b>\n"
            f"📌 Reason: {reason}"
        ))

    async def cpu_throttle(self, bot, cpu_pct: float, held: int):
        await self.send(bot, (
            f"⚡ <b>CPU Throttle — Jobs Held</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"🖥 CPU: <b>{cpu_pct:.0f}%</b>\n"
            f"⏸ Held jobs: <b>{held}</b>"
        ))

    # ── admin actions ─────────────────────────────────────────────────────────

    async def user_banned(self, bot, admin_user, target_id: int):
        await self.send(bot, (
            f"🔒 <b>User Banned</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👮 Admin: {_user_tag(admin_user)}\n"
            f"🚫 Target: <code>{target_id}</code>"
        ))

    async def user_unbanned(self, bot, admin_user, target_id: int):
        await self.send(bot, (
            f"🔓 <b>User Unbanned</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👮 Admin: {_user_tag(admin_user)}\n"
            f"✅ Target: <code>{target_id}</code>"
        ))

    async def broadcast_sent(self, bot, admin_user, count: int, text_preview: str):
        await self.send(bot, (
            f"📢 <b>Broadcast Sent</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"👮 Admin: {_user_tag(admin_user)}\n"
            f"📬 Sent to: <b>{count}</b> users\n"
            f"📝 Preview: <i>{text_preview[:100]}</i>"
        ))

    # ── errors ────────────────────────────────────────────────────────────────

    async def error(self, bot, context: str, err: str):
        await self.send(bot, (
            f"🆘 <b>Error</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {_now()}\n"
            f"📍 Context: <code>{context}</code>\n"
            f"⚠️ <code>{err[:500]}</code>"
        ))


lc = _LogChannel()
