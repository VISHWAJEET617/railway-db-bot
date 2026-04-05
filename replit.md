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
  database.py         — SQLite schema + CRUD (includes delete_user_proxy)
  queue_manager.py    — smart CPU-aware async job queue
  railway_adapter.py  — Playwright automation for Railway.app
  railway_api.py      — Railway GraphQL API helpers
  mail_providers.py   — disposable email rotation
  log_channel.py      — Telegram channel event logging
  progress.py         — live progress tracker (edits message in-place)
  handlers/
    start.py          — /start welcome
    getdb.py          — /getdb flow + inline proxy collection + admin skip
    mydb.py           — /mydb list + detail + pagination
    admin.py          — /admin panel, /stats, /users, /ban, /unban, /broadcast
    proxy.py          — /setproxy, /checkproxy, /myproxy, /proxy_guide
    ping.py           — /ping DB connection check
    verify.py         — /verify Railway URL (conversation handler)
    help_cmd.py       — /help
```

## Secrets Required

| Secret | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ADMIN_ID` | Your Telegram user ID for admin access |
| `LOG_CHANNEL_ID` | (Optional) Telegram channel ID for activity logs |

## Commands

### User
- `/start` — welcome
- `/getdb` — get a free database (inline proxy collection if none stored)
- `/cancel` — cancel active/queued request
- `/mydb` — paginated list of all databases
- `/history` — last 5 databases
- `/ping` — check DB connection liveness
- `/verify <url>` — verify any Railway DB URL live
- `/setproxy` — set proxy (ip:port / ip:port:user:pass / socks5://...)
- `/checkproxy` — check anonymity level and latency
- `/myproxy` — view, re-check, or delete stored proxy
- `/proxy_guide` — guide with free/paid sources and format examples
- `/help` — all commands

### Admin only
- `/admin` — admin panel
- `/stats` — live stats
- `/users` — user list
- `/ban` / `/unban` — ban management
- `/broadcast` — message all users

### Admin-only inline buttons (never shown to regular users)
- **⚡ Skip Proxy (Admin)** — appears on all proxy-required screens; bypasses proxy entirely

## Proxy System Rules

- Only **Anonymous** or **Elite** proxies are saved
- **Transparent** proxies are rejected immediately (not saved)
- **Dead** proxies are auto-deleted from DB on any health check failure
- Before every `/getdb` — stored proxy is silently re-checked
- If no proxy and user runs `/getdb` — bot collects proxy inline in-chat
- Per-failure-type error tips: refused / timeout / SSL / auth errors
- Admin can skip proxy from any point using the inline Skip button

## Workflow

Run: `python3 tgbot/bot.py`
