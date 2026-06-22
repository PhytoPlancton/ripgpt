"""Lightweight in-memory metrics for the ripgpt monitoring dashboard.

One ring buffer of recent requests + rolling 1-minute buckets + a few derived stats.
Everything is computed on read from the buffer (volume is low). Thread-safe via a lock
because requests are recorded from the API thread while /stats reads concurrently.
"""

from __future__ import annotations

import threading
import time
from collections import deque

# Error taxonomy surfaced on the dashboard.
ERROR_CLASSES = ("empty_reply", "composer_timeout", "logged_out", "http_500", "nav_error", "timeout", "other")


def classify_error(message: str) -> str:
    m = (message or "").lower()
    if "prompt-textarea" in m or "composer" in m:
        return "composer_timeout"
    if "logged" in m and "out" in m:
        return "logged_out"
    if "timed out" in m or "timeout" in m:
        return "timeout"
    if "execution context" in m or "navigation" in m or "navigating" in m:
        return "nav_error"
    if "empty" in m:
        return "empty_reply"
    return "other"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


class Metrics:
    def __init__(self, max_recent: int = 500):
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=max_recent)
        self.last_success_ts: float | None = None
        self.consecutive_failures = 0
        self.consecutive_empty_or_timeout = 0

    def record(self, *, model_req: str, model_res: str | None, status: str,
               error_class: str | None, latency_ms: int,
               ptoks: int = 0, ctoks: int = 0) -> None:
        now = time.time()
        rec = {
            "ts": now,
            "model_req": model_req,
            "model_res": model_res or model_req,
            "status": status,                       # "ok" | "error"
            "error_class": error_class,
            "latency_ms": int(latency_ms),
            "ptoks_est": int(ptoks),
            "ctoks_est": int(ctoks),
        }
        with self._lock:
            self._recent.append(rec)
            if status == "ok":
                self.last_success_ts = now
                self.consecutive_failures = 0
                self.consecutive_empty_or_timeout = 0
            else:
                self.consecutive_failures += 1
                if error_class in ("empty_reply", "composer_timeout", "timeout"):
                    self.consecutive_empty_or_timeout += 1

    def _window(self, recs: list, seconds: float, now: float) -> list:
        cutoff = now - seconds
        return [r for r in recs if r["ts"] >= cutoff]

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            recs = list(self._recent)
            cons_fail = self.consecutive_failures
            cons_empty = self.consecutive_empty_or_timeout
            last_ok = self.last_success_ts

        last_15 = self._window(recs, 900, now)
        prev_15 = [r for r in recs if now - 1800 <= r["ts"] < now - 900]

        def err_rate(rs):
            return round(sum(1 for r in rs if r["status"] != "ok") / len(rs), 4) if rs else 0.0

        # per-model latency on successful calls in the last hour
        last_hour = self._window(recs, 3600, now)
        by_model: dict[str, list[float]] = {}
        for r in last_hour:
            if r["status"] == "ok":
                by_model.setdefault(r["model_res"], []).append(r["latency_ms"])
        model_latency = [
            {"model": m, "count": len(v), "p50": int(_percentile(v, 50)), "p95": int(_percentile(v, 95))}
            for m, v in sorted(by_model.items(), key=lambda kv: -len(kv[1]))
        ]

        # error breakdown (last hour)
        by_error: dict[str, int] = {}
        for r in last_hour:
            if r["status"] != "ok" and r["error_class"]:
                by_error[r["error_class"]] = by_error.get(r["error_class"], 0) + 1

        # 1-minute time series for the last 60 minutes
        buckets: dict[int, dict] = {}
        for r in self._window(recs, 3600, now):
            b = int(r["ts"] // 60 * 60)
            d = buckets.setdefault(b, {"t": b, "ok": 0, "err": 0, "lat_sum": 0, "n": 0})
            if r["status"] == "ok":
                d["ok"] += 1
            else:
                d["err"] += 1
            d["lat_sum"] += r["latency_ms"]
            d["n"] += 1
        series = [
            {"t": d["t"], "ok": d["ok"], "err": d["err"],
             "avg_latency_ms": int(d["lat_sum"] / d["n"]) if d["n"] else 0}
            for d in sorted(buckets.values(), key=lambda x: x["t"])
        ]

        recent = list(reversed(recs[-50:]))
        return {
            "now": now,
            "error_rate_15m": err_rate(last_15),
            "error_rate_prev_15m": err_rate(prev_15),
            "req_15m": len(last_15),
            "totals": {"ok": sum(1 for r in recs if r["status"] == "ok"),
                       "err": sum(1 for r in recs if r["status"] != "ok"),
                       "tracked": len(recs)},
            "consecutive_failures": cons_fail,
            "consecutive_empty_or_timeout": cons_empty,
            "last_success_ts": last_ok,
            "seconds_since_success": (now - last_ok) if last_ok else None,
            "by_model_latency": model_latency,
            "by_error_class": by_error,
            "series": series,
            "recent": recent,
        }


METRICS = Metrics()
