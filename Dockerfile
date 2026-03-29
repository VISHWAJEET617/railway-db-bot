# ══════════════════════════════════════════════════════════════════════════════
#  Railway DB Bot — Production Dockerfile
#  Uses Microsoft's official Playwright Python image — ALL Chromium system
#  dependencies are pre-installed. No manual apt-get deps needed.
#
#  Pin the image version to match playwright in requirements.txt (1.58.0)
# ══════════════════════════════════════════════════════════════════════════════
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browsers are NOT in the base image — must install them
# Using --only-shell saves ~200MB (headless-only, which is all we need)
RUN playwright install --only-shell chromium

# Copy source code
COPY . .

# Create data directory for SQLite
RUN mkdir -p data

# Run as non-root user (pre-created in base image)
RUN chown -R pwuser:pwuser /app
USER pwuser

CMD ["python3", "tgbot/tgbot/bot.py"]
