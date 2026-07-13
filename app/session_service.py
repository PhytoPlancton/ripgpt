from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

from . import browser
from . import ratelimit


logger = logging.getLogger("ripgpt.session")

# How often (in seconds) to verify the ChatGPT session is still active.
SESSION_CHECK_INTERVAL = int(os.environ.get("SESSION_CHECK_INTERVAL", "900"))

# File turns (upload + ChatGPT ingest of large docs) can take minutes — longer budget.
FILE_TURN_TIMEOUT = int(os.environ.get("FILE_TURN_TIMEOUT", "840"))
DEFAULT_TURN_TIMEOUT = 330

# Hard ceiling on how long a SINGLE turn may occupy the browser (seconds). Kept UNDER the
# Cloudflare edge timeout (~100s) so a slow turn returns a clean error instead of a 524 —
# and so one stuck turn can't hog a worker. Raise only for direct (non-tunnel) access.
TURN_HARD_TIMEOUT = float(os.environ.get("TURN_HARD_TIMEOUT", "85"))

# Watchdog: if a turn stays in-flight far past its cap, the browser is genuinely hung (e.g. a
# frozen page.evaluate, which has no native timeout). SOFT → flag degraded + enqueue a restart;
# HARD → exit the process so Docker (restart: unless-stopped) brings up fresh browsers.
WATCHDOG_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "10"))
WATCHDOG_SOFT_S = float(os.environ.get("WATCHDOG_SOFT_S", "130"))
WATCHDOG_HARD_S = float(os.environ.get("WATCHDOG_HARD_S", "200"))
WATCHDOG_HARD_EXIT = (os.environ.get("WATCHDOG_HARD_EXIT", "true").strip().lower()
                      in ("1", "true", "yes", "on"))

# Start a fresh temporary chat per turn (default) so a prior turn's answer can't leak into the
# next one. Set FRESH_TEMPORARY_CHAT=false to restore reuse (faster, but answers can cross-wire).
FRESH_TEMPORARY_CHAT = (os.environ.get("FRESH_TEMPORARY_CHAT", "true").strip().lower()
                        in ("1", "true", "yes", "on"))

# ── Multi-account pool ─────────────────────────────────────────────────────────
# Number of ChatGPT accounts (each = its own browser + profile + cookies) served behind ONE
# ripgpt API key. POOL_SIZE=1 (default) is the exact single-account behaviour. Each extra slot
# i reads CHATGPT_SESSION_TOKEN_i / CHATGPT_COOKIES_i and gets its own profile dir "<base>/sloti".
# ⚠ Same-IP multi-account raises ban-in-a-cluster risk — pace conservatively (rate governor).
POOL_SIZE = max(1, min(8, int(os.environ.get("POOL_SIZE", "1") or "1")))
# Stagger worker startup so N browsers don't all hit chatgpt.com at the same instant (same IP).
POOL_STARTUP_STAGGER_S = float(os.environ.get("POOL_STARTUP_STAGGER_S", "5"))


def _worker_config(i: int) -> tuple[str, str, str]:
    """(profile_dir, session_token, cookies) for pool slot i. Slot 0 == the current account."""
    if i == 0:
        return (browser.BROWSER_PROFILE_DIR, browser.CHATGPT_SESSION_TOKEN, browser.CHATGPT_COOKIES)
    base = browser.BROWSER_PROFILE_DIR or "/data/profile"
    return (
        os.path.join(base, f"slot{i}"),
        os.environ.get(f"CHATGPT_SESSION_TOKEN_{i}", "").strip(),
        os.environ.get(f"CHATGPT_COOKIES_{i}", "").strip(),
    )


class Worker:
    """One ChatGPT account: its own browser session, thread, and health state."""

    def __init__(self, wid: int, profile_dir: str, session_token: str, cookies: str):
        self.id = wid
        self.profile_dir = profile_dir
        self.session_token = session_token
        self.cookies = cookies
        self.session: browser.ChatSession | None = None
        self.in_flight: dict | None = None
        self.browser_start_ts: float | None = None
        self.restart_count = 0
        self.startup_error: Exception | None = None
        self.ready = threading.Event()


