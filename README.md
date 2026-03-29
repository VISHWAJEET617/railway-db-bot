# 🚂 Railway DB Bot

A fully automated Telegram bot that provisions **free PostgreSQL, MySQL, MongoDB and Redis** databases on Railway.app using disposable emails and headless Chromium browser automation — no manual steps, no credit card required from the user.

---

## 📋 Table of Contents

- [What It Does](#what-it-does)
- [Features](#features)
- [Prerequisites — Get These First](#prerequisites--get-these-first)
- [Environment Variables Reference](#environment-variables-reference)
- [🖥️ Run Locally (Windows / Mac / Linux)](#️-run-locally-windows--mac--linux)
- [🐧 VPS — Ubuntu / Debian (any provider)](#-vps--ubuntu--debian-any-provider)
- [🔁 Keep Alive — PM2 or systemd](#-keep-alive--pm2-or-systemd)
- [🐳 Docker & Docker Compose](#-docker--docker-compose)
- [🟣 Replit](#-replit)
- [🟦 Railway](#-deploy-on-railway)
- [🟠 Render](#-deploy-on-render)
- [🟪 Heroku](#-deploy-on-heroku)
- [🟡 Fly.io](#-deploy-on-flyio)
- [🟤 Koyeb](#-deploy-on-koyeb)
- [🔶 Oracle Cloud Free Tier (Best Free VPS)](#-oracle-cloud-free-tier-best-free-vps)
- [🔵 DigitalOcean](#-digitalocean)
- [🟢 Hostinger VPS](#-hostinger-vps)
- [⚪ Vultr](#-vultr)
- [🟠 Contabo VPS](#-contabo-vps)
- [🧩 Coolify (Self-Hosted PaaS)](#-coolify-self-hosted-paas)
- [📡 Log Channel Setup](#-log-channel-setup)
- [🤖 Bot Commands](#-bot-commands)
- [⚙️ How the Smart Queue Works](#️-how-the-smart-queue-works)
- [🛠️ Troubleshooting](#️-troubleshooting)
- [📁 Project Structure](#-project-structure)

---

## What It Does

User sends `/getdb` in Telegram → bot opens a real Chromium browser in the background → creates a disposable email account → signs up on Railway.app → deploys the requested database → extracts credentials → sends back a ready-to-use connection string. Fully automated, ~60–90 seconds.

---

## Features

- ✅ PostgreSQL, MySQL, MongoDB, Redis — all 4 types
- ✅ Real public connection URLs (TCP proxy for MongoDB)
- ✅ Smart queue — up to N parallel sessions based on your server's CPU & RAM (auto-detected)
- ✅ CPU load monitoring — auto-holds new jobs when server is busy (>80% CPU)
- ✅ Proxy support per user (mandatory — protects against Railway detection)
- ✅ Telegram log channel — real-time notifications for every event
- ✅ Admin panel — ban/unban, broadcast, stats
- ✅ Runs on any Linux host with Python 3.10+ and Chromium

---

## Prerequisites — Get These First

### 1. Telegram Bot Token

1. Open Telegram → search **@BotFather**
2. Send `/newbot` → choose name → choose username
3. Copy the token — e.g. `8792000918:AAEkvBDh...`

### 2. Your Telegram Admin ID

1. Message **@userinfobot** on Telegram
2. It replies with your numeric ID — e.g. `123456789`
3. This ID gets full admin access (no cooldowns, can ban/broadcast)

### 3. A Proxy (Required for all users)

Every user must configure a proxy before creating a database. This prevents Railway.app from detecting automated signups.

- Use any HTTP/HTTPS/SOCKS5 proxy
- Set with `/setproxy ip:port` or `/setproxy ip:port:user:pass`
- Admin can tap "⚡ Skip Proxy (Admin)" button to bypass

---

## Environment Variables Reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Token from @BotFather |
| `ADMIN_ID` | ✅ | — | Your numeric Telegram user ID |
| `LOG_CHANNEL_ID` | No | blank | Channel/group ID for live logs |
| `CHROMIUM_PATH` | No | auto-detect | Path to Chromium binary |
| `DB_PATH` | No | `data/bot_data.db` | Path for SQLite file |
| `COOLDOWN_SECS` | No | `300` | Seconds between /getdb per user |

Copy `.env.example` to `.env` and fill in the values.

---

## 🖥️ Run Locally (Windows / Mac / Linux)

### Requirements

- Python 3.10 or higher
- pip
- Git

### Steps

```bash
# 1. Clone the repo
git clone https://github.com/vishwajeetcoderr-dev/railway-db-bot.git
cd railway-db-bot

# 2. Run the one-shot setup script
#    (installs Python packages, Chromium, creates data/ and .env)
bash setup.sh

# 3. Fill in your credentials
nano .env          # Linux / Mac
notepad .env       # Windows

# 4. Start the bot
bash run.sh
```

Or without the script:

```bash
pip install -r requirements.txt
python3 -m playwright install chromium --with-deps
mkdir -p data
cp .env.example .env
# edit .env, then:
python3 tgbot/tgbot/bot.py
```

---

## 🐧 VPS — Ubuntu / Debian (any provider)

This works on **DigitalOcean, Vultr, Contabo, Hostinger, Hetzner, Oracle Cloud, AWS EC2, Google Cloud VM** — any Ubuntu/Debian machine with root access.

### Minimum specs

| Resource | Minimum | Recommended |
|---|---|---|
| RAM | 1 GB | 2 GB+ |
| CPU | 1 core | 2+ cores |
| OS | Ubuntu 20.04+ | Ubuntu 22.04 / 24.04 |

### Full Setup

```bash
# 1. Connect
ssh root@your-server-ip

# 2. System update
apt update && apt upgrade -y

# 3. Install Python + Git
apt install -y python3 python3-pip git

# Verify Python 3.10+
python3 --version

# 4. Clone the repo
cd /opt
git clone https://github.com/vishwajeetcoderr-dev/railway-db-bot.git
cd railway-db-bot

# 5. Run setup (installs Python packages + Chromium with all system deps)
bash setup.sh

# 6. Configure
nano .env
# Set TELEGRAM_BOT_TOKEN, ADMIN_ID, LOG_CHANNEL_ID

# 7. Start
bash run.sh
```

> **Tip:** If `setup.sh` fails on Chromium system deps, run manually:
> `python3 -m playwright install chromium --with-deps`
> This single command installs both the browser binary and all required OS libraries.

---

## 🔁 Keep Alive — PM2 or systemd

Running `bash run.sh` in a terminal stops when you close SSH. Use one of these to run 24/7:

### Option A — PM2 (recommended)

```bash
# Install Node.js (needed for PM2)
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install nodejs -y
npm install pm2 -g

# Start bot with PM2
cd /opt/railway-db-bot
pm2 start "python3 tgbot/tgbot/bot.py" --name railway-db-bot

# Auto-start on server reboot
pm2 startup
pm2 save
```

Useful PM2 commands:

```bash
pm2 logs railway-db-bot      # live logs
pm2 restart railway-db-bot   # restart
pm2 stop railway-db-bot      # stop
pm2 status                   # all processes
```

### Option B — systemd (no Node.js needed)

```bash
nano /etc/systemd/system/railway-db-bot.service
```

Paste:

```ini
[Unit]
Description=Railway DB Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/railway-db-bot
EnvironmentFile=/opt/railway-db-bot/.env
ExecStart=/usr/bin/python3 tgbot/tgbot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable railway-db-bot
systemctl start railway-db-bot

# Logs
journalctl -u railway-db-bot -f
```

---

## 🐳 Docker & Docker Compose

Uses Microsoft's official Playwright Python image — **all Chromium system dependencies are pre-installed**, nothing extra needed.

### Build and run with Docker

```bash
# Build
docker build -t railway-db-bot .

# Run
docker run -d \
  --name railway-db-bot \
  --restart unless-stopped \
  --ipc host \
  -e TELEGRAM_BOT_TOKEN=your_token \
  -e ADMIN_ID=123456789 \
  -e LOG_CHANNEL_ID=-1001234567890 \
  -v $(pwd)/data:/app/data \
  railway-db-bot
```

> `--ipc host` is important — without it Chromium can run out of shared memory and crash on low-RAM machines.

### With Docker Compose (easiest)

```bash
# Copy and fill .env
cp .env.example .env
nano .env

# Start
docker compose up -d

# Logs
docker compose logs -f

# Stop
docker compose down
```

The `docker-compose.yml` in the repo already has everything set — persistent data volume, correct ipc mode, auto-restart.

---

## 🟣 Replit

### Steps

1. Go to [replit.com](https://replit.com) → **Create Repl** → **Import from GitHub**
2. Paste your repo URL → Import
3. Open the **Secrets** tab (padlock icon, left sidebar) → add:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | your token |
| `ADMIN_ID` | your user ID |
| `LOG_CHANNEL_ID` | channel ID (optional) |

4. Press the **Run** button

Replit auto-installs packages on first run. The Chromium binary is pre-installed via the Nix environment in `replit.nix`.

> **Note:** Free Replit accounts sleep after inactivity. Use **Replit Deployments** (paid) or ping with UptimeRobot to keep it awake.

---

## 🟦 Deploy on Railway

```bash
# Push to GitHub first
git push origin main
```

1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub** → select repo
2. Railway auto-reads `railway.toml`
3. **Variables** tab → add:

```
TELEGRAM_BOT_TOKEN = your_token
ADMIN_ID           = 123456789
LOG_CHANNEL_ID     = -1001234567890
```

4. Click **Deploy**

**Free tier:** $5/month credit — enough for a long-running bot worker.

---

## 🟠 Deploy on Render

```bash
git push origin main
```

1. [render.com](https://render.com) → **New** → **Background Worker**
2. Connect GitHub repo — Render auto-reads `render.yaml`
3. **Environment** tab → add `TELEGRAM_BOT_TOKEN`, `ADMIN_ID`, `LOG_CHANNEL_ID`
4. Click **Create Background Worker**

`render.yaml` provisions a 1 GB persistent disk at `/var/data` for SQLite — data survives redeploys.

**Free tier:** Background workers are not free on Render. Starter plan starts at ~$7/month.

---

## 🟪 Deploy on Heroku

### Install Heroku CLI

```bash
# Mac
brew install heroku/brew/heroku

# Ubuntu
curl https://cli-assets.heroku.com/install.sh | sh
```

### Deploy

```bash
heroku login
heroku create your-app-name

# Add buildpacks (Python + Playwright system deps)
heroku buildpacks:set heroku/python -a your-app-name
heroku buildpacks:add https://github.com/mxschmitt/heroku-playwright-buildpack -a your-app-name

# Set env vars
heroku config:set TELEGRAM_BOT_TOKEN=your_token -a your-app-name
heroku config:set ADMIN_ID=123456789 -a your-app-name
heroku config:set LOG_CHANNEL_ID=-1001234567890 -a your-app-name
heroku config:set PLAYWRIGHT_BUILDPACK_BROWSERS=chromium -a your-app-name

# Deploy
git push heroku main

# Scale worker (Procfile defines the process)
heroku ps:scale worker=1 -a your-app-name
heroku ps:scale web=0 -a your-app-name

# Logs
heroku logs --tail -a your-app-name
```

**Cost:** No free dynos on Heroku anymore. Eco plan ~$5/month.

> Make sure buildpacks are in this order: `heroku/python` first, then the Playwright buildpack.

---

## 🟡 Deploy on Fly.io

Fly.io uses Docker. The `fly.toml` in the repo is already configured as a **background worker** (no web server port needed).

### Install flyctl

```bash
# Mac / Linux
curl -L https://fly.io/install.sh | sh

# Windows
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
```

### Deploy

```bash
fly auth login

# Create the app (use your own name)
fly launch --name railway-db-bot --no-deploy

# Set secrets
fly secrets set TELEGRAM_BOT_TOKEN=your_token
fly secrets set ADMIN_ID=123456789
fly secrets set LOG_CHANNEL_ID=-1001234567890

# Deploy
fly deploy

# Logs
fly logs
```

**Note:** Fly.io auto-reads `Dockerfile` for the build. The bot needs at least **512MB RAM** — update `fly.toml` if needed:

```toml
[[vm]]
  memory = "512mb"
  size = "shared-cpu-1x"
```

**Free tier:** Fly.io offers 3 free shared-cpu-1x VMs (256MB each). You'll need to upgrade memory to 512MB, which costs ~$1–2/month.

---

## 🟤 Deploy on Koyeb

> ⚠️ **Koyeb Free Tier is NOT suitable** — Free tier only has 512 MB RAM and 0.1 vCPU. Chromium alone needs ~500 MB, leaving nothing for the bot. Use a paid instance (Nano at ~$3/month has 512 MB — borderline) or the Micro instance (1 GB RAM) for comfortable operation.

Koyeb requires Docker. The `Dockerfile` in the repo uses Microsoft's official Playwright image.

### Steps

1. [koyeb.com](https://koyeb.com) → **Create App**
2. Select **Docker** → connect your GitHub repo
3. **Service type:** Worker (not Web Service)
4. Add environment variables:

```
TELEGRAM_BOT_TOKEN = your_token
ADMIN_ID           = 123456789
LOG_CHANNEL_ID     = -1001234567890
```

5. Under **Advanced** → set instance size to **Nano or larger** (512 MB+ RAM)
6. Click **Deploy**

**Important:** In Koyeb's service settings, set the `--ipc=host` equivalent — without it, Chromium may crash due to shared memory limits.

---

## 🔶 Oracle Cloud Free Tier (Best Free VPS)

**This is the best free hosting option available.** Oracle Cloud's Always Free Tier gives you a real VPS — completely free, forever, no credit card charge.

### Always Free specs (ARM Ampere A1)

| Resource | What you get |
|---|---|
| **CPU** | Up to 4 OCPUs (ARM64) |
| **RAM** | Up to 24 GB |
| **Storage** | Up to 200 GB boot volume |
| **Bandwidth** | 10 TB outbound / month |
| **Cost** | **$0 forever** |

With 4 CPUs and 24 GB RAM, this bot's auto-detect will set **MAX_WORKERS = 3** (CPU limit kicks in: 4−1 = 3).

### Setup

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (free forever account)
2. Create an instance:
   - **Shape:** VM.Standard.A1.Flex
   - **OCPUs:** 4 / **RAM:** 24 GB
   - **OS:** Ubuntu 24.04 (Canonical)
3. SSH in and follow the **VPS setup** steps above

> **ARM note:** Playwright supports ARM64 (aarch64) natively. `playwright install chromium --with-deps` works on Oracle's Ampere A1 ARM instances.

---

## 🔵 DigitalOcean

Standard Ubuntu VPS — use the **VPS setup** steps above.

**Recommended droplet:** Basic → 2 vCPU / 2 GB RAM → ~$18/month  
**Minimum droplet:** Basic → 1 vCPU / 1 GB RAM → ~$6/month

```bash
# After creating droplet, SSH in:
ssh root@your-droplet-ip

# Then follow the VPS Ubuntu setup steps
```

Persistent storage: DigitalOcean Block Storage volumes can be mounted at `/var/data` and set in `DB_PATH` for data persistence across droplet rebuilds.

---

## 🟢 Hostinger VPS

Hostinger VPS gives full root access on Ubuntu. Follow the **VPS setup** steps exactly.

**Key Hostinger step** — install Playwright with a single command (handles all system deps):

```bash
python3 -m playwright install chromium --with-deps
```

This is the most reliable method, confirmed for Ubuntu 22.04 and 24.04 on Hostinger.

**Keep alive:** Use **systemd** (no extra install needed on Hostinger VPS):

```bash
nano /etc/systemd/system/railway-db-bot.service
# Paste the systemd config from the "Keep Alive" section
systemctl enable --now railway-db-bot
```

**Recommended plan:** KVM 2 (2 vCPU, 8 GB RAM) for comfortable multi-user operation.

---

## ⚪ Vultr

Full root Ubuntu VPS — follow the **VPS setup** steps exactly.

**Recommended:** Cloud Compute → Regular Performance → 2 vCPU / 2 GB RAM → ~$12/month  
**Minimum:** 1 vCPU / 1 GB RAM → ~$6/month

Vultr also offers **Bare Metal** servers if you need maximum performance.

---

## 🟠 Contabo VPS

Contabo gives significantly more RAM and storage per dollar compared to DigitalOcean/Vultr.

**Recommended:** Cloud VPS S → 4 vCPU / 8 GB RAM → ~$7/month

Follow the **VPS setup** steps above after SSH-ing in.

> Contabo uses standard Ubuntu — no special steps needed.

---

## 🧩 Coolify (Self-Hosted PaaS)

Coolify is an open-source alternative to Heroku/Render — you install it on your own VPS and get a web dashboard to deploy apps without touching Docker manually.

### Install Coolify on your VPS

```bash
# Run on a fresh Ubuntu 22.04+ VPS (2 vCPU / 2 GB RAM minimum)
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```

Access the dashboard at `http://your-server-ip:8000`.

### Deploy the bot via Coolify

1. **Resources** → **New Resource** → **Application**
2. **Source:** Connect your GitHub repo
3. **Build Pack:** Dockerfile (auto-detected)
4. **Type:** Background Worker (disable port/health check)
5. **Environment Variables:** Add `TELEGRAM_BOT_TOKEN`, `ADMIN_ID`, `LOG_CHANNEL_ID`
6. **Persistent Storage:** Mount `/app/data` to a volume for SQLite persistence
7. Click **Deploy**

Coolify handles Docker builds, restarts, and logs through the web UI.

---

## 📡 Log Channel Setup

The bot can send every event to a Telegram channel in real-time.

### Steps

1. Create a Telegram channel or group
2. Add your bot as **Admin** with "Post Messages" permission
3. Forward any message from the channel to **@userinfobot** — it shows the channel ID (starts with `-100`)
4. Set `LOG_CHANNEL_ID=-1001234567890` in your `.env` or hosting dashboard
5. Restart the bot — it sends a 🚀 startup message to confirm

### What gets logged

| Event | Notification |
|---|---|
| 🚀 Bot Started | Server specs, CPU, RAM, auto-detected MAX_WORKERS |
| 👤 New User | Name, @username, Telegram ID |
| ⏳ DB Started | Who requested, which DB type |
| ⏳ User Queued | Position, reason (slots full / CPU high) |
| ✅ DB Created | User, type, email, public URL, time taken, Railway link |
| ❌ DB Failed | User, type, error, time before failure |
| 🚫 DB Cancelled | User, type |
| 🔒 User Banned | Admin, target ID |
| 🔓 User Unbanned | Admin, target ID |
| 📢 Broadcast Sent | Admin, count, preview |
| 🆘 Error | Context, error string |

---

## 🤖 Bot Commands

### User commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/getdb` | Get a free database |
| `/newdb` | Force fresh Railway account |
| `/cancel` | Cancel active/queued request |
| `/mydb` | View all your databases |
| `/history` | View last 5 databases |
| `/ping` | Check DB connection liveness |
| `/verify <url>` | Verify any DB URL live |
| `/setproxy ip:port` | Set your proxy |
| `/setproxy ip:port:user:pass` | Set proxy with auth |
| `/checkproxy` | Check proxy speed and anonymity |
| `/help` | All commands |

### Admin commands

| Command | Description |
|---|---|
| `/admin` | Admin panel with stats |
| `/stats` | Live usage statistics |
| `/users` | List all users |
| `/ban <user_id>` | Ban a user |
| `/unban <user_id>` | Unban a user |
| `/broadcast <msg>` | Message all users (HTML supported) |

---

## ⚙️ How the Smart Queue Works

The bot detects your machine's specs at startup and automatically sets the maximum parallel sessions:

```
cpu_workers   = logical_cores - 1         (keep 1 core for the bot)
ram_workers   = (available_GB - 2) ÷ 0.5  (each session ≈ 500 MB)
MAX_WORKERS   = min(cpu_workers, ram_workers, 10)
```

### Examples

| Server | Cores | Free RAM | MAX_WORKERS |
|---|---|---|---|
| Oracle Cloud Free (ARM) | 4 | 24 GB | 3 |
| This Replit | 8 logical | 19 GB | 7 |
| Contabo S VPS | 4 | 6 GB | 3 |
| DigitalOcean 2GB | 2 | 1.5 GB | 1 |
| Heroku/Fly 512MB | 1 | 0.5 GB | 1 |

**CPU load gate:** Even with free slots, no new session starts when CPU ≥ 80%. Users in queue see a live status update and auto-resume when load drops.

---

## 🛠️ Troubleshooting

### "TELEGRAM_BOT_TOKEN not set"

```bash
cat .env | grep TOKEN
# If blank, edit .env and add your token
```

### "Chromium not found" / "Host system missing dependencies"

```bash
# This single command installs browser + all system libs
python3 -m playwright install chromium --with-deps
```

If on Docker/Koyeb and seeing this error — make sure you are using the `Dockerfile` from this repo (uses official Playwright base image). Never use Alpine as a base for Playwright.

### "proxy required" — can't get DB

This is intentional. Set a proxy first:

```
/setproxy 1.2.3.4:8080
/setproxy 1.2.3.4:8080:user:pass
```

### DB creation fails / times out

- Railway can be slow — MongoDB especially (3–5 minutes is normal)
- Check your proxy speed: `/checkproxy`
- Try `/newdb` to force a fresh account

### Chromium crashes silently (Docker)

Add `--ipc=host` to your Docker run command or `docker-compose.yml`. Without it, Chromium exhausts its shared memory on low-RAM containers.

### "No usable sandbox" error

Add these args to the Playwright launch call. Already set in the bot's `config.py`:
```
--no-sandbox --disable-setuid-sandbox
```

### Bot stops when SSH closes (VPS)

Use PM2 or systemd — see [Keep Alive](#-keep-alive--pm2-or-systemd) section.

### Conflict error in logs on restart

Normal — it means the previous bot instance and the new one briefly overlapped. Resolves automatically within 30 seconds. Already silenced from the log channel.

### Heroku — Chromium not found

Check buildpack order:
```bash
heroku buildpacks -a your-app-name
# Must be:
# 1. heroku/python
# 2. https://github.com/mxschmitt/heroku-playwright-buildpack
```

If wrong order, clear and re-add:
```bash
heroku buildpacks:clear -a your-app-name
heroku buildpacks:set heroku/python -a your-app-name
heroku buildpacks:add https://github.com/mxschmitt/heroku-playwright-buildpack -a your-app-name
git commit --allow-empty -m "fix buildpack order"
git push heroku main
```

---

## 📁 Project Structure

```
railway-db-bot/
├── tgbot/
│   └── tgbot/
│       ├── bot.py              # Main entry point
│       ├── config.py           # Env vars, Chromium auto-detect
│       ├── database.py         # SQLite operations
│       ├── queue_manager.py    # Smart CPU-aware queue (auto-scales)
│       ├── railway_adapter.py  # Playwright browser automation
│       ├── railway_api.py      # Railway GraphQL API client
│       ├── mail_providers.py   # Disposable email (mail.tm)
│       ├── log_channel.py      # Telegram log channel broadcaster
│       ├── progress.py         # Live progress tracking
│       └── handlers/
│           ├── getdb.py        # /getdb and /newdb
│           ├── mydb.py         # /mydb and /history
│           ├── admin.py        # Admin panel
│           ├── ping.py         # /ping
│           ├── verify.py       # /verify
│           ├── proxy.py        # /setproxy, /checkproxy
│           ├── start.py        # /start
│           └── help_cmd.py     # /help
├── data/                       # SQLite DB (auto-created)
├── requirements.txt            # Python dependencies
├── setup.sh                    # One-shot setup script
├── run.sh                      # Start script
├── Dockerfile                  # Production Docker image
├── docker-compose.yml          # Docker Compose config
├── fly.toml                    # Fly.io deployment
├── Procfile                    # Heroku worker
├── railway.toml                # Railway deployment
├── render.yaml                 # Render deployment
├── app.json                    # Heroku app manifest
├── .env.example                # Environment variables template
└── README.md                   # This file
```
