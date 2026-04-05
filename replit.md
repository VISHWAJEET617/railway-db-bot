# Railway DB Bot

A Telegram bot that automates provisioning of free databases (PostgreSQL, MySQL, MongoDB, Redis) on Railway.app using disposable emails and headless browser automation.

## Architecture

- **Language:** Python 3.12
- **Bot framework:** python-telegram-bot v22.7
- **Browser automation:** Playwright (Chromium)
- **Database:** SQLite (`data/bot_data.db`)
- **Queue:** CPU-aware async queue (`tgbot/queue_manager.py`)

## Project Structure

```
tgbot/
  bot.py              — entry point, handler registration
  config.py           — env vars, Chromium detection, DB type definitions
  database.py         — SQLite schema + CRUD
  queue_manager.py    — smart CPU-aware async job queue
  railway_adapter.py  — Playwright automation for Railway.app
  railway_api.py      — Railway GraphQL API helpers
  mail_providers.py   — disposable email rotation
  log_channel.py      — Telegram channel event logging
  progress.py         — live progress tracker (edits message in-place)
  handlers/
    start.py          — /start welcome
    getdb.py          — /getdb flow + inline proxy collection
    mydb.py           — /mydb list + detail + pagination
    admin.py          — /admin panel
    proxy.py          — /setproxy, /checkproxy, /myproxy
    ping.py           — /ping DB connection check
    verify.py         — /verify Railway URL
    help_cmd.py       — /help
```

## Secrets Required

| Secret | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ADMIN_ID` | Your Telegram user ID for admin access |
| `LOG_CHANNEL_ID` | (Optional) Telegram channel ID for activity logs |

## Key Features

- Smart queue with MAX_WORKERS based on available RAM/CPU
- Proxy system: only saves Anonymous/Elite proxies; transparent rejected
- Auto-health check before `/getdb` — dead proxies auto-deleted
- Inline proxy collection: if no proxy set, bot asks in-chat during `/getdb`
- `/myproxy` command to view, re-check, or delete stored proxy
- Per-failure-type proxy error messages

## Workflow

Run: `python3 tgbot/bot.py`
