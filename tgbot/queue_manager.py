"""
Smart async queue for Railway DB provisioning.

Design
------
• MAX_WORKERS = 6  (one slot per CPU core)
• A background loop samples CPU every POLL_SECS seconds.
• If CPU < CPU_LIMIT  AND  free slots exist  → promote next queued job.
• If CPU ≥ CPU_LIMIT → all pending jobs are held; their Telegram messages
  update to show a "CPU load high — held" notice.
• When a running job finishes it triggers an immediate promotion attempt
  so the queue drains without waiting for the next poll cycle.
• Queue position is preserved throughout holding periods.
• `force_clear()` handles bot-restart edge cases cleanly.
"""
import asyncio
import logging
import psutil
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

IST       = timezone(timedelta(hours=5, minutes=30))
CPU_LIMIT = 80    # % — don't start a new job above this
POLL_SECS = 3     # how often the background loop runs


def _auto_max_workers() -> tuple[int, dict]:
    """
    Detect system specs and derive a safe MAX_WORKERS.

    Rules
    -----
    • CPU-based  : logical_cores - 1  (keep 1 core free for the bot process)
    • RAM-based  : (available_GB - 2) / 0.5  (each Chromium ≈ 500 MB; leave 2 GB headroom)
    • Hard cap   : 10  (sanity ceiling)
    • Minimum    : 1

    Returns (max_workers, info_dict) so callers can log the breakdown.
    """
    try:
        cores       = psutil.cpu_count(logical=True) or 2
        avail_gb    = psutil.virtual_memory().available / (1024 ** 3)
        total_gb    = psutil.virtual_memory().total    / (1024 ** 3)
        cpu_workers = max(1, cores - 1)
        ram_workers = max(1, int((avail_gb - 2) / 0.5))
        result      = min(cpu_workers, ram_workers, 10)
        info = {
            "cores": cores, "avail_gb": avail_gb,
            "total_gb": total_gb, "cpu_workers": cpu_workers,
            "ram_workers": ram_workers, "max_workers": result,
        }
        log.info(
            "[Queue] System: %d logical cores, %.1f/%.1f GB RAM available → "
            "cpu_limit=%d  ram_limit=%d  MAX_WORKERS=%d",
            cores, avail_gb, total_gb, cpu_workers, ram_workers, result,
        )
        return result, info
    except Exception as e:
        log.warning("[Queue] Auto-detect failed (%s) → defaulting MAX_WORKERS=2", e)
        return 2, {"cores": "?", "avail_gb": "?", "total_gb": "?",
                   "cpu_workers": "?", "ram_workers": "?", "max_workers": 2}


MAX_WORKERS, _SYSTEM_INFO = _auto_max_workers()


