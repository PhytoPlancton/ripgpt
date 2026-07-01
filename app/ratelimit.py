"""Ban-protection rate governor for ripgpt.

Paces accepted /v1 turns so a runaway client can't hammer the single shared ChatGPT account
(the pattern that gets accounts flagged/banned), and trips a cooldown when ChatGPT starts
pushing back (empty replies / timeouts) instead of restart-looping (which burns cf_clearance).

Limits are read LIVE from app.settings on every call, so the admin UI can change them with no
restart. Two mechanisms: rolling caps (per-minute / hour / day + optional min interval) and a
circuit breaker (N consecutive throttle signals → cooldown).
"""

from __future__ import annotations

import threading
import time
from collections import deque

from app.settings import SETTINGS

# Error classes that mean ChatGPT is throttling / unhappy (early ban signal).
_THROTTLE_SIGNALS = {"empty_reply", "timeout", "composer_timeout", "logged_out", "nav_error", "rate"}


class RateGovernor:
    def __init__(self):
        self._lock = threading.Lock()
        self._ts: deque[float] = deque()      # timestamps of accepted turns (last 24h)
        self._last_accept = 0.0
        self._cooldown_until = 0.0
        self._consec_throttle = 0
        self._breaker_trips = 0

    def _prune(self, now: float) -> None:
        cutoff = now - 86400
        while self._ts and self._ts[0] < cutoff:
            self._ts.popleft()

    def _count_within(self, now: float, window: float) -> int:
        c = 0
        for t in reversed(self._ts):
            if t >= now - window:
                c += 1
            else:
                break
        return c

    def allow(self) -> tuple[bool, int, str]:
        """Gate one turn. Returns (ok, retry_after_seconds, reason). Records on accept."""
        now = time.time()
        s = SETTINGS.all()   # one consistent snapshot; read before taking our lock (no nesting)
        min_interval = s["rate_min_interval_s"]
        per_min, per_hour, per_day = s["rate_per_min"], s["rate_per_hour"], s["rate_per_day"]
        with self._lock:
            self._prune(now)
            if now < self._cooldown_until:
                return False, int(self._cooldown_until - now) + 1, "cooldown"
            if min_interval and (now - self._last_accept) < min_interval:
                return False, max(1, int(min_interval - (now - self._last_accept)) + 1), "min_interval"
            if per_min and self._count_within(now, 60) >= per_min:
                return False, 60, "per_minute"
            if per_hour and self._count_within(now, 3600) >= per_hour:
                return False, 300, "per_hour"
            if per_day and self._count_within(now, 86400) >= per_day:
                return False, 3600, "per_day"
            self._ts.append(now)
            self._last_accept = now
            return True, 0, "ok"

    def note_result(self, ok: bool, error_class: str | None = None) -> None:
        """Feed the circuit breaker with each turn's outcome."""
        threshold = SETTINGS.get("breaker_threshold")
        cooldown = SETTINGS.get("breaker_cooldown_s")
        with self._lock:
            if ok:
                self._consec_throttle = 0
                return
            if error_class in _THROTTLE_SIGNALS:
                self._consec_throttle += 1
                if self._consec_throttle >= threshold and time.time() >= self._cooldown_until:
                    self._cooldown_until = time.time() + cooldown
                    self._breaker_trips += 1
                    self._consec_throttle = 0

    def snapshot(self) -> dict:
        now = time.time()
        s = SETTINGS.all()
        with self._lock:
            self._prune(now)
            return {
                "per_min": {"used": self._count_within(now, 60), "cap": s["rate_per_min"]},
                "per_hour": {"used": self._count_within(now, 3600), "cap": s["rate_per_hour"]},
                "per_day": {"used": self._count_within(now, 86400), "cap": s["rate_per_day"]},
                "cooldown_active": now < self._cooldown_until,
                "cooldown_remaining_s": max(0, int(self._cooldown_until - now)),
                "breaker_trips": self._breaker_trips,
            }


RATE = RateGovernor()
