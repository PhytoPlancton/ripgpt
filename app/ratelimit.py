"""Ban-protection rate governor for ripgpt.

ripgpt drives ONE real ChatGPT account. A runaway client (e.g. a prospecting agent that
loops) hammering it at machine cadence is exactly the pattern that gets a ChatGPT account
flagged/banned. This governs the rate of ACCEPTED /v1 turns so ChatGPT is never called
faster than a human-plausible cadence, and trips a cooldown when ChatGPT starts pushing
back (empty replies / timeouts = early throttle signal) instead of hammering through it.

Two mechanisms:
  * Rolling caps: per-minute / per-hour / per-day + optional min interval between turns.
    Over the cap → the caller gets 429 + Retry-After (no ChatGPT call happens).
  * Circuit breaker: after N consecutive throttle signals, PAUSE for a cooldown (503),
    giving the account a rest rather than restart-looping (which burns cf_clearance).

All limits are env-configurable; defaults are conservative (protect by default). Set a cap
to 0 to disable it.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


MIN_INTERVAL = _float("RATE_MIN_INTERVAL_S", 0.0)   # min seconds between accepted turns (0=off)
PER_MIN = _int("RATE_PER_MIN", 20)
PER_HOUR = _int("RATE_PER_HOUR", 300)
PER_DAY = _int("RATE_PER_DAY", 2000)
BREAKER_THRESHOLD = _int("RATE_BREAKER_THRESHOLD", 4)      # consecutive throttle signals → trip
BREAKER_COOLDOWN = _float("RATE_BREAKER_COOLDOWN_S", 600)  # cooldown length (seconds)

# Error classes that indicate ChatGPT is throttling / unhappy (early ban signal).
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
        with self._lock:
            self._prune(now)
            if now < self._cooldown_until:
                return False, int(self._cooldown_until - now) + 1, "cooldown"
            if MIN_INTERVAL and (now - self._last_accept) < MIN_INTERVAL:
                return False, max(1, int(MIN_INTERVAL - (now - self._last_accept)) + 1), "min_interval"
            if PER_MIN and self._count_within(now, 60) >= PER_MIN:
                return False, 60, "per_minute"
            if PER_HOUR and self._count_within(now, 3600) >= PER_HOUR:
                return False, 300, "per_hour"
            if PER_DAY and self._count_within(now, 86400) >= PER_DAY:
                return False, 3600, "per_day"
            self._ts.append(now)
            self._last_accept = now
            return True, 0, "ok"

    def note_result(self, ok: bool, error_class: str | None = None) -> None:
        """Feed the circuit breaker with each turn's outcome."""
        with self._lock:
            if ok:
                self._consec_throttle = 0
                return
            if error_class in _THROTTLE_SIGNALS:
                self._consec_throttle += 1
                if (self._consec_throttle >= BREAKER_THRESHOLD
                        and time.time() >= self._cooldown_until):
                    self._cooldown_until = time.time() + BREAKER_COOLDOWN
                    self._breaker_trips += 1
                    self._consec_throttle = 0

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._prune(now)
            return {
                "per_min": {"used": self._count_within(now, 60), "cap": PER_MIN},
                "per_hour": {"used": self._count_within(now, 3600), "cap": PER_HOUR},
                "per_day": {"used": self._count_within(now, 86400), "cap": PER_DAY},
                "cooldown_active": now < self._cooldown_until,
                "cooldown_remaining_s": max(0, int(self._cooldown_until - now)),
                "breaker_trips": self._breaker_trips,
            }


RATE = RateGovernor()
