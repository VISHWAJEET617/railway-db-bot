"""
Central config — works on Replit, VPS, local, and any Linux host.

Priority for CHROMIUM_PATH:
  1. CHROMIUM_PATH env var (manual override)
  2. System PATH — picks up nix-installed chromium on Replit (validated by --version)
  3. Playwright's own bundled Chromium (validated; skipped if missing shared libs)
  4. Common fixed paths (Debian/Ubuntu VPS)

Priority for DB_PATH:
  1. DB_PATH env var
  2. <project_root>/data/bot_data.db  (auto-created)
"""
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

# ── load .env if present (local / VPS) ────────────────────────────────────────
try:
    from dotenv import load_dotenv
    _env_file = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if os.path.exists(_env_file):
        load_dotenv(_env_file)
        log.info("[Config] Loaded .env from %s", os.path.abspath(_env_file))
except ImportError:
    pass

# ── core secrets ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID", "")   # optional Telegram channel/group ID

# ── rate limiting ─────────────────────────────────────────────────────────────
DAILY_LIMIT   = 3
COOLDOWN_SECS = int(os.environ.get("COOLDOWN_SECS", "300"))

# ── database path ─────────────────────────────────────────────────────────────
def _resolve_db_path() -> str:
    env_path = os.environ.get("DB_PATH", "")
    if env_path:
        os.makedirs(os.path.dirname(os.path.abspath(env_path)), exist_ok=True)
        return env_path
    # Default: <repo_root>/data/bot_data.db
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "bot_data.db")

DB_PATH = _resolve_db_path()

# ── Chromium auto-detection ───────────────────────────────────────────────────
def _chromium_works(path: str) -> bool:
    """Return True if the binary at *path* can run (not missing shared libs)."""
    try:
        r = subprocess.run(
            [path, "--version"],
            capture_output=True,
            timeout=4,
        )
        return r.returncode == 0
    except Exception:
        return False


def _find_chromium() -> str:
    # 1. Explicit env override
    env_path = os.environ.get("CHROMIUM_PATH", "")
    if env_path and os.path.isfile(env_path):
        log.info("[Config] Chromium from CHROMIUM_PATH env: %s", env_path)
        return env_path

    # 2. System PATH (nix-installed chromium on Replit lands here first and works)
    for name in ("chromium", "chromium-browser", "chromium-headless", "google-chrome", "google-chrome-stable"):
        p = shutil.which(name)
        if p and _chromium_works(p):
            log.info("[Config] Chromium from system PATH: %s", p)
            return p

    # 3. Playwright bundled Chromium — resolve exact revision from browsers.json (no glob needed)
    _home = os.path.expanduser("~")
    # playwright respects PLAYWRIGHT_BROWSERS_PATH; otherwise defaults to ~/.cache/ms-playwright
    # On Replit, the workspace dir is used instead of the home dir.
    _pw_browsers_env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    try:
        import importlib.util, json as _json
        _spec = importlib.util.find_spec("playwright")
        if _spec and _spec.origin:
            _pkg_dir = os.path.dirname(_spec.origin)  # .../playwright/
            _browsers_json = os.path.join(_pkg_dir, "driver", "package", "browsers.json")
            if os.path.isfile(_browsers_json):
                with open(_browsers_json) as _f:
                    _browsers_data = _json.load(_f)
                for _b in _browsers_data.get("browsers", []):
                    if _b.get("name") == "chromium":
                        _rev = _b.get("revision", "")
                        # Build list of candidate base dirs:
                        # PLAYWRIGHT_BROWSERS_PATH > workspace cache > home cache > Windows cache
                        _bases = []
                        if _pw_browsers_env:
                            _bases.append(_pw_browsers_env)
                        # Derive workspace root from the package install location
                        # .pythonlibs is typically at <workspace>/.pythonlibs/...
                        _pkg_parts = _pkg_dir.split(os.sep)
                        if ".pythonlibs" in _pkg_parts:
                            _ws = os.sep.join(_pkg_parts[: _pkg_parts.index(".pythonlibs")])
                            _bases.append(os.path.join(_ws, ".cache", "ms-playwright"))
                        _bases += [
                            os.path.join(_home, ".cache", "ms-playwright"),
                            os.path.join(_home, "AppData", "Local", "ms-playwright"),
                        ]
                        for _base in _bases:
                            for _sub, _exe in [
                                (f"chromium-{_rev}", os.path.join("chrome-linux64", "chrome")),
                                (f"chromium-{_rev}", os.path.join("chrome-linux",   "chrome")),
                                (f"chromium-{_rev}", os.path.join("chrome-win",     "chrome.exe")),
                            ]:
                                _p = os.path.join(_base, _sub, _exe)
                                if os.path.isfile(_p) and _chromium_works(_p):
                                    log.info("[Config] Chromium from Playwright cache (rev %s): %s", _rev, _p)
                                    return _p
                        break
    except Exception:
        pass

    # 4. Well-known fixed paths (Debian/Ubuntu VPS)
    for fixed in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/snap/bin/chromium",
    ):
        if os.path.isfile(fixed) and _chromium_works(fixed):
            log.info("[Config] Chromium at fixed path: %s", fixed)
            return fixed

    log.warning(
        "[Config] Chromium not found automatically. "
        "Set CHROMIUM_PATH env var or run: playwright install chromium"
    )
    return ""