class QueueManager:
    def __init__(self):
        self._active:  dict[int, asyncio.Event] = {}
        self._pending: list[dict] = []
        self._lock    = asyncio.Lock()
        self._bot     = None
        self._cpu_pct = 0.0          # latest CPU sample (updated by loop)
        self._monitor = None         # background asyncio.Task

    # ── startup ───────────────────────────────────────────────────────────────

    def set_bot(self, bot):
        self._bot = bot

    def start(self):
        """Launch the CPU-monitor + auto-promotion loop. Call once at bot startup."""
        if self._monitor is None or self._monitor.done():
            self._monitor = asyncio.create_task(self._loop())
            log.info("[Queue] Smart queue started — MAX_WORKERS=%d  CPU_LIMIT=%d%%",
                     MAX_WORKERS, CPU_LIMIT)

    # ── public API ────────────────────────────────────────────────────────────

    async def submit(
        self,
        user_id:      int,
        cancel_event: asyncio.Event,
        chat_id:      int,
        message_id:   int,
    ) -> "asyncio.Future | None":
        """
        Returns None   → slot available + CPU OK → job starts immediately.
        Returns Future → job is queued; Future resolves when it's their turn.
        """
        async with self._lock:
            if self.is_queued(user_id):
                raise ValueError("Already in queue")

            if len(self._active) < MAX_WORKERS and self._cpu_pct < CPU_LIMIT:
                self._active[user_id] = cancel_event
                log.info("[Queue] User %d started immediately (active=%d, cpu=%.0f%%)",
                         user_id, len(self._active), self._cpu_pct)
                return None

            loop = asyncio.get_event_loop()
            fut  = loop.create_future()
            self._pending.append({
                "user_id":      user_id,
                "cancel_event": cancel_event,
                "future":       fut,
                "chat_id":      chat_id,
                "message_id":   message_id,
            })
            reason = (f"cpu={self._cpu_pct:.0f}%>={CPU_LIMIT}%"
                      if self._cpu_pct >= CPU_LIMIT else
                      f"active={len(self._active)}>={MAX_WORKERS}")
            log.info("[Queue] User %d queued pos=%d (%s)",
                     user_id, len(self._pending), reason)
            return fut

    async def done(self, user_id: int):
        """Call when a job finishes (success or error). Triggers immediate promotion."""
        async with self._lock:
            self._active.pop(user_id, None)
            log.info("[Queue] User %d done — active=%d pending=%d",
                     user_id, len(self._active), len(self._pending))
        # Kick promotion immediately rather than waiting for the next poll
        asyncio.create_task(self._promote_now())

    async def cancel(self, user_id: int) -> bool:
        """Signal active job to stop, or drop from pending queue. Returns True if found."""
        async with self._lock:
            if user_id in self._active:
                self._active[user_id].set()
                return True
            for i, job in enumerate(self._pending):
                if job["user_id"] == user_id:
                    self._pending.pop(i)
                    if not job["future"].done():
                        job["future"].cancel()
                    await self._update_positions()
                    return True
        return False

    async def force_clear(self, user_id: int):
        """Unconditionally remove a user from all state (bot-restart recovery)."""
        async with self._lock:
            self._active.pop(user_id, None)
            self._pending = [j for j in self._pending if j["user_id"] != user_id]

    def is_queued(self, user_id: int) -> bool:
        return (user_id in self._active or
                any(j["user_id"] == user_id for j in self._pending))

    def queue_size(self) -> int:
        return len(self._pending)

    def active_count(self) -> int:
        return len(self._active)

    def current_cpu(self) -> float:
        return self._cpu_pct

    # ── background loop ───────────────────────────────────────────────────────

    async def _loop(self):
        """Continuously sample CPU and promote queued jobs when safe."""
        loop = asyncio.get_event_loop()
        while True:
            try:
                # 1-second blocking CPU sample — run in thread pool so we don't block the loop
                self._cpu_pct = await loop.run_in_executor(
                    None, psutil.cpu_percent, 1.0
                )
                await self._promote_now()
            except Exception as e:
                log.warning("[Queue] Loop error: %s", e)

            await asyncio.sleep(POLL_SECS)

    async def _promote_now(self):
        """Promote as many queued jobs as slots + CPU allow right now."""
        async with self._lock:
            promoted = 0
            while (self._pending
                   and len(self._active) < MAX_WORKERS
                   and self._cpu_pct < CPU_LIMIT):
                nxt = self._pending.pop(0)
                self._active[nxt["user_id"]] = nxt["cancel_event"]
                if not nxt["future"].done():
                    nxt["future"].set_result(True)
                promoted += 1

            if promoted:
                log.info("[Queue] Promoted %d job(s) — active=%d  cpu=%.0f%%",
                         promoted, len(self._active), self._cpu_pct)

            # Always refresh queue messages (position or held notice)
            if self._pending:
                await self._update_positions()

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _update_positions(self):
        """Edit pending users' Telegram messages with current position / CPU status."""
        if not self._bot:
            return
        now = datetime.now(IST).strftime("%H:%M:%S")
        cpu = self._cpu_pct
        held = cpu >= CPU_LIMIT

        for i, job in enumerate(self._pending):
            pos = i + 1
            if held:
                status_line = (
                    f"⚡ <b>System load high ({cpu:.0f}% CPU)</b> — your job is on hold.\n"
                    f"It will start automatically when load drops."
                )
            else:
                status_line = "Your turn is coming up soon!"

            try:
                await self._bot.edit_message_text(
                    chat_id=job["chat_id"],
                    message_id=job["message_id"],
                    text=(
                        f"⏳ <b>Queue position #{pos}</b>\n\n"
                        f"{status_line}\n\n"
                        f"<i>Updated: {now} IST</i>\n\n"
                        f"Use /cancel to leave the queue."
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass


queue_manager = QueueManager()
