"""tgbot.progress

Real-time progress tracker for database creation.
Renders a live message that edits itself periodically with IST timestamps.
"""

import asyncio
import time
from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

IST = timezone(timedelta(hours=5, minutes=30))

BASE_STEPS = [
    ("creating_email", "Creating temp email"),
    ("submitting_email", "Submitting email to Railway"),
    ("waiting_otp", "Waiting for OTP"),
    ("entering_otp", "Entering OTP"),
    ("accepting_tos", "Accepting Terms of Service"),
    ("creating_project", "Creating project"),
    ("provisioning_db", "Provisioning database"),
    ("extracting_creds", "Extracting credentials"),
]

MONGODB_EXTRA_STEPS = [
    ("deploying_mongo", "Deploying MongoDB container (can take 1–3 min)"),
    ("waiting_tcp_proxy", "Provisioning TCP proxy (Public URL)"),
]

_CANCEL_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_job")]]
)


class ProgressTracker:
    def __init__(self, db_type: str, db_label: str):
        self.db_type  = db_type
        self.db_label = db_label
        self.start_ts = time.time()

        self.steps = list(BASE_STEPS)
        if db_type == "mongodb":
            # Insert extra steps right after provisioning_db.
            out = []
            for sid, label in self.steps:
                out.append((sid, label))
                if sid == "provisioning_db":
                    out.extend(MONGODB_EXTRA_STEPS)
            self.steps = out

        self.states:     dict[str, str] = {}
        self.timestamps: dict[str, str] = {}
        self.meta:       dict[str, str] = {}
        self.done  = False
        self.error: str | None = None
        self._lock = asyncio.Lock()

    async def update(self, step_id: str, status: str = "current", meta: str | None = None):
        async with self._lock:
            self.states[step_id] = status
            if meta is not None:
                self.meta[step_id] = meta
            if step_id not in self.timestamps or status != "pending":
                self.timestamps[step_id] = datetime.now(IST).strftime("%H:%M:%S")

    def render(self) -> str:
        elapsed = int(time.time() - self.start_ts)
        mins    = elapsed // 60
        secs    = elapsed % 60
        now_ist = datetime.now(IST).strftime("%H:%M:%S")

        if self.error:
            cancelled = "Cancelled" in self.error or "cancelled" in self.error
            return "\n".join([
                f"{'🚫' if cancelled else '❌'} <b>{'Cancelled' if cancelled else 'Failed to create ' + self.db_label}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "",
                f"<b>{'Reason' if cancelled else 'Error'}:</b> {self.error}",
                "",
                "Use /getdb to start a new request.",
            ])

        if self.done:
            lines = [
                f"✅ <b>{self.db_label} is ready!</b>",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            ]
            for sid, label in self.steps:
                ts = self.timestamps.get(sid, "")
                meta = self.meta.get(sid)
                suffix = f"\n   <i>{meta}</i>" if meta else ""
                lines.append(f"✅ <code>{ts} IST</code>  {label}{suffix}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"⏱ Total time: {mins:02d}:{secs:02d}")
            return "\n".join(lines)

        lines = [
            f"🔄 <b>Creating your {self.db_label}...</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "",
        ]
        for sid, label in self.steps:
            state = self.states.get(sid, "pending")
            ts    = self.timestamps.get(sid, "")
            meta  = self.meta.get(sid)
            meta_line = f"\n   <i>{meta}</i>" if meta else ""
            if state == "done":
                lines.append(f"✅ <code>{ts} IST</code>  {label}{meta_line}")
            elif state == "current":
                lines.append(f"⏳ <code>{ts} IST</code>  <b>{label}...</b>{meta_line}")
            else:
                lines.append(f"⬜  <i>{label}</i>")

        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⏱ Elapsed: {mins:02d}:{secs:02d}  |  🔄 <code>{now_ist} IST</code>",
        ]
        return "\n".join(lines)


async def run_edit_loop(bot, chat_id: int, message_id: int, tracker: ProgressTracker):
    """
    Background task: edits the Telegram message periodically.
    Shows a Cancel button while the job is running.
    """
    last_text = ""
    while not tracker.done and not tracker.error:
        await asyncio.sleep(6)
        text = tracker.render()
        if text != last_text:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=_CANCEL_KB,
                )
                last_text = text
            except Exception:
                pass

    await asyncio.sleep(0.5)
    final_text = tracker.render()
    # Show "Get New DB" button on error/cancel, no button on success (cred card follows)
    final_kb = None
    if tracker.error:
        final_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚀 Get New DB", callback_data="getdb_menu")]]
        )
    if final_text != last_text or final_kb is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=final_text,
                parse_mode="HTML",
                reply_markup=final_kb,
            )
        except Exception:
            pass
