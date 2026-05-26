"""
Adaptive FloodWait handler.

Telegram returns FloodWait errors when you hit rate limits. Default behavior:
sleep, retry. Smarter behavior: track recent FloodWaits per chat and proactively
slow down BEFORE hitting another one.
"""
import asyncio
from collections import deque
from time import time as ts
from logger import LOGGER


class FloodWaitGuard:
    """
    Per-chat (or global) FloodWait tracker with exponential backoff suggestions.
    
    Tracks recent FloodWait events. If many recent → slow down preemptively.
    """

    def __init__(self, history_size: int = 10, window_seconds: int = 300):
        self.history_size = history_size
        self.window = window_seconds
        # chat_id -> deque of (timestamp, wait_seconds)
        self._events: dict = {}
        # chat_id -> current backoff multiplier
        self._backoff: dict = {}

    def record(self, chat_id, wait_seconds: int):
        """Call when a FloodWait error happens."""
        key = str(chat_id)
        if key not in self._events:
            self._events[key] = deque(maxlen=self.history_size)
        self._events[key].append((ts(), wait_seconds))
        # Increase backoff
        current = self._backoff.get(key, 1.0)
        self._backoff[key] = min(current * 1.5, 8.0)  # cap at 8x

        LOGGER(__name__).warning(
            f"FloodWait recorded for {chat_id}: {wait_seconds}s "
            f"(backoff now {self._backoff[key]:.1f}x)"
        )

    def suggest_delay(self, chat_id, base_delay: float) -> float:
        """
        Returns suggested delay before next operation on this chat.
        Combines base delay with adaptive backoff from recent FloodWaits.
        """
        key = str(chat_id)
        events = self._events.get(key)
        if not events:
            return base_delay

        now = ts()
        # Prune old events
        while events and now - events[0][0] > self.window:
            events.popleft()

        if not events:
            # No recent events — gradually recover backoff
            current = self._backoff.get(key, 1.0)
            self._backoff[key] = max(current * 0.8, 1.0)
            return base_delay

        # Apply backoff
        multiplier = self._backoff.get(key, 1.0)
        return base_delay * multiplier

    def reset(self, chat_id):
        """Manually reset backoff (e.g. after successful streak)."""
        key = str(chat_id)
        self._backoff[key] = 1.0


async def handle_floodwait(exc, chat_id, guard: FloodWaitGuard = None):
    """
    Standardized FloodWait handler: sleep the required time + a small buffer,
    record in guard so future operations slow down.
    """
    wait = int(getattr(exc, "value", 0) or 0)
    if guard and chat_id is not None:
        guard.record(chat_id, wait)
    # Add small buffer (10% or 1s, whichever larger) to be safe
    buffer = max(1, int(wait * 0.1))
    total = wait + buffer
    if total > 0:
        await asyncio.sleep(total)


# Global singleton — one guard shared across the bot
floodwait_guard = FloodWaitGuard()
