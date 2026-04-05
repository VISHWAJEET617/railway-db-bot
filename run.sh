#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Railway DB Bot — Start script
#  Usage:  bash run.sh
# ─────────────────────────────────────────────────────────────────
set -e

# Load .env if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Ensure data dir exists
mkdir -p data

echo "Starting Railway DB Bot..."
exec python3 tgbot/bot.py
