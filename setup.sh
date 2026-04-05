#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Railway DB Bot — One-shot setup for VPS / local / any Linux host
#  Usage:  bash setup.sh
# ─────────────────────────────────────────────────────────────────
set -e

echo "=== Railway DB Bot Setup ==="
echo ""

# 1. Python check
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.10+ first."
    exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "[1/5] Python $PY_VER found"

# 2. Install Python dependencies
echo "[2/5] Installing Python packages..."
pip install -r requirements.txt -q
echo "      Done."

# 3. Install Playwright Chromium + all system dependencies
echo "[3/5] Installing Playwright Chromium browser + system dependencies..."
python3 -m playwright install chromium --with-deps
echo "      Done."

# 4. Create data directory
echo "[4/5] Creating data/ directory..."
mkdir -p data
echo "      Done."

# 5. .env setup
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[5/5] Created .env from .env.example"
    echo ""
    echo "  >>> IMPORTANT: Edit .env and set your TELEGRAM_BOT_TOKEN and ADMIN_ID <<<"
    echo ""
else
    echo "[5/5] .env already exists — skipping."
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To start the bot:"
echo "  python3 tgbot/bot.py"
echo ""
echo "Or use the run script:"
echo "  bash run.sh"
