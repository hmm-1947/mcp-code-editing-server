"""Request budget for OpenRouter's free tier, plus per-model cooldowns."""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Sliding one-minute window. `rpm` stays below the real limit (20) on purpose, so a
    burst from several parallel tasks cannot trip a 429 that cools everything down."""

    def __init__(self, rpm: int = 18):
        self.rpm = max(1, rpm)
        self._stamps: list[float] = []
        self._cond = threading.Condition()
        self._blocked_until = 0.0
        self._blocked_reason = ""
        self._cooldowns: dict[str, float] = {}

    def configure(self, rpm: int) -> None:
        with self._cond:
            self.rpm = max(1, rpm)

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._stamps and self._stamps[0] <= cutoff:
            self._stamps.pop(0)

    def wait_needed(self) -> float:
        """Seconds until a request may be sent (0 = now). inf when blocked for long."""
        with self._cond:
            now = time.time()
            if now < self._blocked_until:
                return self._blocked_until - now
            self._prune(now)
            if len(self._stamps) < self.rpm:
                return 0.0
            return self._stamps[0] + 60.0 - now

    def acquire(self, max_wait: float) -> bool:
        """Reserve one request slot, waiting up to `max_wait` seconds. False = give up."""
        deadline = time.time() + max_wait
        with self._cond:
            while True:
                now = time.time()
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                else:
                    self._prune(now)
                    if len(self._stamps) < self.rpm:
                        self._stamps.append(now)
                        return True
                    wait = self._stamps[0] + 60.0 - now
                if now + wait > deadline:
                    return False
                self._cond.wait(timeout=min(wait + 0.05, 5.0))

    def used(self) -> int:
        with self._cond:
            self._prune(time.time())
            return len(self._stamps)

    def block(self, seconds: float, reason: str = "") -> None:
        with self._cond:
            self._blocked_until = max(self._blocked_until, time.time() + seconds)
            self._blocked_reason = reason
            self._cond.notify_all()

    def unblock(self) -> None:
        with self._cond:
            self._blocked_until = 0.0
            self._blocked_reason = ""
            self._cooldowns.clear()
            self._cond.notify_all()

    def blocked_for(self) -> float:
        with self._cond:
            return max(0.0, self._blocked_until - time.time())

    def blocked_reason(self) -> str:
        with self._cond:
            return self._blocked_reason if time.time() < self._blocked_until else ""

    def cool(self, model: str, seconds: float) -> None:
        with self._cond:
            self._cooldowns[model] = time.time() + seconds

    def cooling(self, model: str) -> float:
        with self._cond:
            return max(0.0, self._cooldowns.get(model, 0.0) - time.time())

    def cooling_models(self) -> dict[str, int]:
        with self._cond:
            now = time.time()
            return {m: int(t - now) for m, t in self._cooldowns.items() if t > now}
