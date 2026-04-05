import sqlite3
import os
from datetime import datetime, timezone, timedelta

from tgbot.config import DB_PATH

IST = timezone(timedelta(hours=5, minutes=30))


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            is_banned   INTEGER DEFAULT 0,
            joined_at   TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS db_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            db_type         TEXT NOT NULL,
            email           TEXT,
            db_url          TEXT,
            private_url     TEXT,
            host            TEXT,
            port            TEXT,
            username_val    TEXT,
            password        TEXT,
            db_name         TEXT,
            project_url     TEXT,
            railway_token   TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS rate_limits (
            user_id         INTEGER PRIMARY KEY,
            last_request_at TEXT,
            requests_today  INTEGER DEFAULT 0,
            last_reset_date TEXT
        );

        CREATE TABLE IF NOT EXISTS user_proxies (
            user_id         INTEGER PRIMARY KEY,
            proxy_str       TEXT NOT NULL,
            is_valid        INTEGER DEFAULT 0,
            proxy_ip        TEXT DEFAULT '',
            anonymity       TEXT DEFAULT '',
            latency_ms      INTEGER DEFAULT 0,
            last_checked    TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS used_emails (
            email       TEXT PRIMARY KEY,
            provider    TEXT DEFAULT '',
            used_at     TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    # Migration: add railway_token column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE db_history ADD COLUMN railway_token TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Backfill used_emails from existing db_history so past addresses are never reused
    try:
        conn.execute("""
            INSERT OR IGNORE INTO used_emails (email, provider, used_at)
            SELECT LOWER(TRIM(email)), '', created_at
            FROM db_history
            WHERE email IS NOT NULL AND email != ''
        """)
        conn.commit()
    except Exception:
        pass

    conn.close()


def upsert_user(user_id: int, username: str, first_name: str) -> bool:
    """Insert or update a user. Returns True if the user is brand-new."""
    conn = get_conn()
    cur = conn.execute("""
        INSERT INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (user_id, username or "", first_name or ""))
    is_new = cur.rowcount == 1
    conn.commit()
    conn.close()
    return is_new


def is_banned(user_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row["is_banned"])


def ban_user(user_id: int):
    conn = get_conn()
    conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def unban_user(user_id: int):
    conn = get_conn()
    conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def check_rate_limit(user_id: int, daily_limit: int, cooldown_secs: int) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    reason is '' if allowed, else human-readable explanation.
    """
    conn = get_conn()
    row = conn.execute("SELECT * FROM rate_limits WHERE user_id=?", (user_id,)).fetchone()
    conn.close()

    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    if not row:
        return True, ""

    last_reset = row["last_reset_date"] or ""
    requests_today = row["requests_today"] or 0

    if last_reset != today_str:
        requests_today = 0

    if requests_today >= daily_limit:
        return False, f"Daily limit reached ({daily_limit} DBs/day). Resets at midnight IST."

    if row["last_request_at"]:
        try:
            last_dt = datetime.fromisoformat(row["last_request_at"])
            elapsed = (now_ist - last_dt).total_seconds()
            remaining = int(cooldown_secs - elapsed)
            if remaining > 0:
                mins = remaining // 60
                secs = remaining % 60
                return False, f"Cooldown active. Please wait {mins}m {secs}s."
        except:
            pass

    return True, ""


def record_request(user_id: int):
    conn = get_conn()
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")
    now_str   = now_ist.isoformat()

    existing = conn.execute("SELECT * FROM rate_limits WHERE user_id=?", (user_id,)).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO rate_limits (user_id, last_request_at, requests_today, last_reset_date)
            VALUES (?, ?, 1, ?)
        """, (user_id, now_str, today_str))
    else:
        requests_today = existing["requests_today"] or 0
        if existing["last_reset_date"] != today_str:
            requests_today = 0
        conn.execute("""
            UPDATE rate_limits SET
                last_request_at=?,
                requests_today=?,
                last_reset_date=?
            WHERE user_id=?
        """, (now_str, requests_today + 1, today_str, user_id))
    conn.commit()
    conn.close()


def save_db_record(user_id: int, db_type: str, creds: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO db_history
            (user_id, db_type, email, db_url, private_url, host, port, username_val, password, db_name, project_url, railway_token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        db_type,
        creds.get("email", ""),
        creds.get("db_url", ""),
        creds.get("private_url", ""),
        creds.get("host", ""),
        creds.get("port", ""),
        creds.get("username", ""),
        creds.get("password", ""),
        creds.get("db_name", ""),
        creds.get("project_url", ""),
        creds.get("railway_token", ""),
    ))
    conn.commit()
    conn.close()


def get_latest_db(user_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("""
        SELECT * FROM db_history WHERE user_id=?
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_db_by_id(db_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM db_history WHERE id=?", (db_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_db_record(db_id: int, user_id: int) -> bool:
    """Delete a db_history record by id, only if it belongs to user_id."""
    conn = get_conn()
    cur  = conn.execute(
        "DELETE FROM db_history WHERE id=? AND user_id=?", (db_id, user_id)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_all_dbs(user_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM db_history WHERE user_id=?
        ORDER BY created_at DESC
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_history(user_id: int, limit=5) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM db_history WHERE user_id=?
        ORDER BY created_at DESC LIMIT ?
    """, (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    total_users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_dbs     = conn.execute("SELECT COUNT(*) FROM db_history").fetchone()[0]
    today_str     = datetime.now(IST).strftime("%Y-%m-%d")
    dbs_today     = conn.execute(
        "SELECT COUNT(*) FROM db_history WHERE created_at LIKE ?", (f"{today_str}%",)
    ).fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
        "total_dbs":   total_dbs,
        "dbs_today":   dbs_today,
    }


def save_user_proxy(user_id: int, proxy_str: str, result: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO user_proxies (user_id, proxy_str, is_valid, proxy_ip, anonymity, latency_ms, last_checked)
        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(user_id) DO UPDATE SET
            proxy_str    = excluded.proxy_str,
            is_valid     = excluded.is_valid,
            proxy_ip     = excluded.proxy_ip,
            anonymity    = excluded.anonymity,
            latency_ms   = excluded.latency_ms,
            last_checked = excluded.last_checked
    """, (
        user_id,
        proxy_str,
        1 if result.get("status") == "pass" else 0,
        result.get("proxy_ip", ""),
        result.get("anonymity", ""),
        result.get("latency_ms", 0),
    ))
    conn.commit()
    conn.close()


def get_user_proxy(user_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_proxies WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_user_proxy(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM user_proxies WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def is_email_used(email: str) -> bool:
    """Return True if this email has already been submitted to Railway."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM used_emails WHERE email=? LIMIT 1", (email.lower().strip(),)
    ).fetchone()
    conn.close()
    return row is not None


def mark_email_used(email: str, provider: str = ""):
    """Record this email so it is never reused."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO used_emails (email, provider) VALUES (?, ?)",
            (email.lower().strip(), provider),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_all_user_ids() -> list[int]:
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_all_users(limit=50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.user_id, u.username, u.first_name, u.is_banned,
               COUNT(h.id) as db_count
        FROM users u
        LEFT JOIN db_history h ON h.user_id = u.user_id
        GROUP BY u.user_id
        ORDER BY db_count DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