CHROMIUM_PATH = _find_chromium()

# ── DB type definitions ───────────────────────────────────────────────────────
DB_TYPES = {
    "postgresql": {
        "label":        "🐘 PostgreSQL",
        "search":       "PostgreSQL",
        "emoji":        "🐘",
        "url_keys":     ["DATABASE_PUBLIC_URL", "DATABASE_URL"],
        "private_keys": ["DATABASE_URL"],
        "host_keys":    ["PGHOST"],
        "port_keys":    ["PGPORT"],
        "user_keys":    ["PGUSER", "POSTGRES_USER"],
        "pass_keys":    ["PGPASSWORD", "POSTGRES_PASSWORD"],
        "db_keys":      ["PGDATABASE", "POSTGRES_DB"],
    },
    "mysql": {
        "label":        "🐬 MySQL",
        "search":       "MySQL",
        "emoji":        "🐬",
        "url_keys":     ["MYSQL_PUBLIC_URL", "MYSQL_URL", "DATABASE_URL"],
        "private_keys": ["MYSQL_URL", "DATABASE_URL"],
        "host_keys":    ["MYSQLHOST", "MYSQL_HOST"],
        "port_keys":    ["MYSQLPORT", "MYSQL_PORT"],
        "user_keys":    ["MYSQLUSER", "MYSQL_USER"],
        "pass_keys":    ["MYSQLPASSWORD", "MYSQL_PASSWORD"],
        "db_keys":      ["MYSQLDATABASE", "MYSQL_DATABASE"],
    },
    "mongodb": {
        "label":        "🍃 MongoDB",
        "search":       "MongoDB",
        "emoji":        "🍃",
        # Railway often provisions the public TCP proxy a bit later.
        # When it does, it exposes MONGO_PUBLIC_URL.
        "url_keys":     ["MONGO_PUBLIC_URL", "MONGO_URL", "MONGODB_URL", "DATABASE_URL"],
        "private_keys": ["MONGO_URL", "MONGODB_URL"],
        "host_keys":    ["MONGOHOST", "MONGO_HOST"],
        "port_keys":    ["MONGOPORT", "MONGO_PORT"],
        "user_keys":    ["MONGOUSER", "MONGO_USER"],
        "pass_keys":    ["MONGOPASSWORD", "MONGO_PASSWORD"],
        "db_keys":      ["MONGODATABASE", "MONGO_DATABASE"],
    },
    "redis": {
        "label":        "⚡ Redis",
        "search":       "Redis",
        "emoji":        "⚡",
        "url_keys":     ["REDIS_PUBLIC_URL", "REDIS_URL"],
        "private_keys": ["REDIS_URL"],
        "host_keys":    ["REDISHOST", "REDIS_HOST"],
        "port_keys":    ["REDISPORT", "REDIS_PORT"],
        "user_keys":    [],
        "pass_keys":    ["REDISPASSWORD", "REDIS_PASSWORD"],
        "db_keys":      [],
    },
}
