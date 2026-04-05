"""
Telegram Railway DB Bot — main entry point.
Run with: python3 tgbot/bot.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from tgbot.config import BOT_TOKEN, ADMIN_ID
from tgbot.database import init_db
from tgbot.queue_manager import queue_manager, MAX_WORKERS, _SYSTEM_INFO
from tgbot.log_channel import lc
from tgbot.handlers.start import start
from tgbot.handlers.getdb import (
    getdb_command,
    cancel_command,
    handle_callback,
    handle_proxy_input,
)
from tgbot.handlers.mydb import (
    mydb, history, history_callback,
    mydb_detail_callback, mydb_list_callback,
    mydb_page_callback, mydb_noop_callback,
    mydb_delete_callback, mydb_confirm_delete_callback,
)
from tgbot.handlers.admin import (
    admin, stats, users_list, ban, unban, broadcast, admin_callback,
)
from tgbot.handlers.help_cmd import help_cmd
from tgbot.handlers.ping import ping_command
from tgbot.handlers.verify import build_verify_handler, verify_start, verify_cancel
from tgbot.handlers.proxy import setproxy, checkproxy, myproxy, proxy_callback

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def _ensure_chromium():
    """Install playwright's Chromium if it isn't present in the cache yet.

    This runs once at startup and is a no-op when the binary already exists.
    It makes the bot self-healing after Replit workspace resets or fresh clones.
    """
    from tgbot.config import CHROMIUM_PATH
    if CHROMIUM_PATH:
        log.info("Chromium found at: %s", CHROMIUM_PATH)
        return
    log.info("Chromium not found — running 'playwright install chromium' …")
    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=300,
            check=True,
        )
        log.info("playwright install chromium completed (rc=%d)", result.returncode)
        # Re-detect now that the binary exists
        from tgbot import config as _cfg
        import importlib
        # Clear the cached value and re-run detection
        _cfg.CHROMIUM_PATH = _cfg._find_chromium()
        if _cfg.CHROMIUM_PATH:
            log.info("Chromium now available at: %s", _cfg.CHROMIUM_PATH)
        else:
            log.warning("Chromium still not found after install — browser launch may fail")
    except Exception as e:
        log.error("Failed to install playwright chromium: %s", e)


def main():
    if not BOT_TOKEN:
        log.critical("TELEGRAM_BOT_TOKEN environment variable is not set!")
        sys.exit(1)

    if ADMIN_ID == 0:
        log.warning("ADMIN_ID is not set — admin commands will be disabled")

    _ensure_chromium()

    init_db()
    log.info("Database initialised")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      help_cmd))
    app.add_handler(CommandHandler("getdb",     getdb_command))
    app.add_handler(CommandHandler("cancel",    cancel_command))
    app.add_handler(CommandHandler("mydb",      mydb))
    app.add_handler(CommandHandler("history",   history))
    app.add_handler(CommandHandler("admin",     admin))
    app.add_handler(CommandHandler("stats",     stats))
    app.add_handler(CommandHandler("users",     users_list))
    app.add_handler(CommandHandler("ban",       ban))
    app.add_handler(CommandHandler("unban",     unban))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ping",       ping_command))
    app.add_handler(CommandHandler("setproxy",   setproxy))
    app.add_handler(CommandHandler("checkproxy", checkproxy))
    app.add_handler(CommandHandler("myproxy",    myproxy))

    # Verify conversation (must be added before generic CallbackQueryHandler)
    app.add_handler(build_verify_handler())

    app.add_handler(CallbackQueryHandler(admin_callback,        pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(history_callback,      pattern="^show_history$"))
    app.add_handler(CallbackQueryHandler(mydb_detail_callback,          pattern="^mydb_detail_"))
    app.add_handler(CallbackQueryHandler(mydb_list_callback,            pattern="^mydb_list$"))
    app.add_handler(CallbackQueryHandler(mydb_page_callback,            pattern="^mydb_page_"))
    app.add_handler(CallbackQueryHandler(mydb_noop_callback,            pattern="^mydb_noop$"))
    app.add_handler(CallbackQueryHandler(mydb_delete_callback,          pattern="^mydb_delete_"))
    app.add_handler(CallbackQueryHandler(mydb_confirm_delete_callback,  pattern="^mydb_confirm_delete_"))
    app.add_handler(CallbackQueryHandler(proxy_callback,        pattern="^setproxy_help$|^recheck_proxy$|^myproxy_recheck$|^myproxy_delete$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_proxy_input))

    async def ping_cb(update, context):
        from tgbot.handlers.ping import do_ping
        q = update.callback_query
        await q.answer()
        if q.data == "ping_recheck":
            # Pass edit_msg so the existing message is updated in-place (no new message)
            await do_ping(update.effective_user, q.message, edit_msg=q.message)
        elif q.data == "mydb_view":
            update._effective_message = q.message
            await mydb(update, context)

    async def verify_cb(update, context):
        q = update.callback_query
        await q.answer()
        if q.data == "verify_another":
            # verify_start handles callback_query context natively
            await verify_start(update, context)
        elif q.data == "verify_cancel":
            await q.edit_message_text("❌ Verify cancelled.")

    app.add_handler(CallbackQueryHandler(ping_cb,   pattern="^ping_recheck$|^mydb_view$"))
    app.add_handler(CallbackQueryHandler(verify_cb, pattern="^verify_another$|^verify_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_callback))

    async def post_init(application):
        await application.bot.set_my_commands([
            BotCommand("start",   "Welcome message"),
            BotCommand("getdb",   "Get a free database"),
            BotCommand("cancel",  "Cancel your active/queued request"),
            BotCommand("mydb",    "View all your databases"),
            BotCommand("ping",       "Check liveness of your DB connections"),
            BotCommand("verify",     "Verify any Railway URL in real-time"),
            BotCommand("history",    "View last 5 databases"),
            BotCommand("setproxy",   "Set your proxy (required to get a DB)"),
            BotCommand("checkproxy", "Check your proxy anonymity and speed"),
            BotCommand("myproxy",    "View or manage your stored proxy"),
            BotCommand("help",       "Help and documentation"),
        ])
        log.info("Bot commands registered")
        # Start the smart CPU-aware queue loop
        queue_manager.set_bot(application.bot)
        queue_manager.start()
        # Log bot startup to channel
        import psutil
        cpu_now = psutil.cpu_percent(interval=0.5)
        avail_gb = psutil.virtual_memory().available / (1024 ** 3)
        cores    = psutil.cpu_count(logical=True) or 0
        import asyncio as _asyncio
        _asyncio.create_task(lc.bot_started(
            application.bot,
            max_workers=MAX_WORKERS,
            cpu_pct=cpu_now,
            cores=cores,
            ram_gb=avail_gb,
        ))

    app.post_init = post_init

    # ── global error handler → log to channel ─────────────────────────────────
    async def _error_handler(update, context):
        from telegram.error import Conflict, NetworkError
        err = str(context.error)
        # Suppress Conflict (two instances overlapping on restart) and transient
        # network blips — never worth spamming the log channel with these.
        if isinstance(context.error, (Conflict, NetworkError)):
            log.warning("Transient error (ignored): %s", err)
            return
        # Fallback string check in case the exception is raised as a non-standard type
        _lower = err.lower()
        if (
            "conflict" in _lower
            or "terminated by other getupdates" in _lower
            or "networkerror" in _lower
            or "timed out" in _lower
        ):
            log.warning("Transient error (ignored by string): %s", err)
            return
        log.error("Unhandled exception: %s", err, exc_info=context.error)
        import asyncio as _asyncio
        ctx = f"update={getattr(update, 'update_id', '?')}"
        _asyncio.create_task(lc.error(context.bot, ctx, err))

    app.add_error_handler(_error_handler)

    log.info("Bot starting — polling for updates")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