@dataclass(slots=True)
class SessionRequest:
    prompt: str
    temporary: bool
    holder: dict | queue.Queue
    done_event: threading.Event
    stream: bool = False
    model_slug: str | None = None
    image: bool = False          # image-generation turn (capture the rendered <img>)
    files: list | None = None    # [(filename, mime, bytes)] to upload into the composer
    control: str | None = None   # e.g. "restart" — handled by the worker, not a chat turn


class BrowserSessionService:
    def __init__(self, startup_timeout: float = 300.0):
        self._startup_timeout = startup_timeout
        self._request_queue: queue.Queue[SessionRequest | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._watchdog: threading.Thread | None = None
        self._proxy_start_ts = time.time()
        self._paused = False
        self._degraded = False
        self._workers: list[Worker] = [Worker(i, *_worker_config(i)) for i in range(POOL_SIZE)]

    def start(self) -> None:
        if self._threads and any(t.is_alive() for t in self._threads):
            return
        self._threads = []
        for w in self._workers:
            w.ready.clear()
            w.startup_error = None
            t = threading.Thread(target=self._worker_loop, args=(w,),
                                 name=f"ripgpt-browser-{w.id}", daemon=True)
            t.start()
            self._threads.append(t)
        if not (self._watchdog and self._watchdog.is_alive()):
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="ripgpt-watchdog", daemon=True)
            self._watchdog.start()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        deadline = time.time() + (timeout or self._startup_timeout)
        for w in self._workers:
            w.ready.wait(max(0.0, deadline - time.time()))
        return self.is_ready()

    def is_ready(self) -> bool:
        return any(w.ready.is_set() and w.startup_error is None and w.session is not None
                   for w in self._workers)

    def health_ok(self) -> bool:
        """Liveness for /health: at least one worker ready, and not watchdog-degraded."""
        return self.is_ready() and not self._degraded

    # ── monitoring / control ──────────────────────────────────────────────
    def queue_depth(self) -> int:
        return self._request_queue.qsize()

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)

    def _safe_enqueue_restart(self) -> None:
        try:
            self._request_queue.put(SessionRequest(prompt="", temporary=False, holder={},
                                                   done_event=threading.Event(), control="restart"))
        except Exception:
            pass

    def request_restart(self, timeout: float = 120) -> bool:
        """Recreate every worker's browser session (one-click recovery)."""
        self._ensure_ready()
        events = []
        for _ in self._workers:
            ev = threading.Event()
            self._request_queue.put(SessionRequest(prompt="", temporary=False, holder={},
                                                   done_event=ev, control="restart"))
            events.append(ev)
        deadline = time.time() + timeout
        ok = True
        for ev in events:
            ok = ev.wait(max(0.0, deadline - time.time())) and ok
        return ok

    def _worker_state(self, w: Worker) -> str:
        if w.startup_error is not None:
            return "browser_dead"
        if not w.ready.is_set() or w.session is None:
            return "starting"
        if getattr(w.session, "logged_out", False):
            return "logged_out"
        return "logged_in"

    def _session_state(self) -> str:
        states = [self._worker_state(w) for w in self._workers]
        for s in ("logged_in", "logged_out", "starting", "browser_dead"):
            if s in states:
                return s
        return "browser_dead"

    def live_state(self) -> dict:
        now = time.time()
        workers = []
        in_flight = None
        for w in self._workers:
            wf = None
            if w.in_flight:
                wf = {"model": w.in_flight.get("model"),
                      "age_s": round(now - w.in_flight.get("started", now), 1)}
                if in_flight is None:
                    in_flight = wf
            workers.append({
                "id": w.id,
                "state": self._worker_state(w),
                "in_flight": wf,
                "browser_uptime_s": round(now - w.browser_start_ts) if w.browser_start_ts else None,
                "restart_count": w.restart_count,
            })
        ups = [w.browser_start_ts for w in self._workers if w.browser_start_ts]
        return {
            "session_state": self._session_state(),
            "queue_depth": self.queue_depth(),
            "in_flight": in_flight,
            "proxy_uptime_s": round(now - self._proxy_start_ts),
            "browser_uptime_s": round(now - min(ups)) if ups else None,
            "restart_count": sum(w.restart_count for w in self._workers),
            "paused": self._paused,
            "degraded": self._degraded,
            "pool_size": len(self._workers),
            "workers": workers,
        }

    def _do_restart(self, w: Worker) -> None:
        logger.info("Restarting browser session (worker %d)...", w.id)
        try:
            if w.session is not None:
                w.session.close()
        except Exception:
            pass
        try:
            w.session = browser.ChatSession(profile_dir=w.profile_dir,
                                            session_token=w.session_token, cookies=w.cookies)
            w.browser_start_ts = time.time()
            w.restart_count += 1
            w.startup_error = None
            logger.info("Browser session restarted (worker %d).", w.id)
        except Exception as exc:
            w.startup_error = exc
            logger.exception("Browser restart failed (worker %d).", w.id)

    def _run_turn(self, w: Worker, request: "SessionRequest", force_fresh: bool = False) -> None:
        """Execute one chat turn on this worker (stream or not). Raises on failure."""
        self._start_new_chat(w, temporary=request.temporary, force=force_fresh)
        if request.stream:
            assert isinstance(request.holder, queue.Queue)
            page = w.session._page
            baseline = browser._read_answer_from_dom(page)   # previous answer (if chat reused)
            w.session.send(request.prompt, request.model_slug, image=request.image, files=request.files)
            self._stream_answer_via_dom(page, request.holder, baseline=baseline)
        else:
            assert isinstance(request.holder, dict)
            request.holder["answer"] = w.session.ask(
                request.prompt, request.model_slug, image=request.image, files=request.files,
                answer_timeout=TURN_HARD_TIMEOUT)

    def _put_error(self, request: "SessionRequest", exc: Exception) -> None:
        if request.stream:
            assert isinstance(request.holder, queue.Queue)
            request.holder.put({"error": str(exc)})
            request.holder.put(None)
        else:
            assert isinstance(request.holder, dict)
            request.holder["error"] = str(exc)

    def stop(self) -> None:
        if not self._threads:
            return
        for _ in self._threads:
            self._request_queue.put(None)
        for t in self._threads:
            t.join(timeout=30)
        self._threads = []

    def ask(self, prompt: str, temporary: bool = False, model_slug: str | None = None,
            image: bool = False, files: list | None = None, timeout: float | None = None) -> str:
        self._ensure_ready()
        if timeout is None:
            timeout = FILE_TURN_TIMEOUT if files else DEFAULT_TURN_TIMEOUT
        result: dict[str, str] = {}
        done_event = threading.Event()
        self._request_queue.put(SessionRequest(prompt=prompt, temporary=temporary, holder=result,
                                               done_event=done_event, model_slug=model_slug,
                                               image=image, files=files))
        if not done_event.wait(timeout):
            raise TimeoutError("Browser session timed out waiting for a response.")
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("answer", "")

    def stream(self, prompt: str, temporary: bool = False, model_slug: str | None = None,
               image: bool = False, files: list | None = None) -> queue.Queue:
        self._ensure_ready()
        chunk_queue: queue.Queue = queue.Queue()
        done_event = threading.Event()
        self._request_queue.put(SessionRequest(prompt=prompt, temporary=temporary, holder=chunk_queue,
                                               done_event=done_event, stream=True, model_slug=model_slug,
                                               image=image, files=files))
        return chunk_queue

    def _ensure_ready(self) -> None:
        if not self.wait_until_ready():
            if not self.is_ready():
                errs = [w.startup_error for w in self._workers if w.startup_error is not None]
                if errs and all(w.startup_error is not None for w in self._workers):
                    raise RuntimeError(f"Browser session failed to start: {errs[0]}")
            raise TimeoutError("Browser session did not become ready in time.")

    def _check_and_restore_session(self, w: Worker) -> None:
        if w.session is None:
            return
        try:
            if not w.session.is_alive():
                logger.warning("Session expired (worker %d) — re-logging in...", w.id)
                w.session.relogin()
                logger.info("Session restored (worker %d).", w.id)
        except Exception as exc:
            logger.error("Session health check/restore failed (worker %d): %s", w.id, exc)

    def _worker_loop(self, w: Worker) -> None:
        if w.id and POOL_STARTUP_STAGGER_S:
            time.sleep(w.id * POOL_STARTUP_STAGGER_S)   # avoid N browsers hitting chatgpt.com at once
        logger.info("Starting browser session (worker %d)...", w.id)
        try:
            w.session = browser.ChatSession(profile_dir=w.profile_dir,
                                            session_token=w.session_token, cookies=w.cookies)
            w.browser_start_ts = time.time()
        except Exception as exc:
            w.startup_error = exc
            logger.exception("Browser session startup failed (worker %d).", w.id)
            w.ready.set()
            return

        w.ready.set()
        logger.info("Browser session ready (worker %d) — accepting requests.", w.id)
        last_check = time.time()

        while True:
            time_until_check = max(0.1, SESSION_CHECK_INTERVAL - (time.time() - last_check))
            try:
                request = self._request_queue.get(timeout=time_until_check)
            except queue.Empty:
                self._check_and_restore_session(w)
                last_check = time.time()
                continue

            if request is None:
                break

            if request.control == "restart":
                self._do_restart(w)
                request.done_event.set()
                last_check = time.time()
                continue

            w.in_flight = {"model": request.model_slug or "auto", "started": time.time()}
            try:
                self._run_turn(w, request)
            except browser.RateLimitedError as exc:
                # ChatGPT said "too fast" → force a cooldown so we stop hammering the account,
                # and surface a clean error (never return the throttle text as an answer).
                logger.warning("ChatGPT rate-limited (worker %d) — tripping anti-ban cooldown.", w.id)
                try:
                    ratelimit.RATE.trip_cooldown()
                except Exception:
                    pass
                self._put_error(request, exc)
            except browser.StaleChatError:
                # Expired temporary chat (24h idle) → force a brand-new chat and retry once.
                logger.warning("Temporary chat expired (worker %d) — fresh chat + retry.", w.id)
                try:
                    self._run_turn(w, request, force_fresh=True)
                except Exception as exc2:
                    self._put_error(request, exc2)
                    logger.error("Retry on fresh chat failed (worker %d): %s", w.id, exc2)
            except Exception as exc:
                msg = str(exc)
                nav_race = ("execution context" in msg.lower()) or ("navigation" in msg.lower())
                wedged = ("prompt-textarea" in msg) or ("Timeout" in msg) or ("composer" in msg.lower())
                if nav_race and not wedged:
                    logger.warning("Navigation race (worker %d: %s) — retrying once.", w.id, msg[:80])
                    time.sleep(1.0)
                    try:
                        self._run_turn(w, request)
                    except Exception as exc2:
                        self._put_error(request, exc2)
                        logger.error("Retry after nav race failed (worker %d): %s", w.id, exc2)
                elif wedged:
                    logger.warning("Session wedged (worker %d: %s) — recreating + retry.", w.id, msg[:80])
                    self._do_restart(w)
                    try:
                        self._run_turn(w, request)
                    except Exception as exc2:
                        self._put_error(request, exc2)
                        logger.error("Retry after restart failed (worker %d): %s", w.id, exc2)
                else:
                    self._put_error(request, exc)
                    logger.error("Session error (worker %d): %s", w.id, exc)
            finally:
                w.in_flight = None
                request.done_event.set()
                last_check = time.time()

        logger.info("Shutting down browser session (worker %d)...", w.id)
        if w.session is not None:
            w.session.close()

    def _watchdog_loop(self) -> None:
        """Detect a turn stuck far past its cap (a hard browser hang) on ANY worker and recover."""
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            now = time.time()
            stuck_hard = False
            stuck_soft = False
            for w in self._workers:
                inf = w.in_flight
                if not inf:
                    continue
                age = now - inf.get("started", now)
                if age > WATCHDOG_HARD_S:
                    stuck_hard = True
                elif age > WATCHDOG_SOFT_S:
                    stuck_soft = True
            if stuck_hard:
                logger.critical("Watchdog: a turn is hard-hung (>%.0fs) — %s", WATCHDOG_HARD_S,
                                "exiting for container restart" if WATCHDOG_HARD_EXIT else "enqueuing restart")
                if WATCHDOG_HARD_EXIT:
                    os._exit(1)   # Docker restart: unless-stopped recreates fresh browsers
                self._safe_enqueue_restart()
            elif stuck_soft:
                if not self._degraded:
                    self._degraded = True
                    logger.warning("Watchdog: a turn is stuck (>%.0fs) — degraded, enqueuing restart.",
                                   WATCHDOG_SOFT_S)
                    self._safe_enqueue_restart()
            else:
                self._degraded = False

    def _start_new_chat(self, w: Worker, temporary: bool = False, force: bool = False) -> None:
        assert w.session is not None
        page = w.session._page
        current = page.url
        target = "https://chatgpt.com/?temporary-chat=true" if temporary else "https://chatgpt.com"

        # Reuse the current chat only when allowed. force=True bypasses reuse (stale-chat recovery).
        if not force and not temporary and current.rstrip("/") == "https://chatgpt.com":
            return
        # Default: a fresh temporary chat per turn so a prior answer can't leak into this one.
        if (not force and temporary and not FRESH_TEMPORARY_CHAT
                and "temporary-chat=true" in current and "/c/" not in current):
            return

        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            browser._ensure_composer(page)
            try:
                page.evaluate(browser.FETCH_INTERCEPT_JS)   # re-assert; init script already injected it
            except Exception:
                pass
        except Exception as exc:
            logger.warning("Could not start new chat (worker %d): %s", w.id, exc)

    def _stream_answer_via_dom(self, page, chunk_queue: queue.Queue, baseline: str = "") -> None:
        sent = ""
        previous_safe = ""
        deadline = time.time() + min(browser.ANSWER_TIMEOUT, TURN_HARD_TIMEOUT)
        last_markdown = ""
        last_change = time.time()
        started = False
        time.sleep(0.5)

        while time.time() < deadline:
            # ChatGPT interstitials appear instead of an answer — detect before streaming so
            # the worker backs off / retries on a fresh chat (never emit the wall text).
            if not sent:
                _dom = browser._read_answer_from_dom(page)
                if browser._is_rate_limited(_dom):
                    raise browser.RateLimitedError("ChatGPT is rate-limiting the account (requests too fast).")
                if browser._is_stale_chat(_dom):
                    raise browser.StaleChatError("ChatGPT temporary chat expired (chat history off).")
            done = bool(page.evaluate("() => !!window.__answer_done"))
            started = started or bool(page.evaluate("() => !!window.__turn_started"))
            current_markdown = browser._read_answer_from_dom(page, strict=True)
            if not current_markdown or current_markdown == baseline:
                current_markdown = ""
            safe_prefix = self._stream_safe_prefix(current_markdown, done=False) if current_markdown else ""

            if previous_safe:
                stable_length = self._common_prefix_len(previous_safe, safe_prefix)
                if stable_length > len(sent):
                    chunk_queue.put(previous_safe[len(sent):stable_length])
                    sent = previous_safe[:stable_length]

            previous_safe = safe_prefix

            if current_markdown != last_markdown:
                last_markdown = current_markdown
                last_change = time.time()
            if not done and started and current_markdown and not browser._is_generating(page) \
                    and (time.time() - last_change) > browser.DOM_STABLE_SECS:
                done = True

            if done:
                time.sleep(0.4)
                final_markdown = browser._read_answer_from_dom(page, strict=True)
                if not final_markdown or final_markdown == baseline:
                    nf = browser._read_answer_from_dom(page)
                    final_markdown = nf if (nf and nf != baseline) else sent
                if len(final_markdown) > len(sent):
                    chunk_queue.put(final_markdown[len(sent):])
                break

            time.sleep(0.25)

        chunk_queue.put(None)

    @staticmethod
    def _common_prefix_len(left: str, right: str) -> int:
        limit = min(len(left), len(right))
        index = 0
        while index < limit and left[index] == right[index]:
            index += 1
        return index

    @staticmethod
    def _stream_safe_prefix(markdown: str, done: bool = False) -> str:
        if done or not markdown:
            return markdown
        matches = list(re.finditer(r"```[^\n]*\n.*?\n```(?:\n)?", markdown, re.DOTALL))
        if not matches:
            return markdown
        last_match = matches[-1]
        trailing = markdown[last_match.end():]
        if trailing.strip():
            return markdown
        return markdown[:last_match.start()]
