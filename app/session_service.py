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
from . import accounts


logger = logging.getLogger("ripgpt.session")

SESSION_CHECK_INTERVAL = int(os.environ.get("SESSION_CHECK_INTERVAL", "900"))
FILE_TURN_TIMEOUT = int(os.environ.get("FILE_TURN_TIMEOUT", "840"))
DEFAULT_TURN_TIMEOUT = 330

# Hard ceiling on how long a SINGLE turn may occupy a browser (kept under Cloudflare's ~100s).
TURN_HARD_TIMEOUT = float(os.environ.get("TURN_HARD_TIMEOUT", "85"))

WATCHDOG_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "10"))
WATCHDOG_SOFT_S = float(os.environ.get("WATCHDOG_SOFT_S", "130"))
WATCHDOG_HARD_S = float(os.environ.get("WATCHDOG_HARD_S", "200"))
WATCHDOG_HARD_EXIT = (os.environ.get("WATCHDOG_HARD_EXIT", "true").strip().lower()
                      in ("1", "true", "yes", "on"))

FRESH_TEMPORARY_CHAT = (os.environ.get("FRESH_TEMPORARY_CHAT", "true").strip().lower()
                        in ("1", "true", "yes", "on"))

# Extra ChatGPT accounts from env (slot 0 = the main account). UI-added accounts stack on top.
POOL_SIZE = max(1, min(8, int(os.environ.get("POOL_SIZE", "1") or "1")))
POOL_STARTUP_STAGGER_S = float(os.environ.get("POOL_STARTUP_STAGGER_S", "5"))
# How often the worker loop wakes to notice stop/reconnect flags (health check still runs on
# SESSION_CHECK_INTERVAL). Kept short so UI actions apply within a few seconds.
_LOOP_WAKE_S = float(os.environ.get("LOOP_WAKE_S", "8"))


def _env_worker_config(i: int) -> tuple[str, str, str]:
    """(profile_dir, session_token, cookies) for env pool slot i. Slot 0 = the main account."""
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

    def __init__(self, wid, profile_dir, session_token, cookies, account_id=None, label=""):
        self.id = wid
        self.profile_dir = profile_dir
        self.session_token = session_token
        self.cookies = cookies              # in-memory only; used to bootstrap, then discarded
        self.account_id = account_id        # store id for UI-added accounts (None for env slots)
        self.label = label
        self.session: browser.ChatSession | None = None
        self.in_flight: dict | None = None
        self.browser_start_ts: float | None = None
        self.restart_count = 0
        self.startup_error: Exception | None = None
        self.ready = threading.Event()
        self.stop = threading.Event()       # set → worker exits and removes itself
        self.pending_cookie: str | None = None  # set → worker re-bootstraps with this cookie
        self.restart_req = threading.Event()    # set → THIS worker restarts its browser
        self.restart_done = threading.Event()   # signals the requested restart completed
        self.thread: threading.Thread | None = None


@dataclass(slots=True)
class SessionRequest:
    prompt: str
    temporary: bool
    holder: dict | queue.Queue
    done_event: threading.Event
    stream: bool = False
    model_slug: str | None = None
    image: bool = False
    files: list | None = None
    control: str | None = None


class BrowserSessionService:
    def __init__(self, startup_timeout: float = 300.0):
        self._startup_timeout = startup_timeout
        self._request_queue: queue.Queue[SessionRequest | None] = queue.Queue()
        self._watchdog: threading.Thread | None = None
        self._proxy_start_ts = time.time()
        self._paused = False
        self._degraded = False
        self._workers_lock = threading.Lock()
        self._next_wid = 0
        self._workers: list[Worker] = self._build_initial_workers()

    def _new_worker(self, profile_dir, session_token, cookies, account_id=None, label="") -> Worker:
        self._next_wid += 1
        return Worker(self._next_wid, profile_dir, session_token, cookies, account_id, label)

    def _build_initial_workers(self) -> list[Worker]:
        workers = []
        for i in range(POOL_SIZE):
            pd, tok, ck = _env_worker_config(i)
            workers.append(self._new_worker(pd, tok, ck, None,
                                            "compte principal" if i == 0 else f"env slot {i}"))
        base = browser.BROWSER_PROFILE_DIR or "/data/profile"
        for a in accounts.ACCOUNTS.list():
            pd = os.path.join(base, f"acct-{a['id']}")
            # No cookie: rely on the persisted profile (bootstrapped when the account was added).
            workers.append(self._new_worker(pd, "", "", a["id"], a.get("label") or a["id"]))
        return workers

    def _spawn(self, w: Worker) -> None:
        w.thread = threading.Thread(target=self._worker_loop, args=(w,),
                                    name=f"ripgpt-browser-{w.id}", daemon=True)
        w.thread.start()

    def start(self) -> None:
        for w in list(self._workers):
            if not (w.thread and w.thread.is_alive()):
                w.ready.clear()
                w.startup_error = None
                self._spawn(w)
        if not (self._watchdog and self._watchdog.is_alive()):
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="ripgpt-watchdog", daemon=True)
            self._watchdog.start()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        deadline = time.time() + (timeout or self._startup_timeout)
        for w in list(self._workers):
            w.ready.wait(max(0.0, deadline - time.time()))
        return self.is_ready()

    def is_ready(self) -> bool:
        return any(w.ready.is_set() and w.startup_error is None and w.session is not None
                   for w in list(self._workers))

    def health_ok(self) -> bool:
        return self.is_ready() and not self._degraded

    # ── account onboarding (from the UI) ────────────────────────────────────────
    def add_account(self, label: str, cookie: str) -> dict:
        """Register a new ChatGPT account, launch its worker live (bootstraps from the cookie)."""
        rec = accounts.ACCOUNTS.add(label)
        base = browser.BROWSER_PROFILE_DIR or "/data/profile"
        pd = os.path.join(base, f"acct-{rec['id']}")
        w = self._new_worker(pd, "", cookie or "", rec["id"], rec["label"])
        with self._workers_lock:
            self._workers.append(w)
        self._spawn(w)
        return rec

    def remove_account(self, account_id: str) -> bool:
        ok = accounts.ACCOUNTS.remove(account_id)
        for w in list(self._workers):
            if w.account_id == account_id:
                w.stop.set()
        return ok

    def reconnect_account(self, account_id: str, cookie: str) -> bool:
        """Re-bootstrap an account's worker with a fresh cookie.

        If the worker thread is alive it re-bootstraps in its own loop (pending_cookie).
        If it died at startup (e.g. the first cookie was invalid) we RESPAWN the thread —
        otherwise a fresh cookie would be silently dropped and the account stay dead.
        """
        for w in list(self._workers):
            if w.account_id == account_id:
                w.cookies = cookie or ""
                if w.thread and w.thread.is_alive():
                    w.pending_cookie = cookie or ""
                else:
                    w.startup_error = None
                    w.ready.clear()
                    w.stop.clear()
                    self._spawn(w)
                return True
        return False

    # ── monitoring / control ──────────────────────────────────────────────
    def queue_depth(self) -> int:
        return self._request_queue.qsize()

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)

    def request_restart(self, timeout: float = 120) -> bool:
        """Restart every worker's browser — each targeted on its OWN Worker (no shared-queue race)."""
        self._ensure_ready()
        ws = list(self._workers)
        for w in ws:
            w.restart_done.clear()
            w.restart_req.set()
        deadline = time.time() + timeout
        ok = True
        for w in ws:
            ok = w.restart_done.wait(max(0.0, deadline - time.time())) and ok
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
        states = [self._worker_state(w) for w in list(self._workers)]
        for s in ("logged_in", "logged_out", "starting", "browser_dead"):
            if s in states:
                return s
        return "browser_dead"

    def live_state(self) -> dict:
        now = time.time()
        workers = []
        in_flight = None
        snapshot = list(self._workers)
        for w in snapshot:
            wf = None
            if w.in_flight:
                wf = {"model": w.in_flight.get("model"),
                      "age_s": round(now - w.in_flight.get("started", now), 1)}
                if in_flight is None:
                    in_flight = wf
            workers.append({
                "id": w.id,
                "label": w.label,
                "account_id": w.account_id,           # non-null → removable from the UI
                "state": self._worker_state(w),
                "in_flight": wf,
                "browser_uptime_s": round(now - w.browser_start_ts) if w.browser_start_ts else None,
                "restart_count": w.restart_count,
            })
        ups = [w.browser_start_ts for w in snapshot if w.browser_start_ts]
        return {
            "session_state": self._session_state(),
            "queue_depth": self.queue_depth(),
            "in_flight": in_flight,
            "proxy_uptime_s": round(now - self._proxy_start_ts),
            "browser_uptime_s": round(now - min(ups)) if ups else None,
            "restart_count": sum(w.restart_count for w in snapshot),
            "paused": self._paused,
            "degraded": self._degraded,
            "pool_size": len(snapshot),
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
        self._start_new_chat(w, temporary=request.temporary, force=force_fresh)
        if request.stream:
            assert isinstance(request.holder, queue.Queue)
            page = w.session._page
            baseline = browser._read_answer_from_dom(page)
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
        workers = list(self._workers)
        for w in workers:
            w.stop.set()                     # each worker exits on its next wake (targeted)
        for _ in workers:
            self._request_queue.put(None)    # wake any blocked get() so shutdown is prompt
        for w in workers:
            if w.thread:
                w.thread.join(timeout=30)

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
            snap = list(self._workers)
            if snap and all(w.startup_error is not None for w in snap):
                raise RuntimeError(f"Browser session failed to start: {snap[0].startup_error}")
            raise TimeoutError("Browser session did not become ready in time.")

    def _check_and_restore_session(self, w: Worker) -> None:
        if w.session is None:
            return
        try:
            if not w.session.is_alive():
                logger.warning("Session expired (worker %d) — re-logging in...", w.id)
                w.session.relogin()
        except Exception as exc:
            logger.error("Session health check/restore failed (worker %d): %s", w.id, exc)

    def _worker_loop(self, w: Worker) -> None:
        if w.id and POOL_STARTUP_STAGGER_S:
            time.sleep(min(w.id, 4) * POOL_STARTUP_STAGGER_S)   # avoid simultaneous logins (same IP)
        logger.info("Starting browser session (worker %d: %s)...", w.id, w.label)
        try:
            w.session = browser.ChatSession(profile_dir=w.profile_dir,
                                            session_token=w.session_token, cookies=w.cookies)
            w.browser_start_ts = time.time()
        except Exception as exc:
            w.startup_error = exc
            logger.exception("Browser startup failed (worker %d).", w.id)
            w.ready.set()
            # A UI-added account whose bootstrap failed: leave it (shows browser_dead) so the
            # admin can reconnect; env slot 0 failing is the classic single-account error.
            return

        w.ready.set()
        logger.info("Browser session ready (worker %d: %s).", w.id, w.label)
        last_check = time.time()

        removed = False
        while True:
            if w.stop.is_set():
                removed = True
                break
            if w.pending_cookie is not None:
                w.cookies = w.pending_cookie
                w.pending_cookie = None
                logger.info("Reconnecting worker %d with a fresh cookie.", w.id)
                self._do_restart(w)
                last_check = time.time()
            if w.restart_req.is_set():          # targeted restart (admin button / watchdog)
                w.restart_req.clear()
                self._do_restart(w)
                w.restart_done.set()
                last_check = time.time()

            wait = min(_LOOP_WAKE_S, max(0.1, SESSION_CHECK_INTERVAL - (time.time() - last_check)))
            try:
                request = self._request_queue.get(timeout=wait)
            except queue.Empty:
                if time.time() - last_check >= SESSION_CHECK_INTERVAL:
                    self._check_and_restore_session(w)
                    last_check = time.time()
                continue

            if request is None:
                break

            w.in_flight = {"model": request.model_slug or "auto", "started": time.time()}
            try:
                self._run_turn(w, request)
            except browser.RateLimitedError as exc:
                logger.warning("ChatGPT rate-limited (worker %d) — tripping cooldown.", w.id)
                try:
                    ratelimit.RATE.trip_cooldown()
                except Exception:
                    pass
                self._put_error(request, exc)
            except browser.StaleChatError:
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
                    logger.warning("Navigation race (worker %d) — retrying once.", w.id)
                    time.sleep(1.0)
                    try:
                        self._run_turn(w, request)
                    except Exception as exc2:
                        self._put_error(request, exc2)
                        logger.error("Retry after nav race failed (worker %d): %s", w.id, exc2)
                elif wedged:
                    logger.warning("Session wedged (worker %d) — recreating + retry.", w.id)
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
            try:
                w.session.close()
            except Exception:
                pass
        if removed:
            with self._workers_lock:
                if w in self._workers:
                    self._workers.remove(w)
            logger.info("Worker %d (%s) removed from the pool.", w.id, w.label)

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(WATCHDOG_INTERVAL)
            now = time.time()
            hard, soft = [], []
            for w in list(self._workers):
                inf = w.in_flight
                if not inf:
                    continue
                age = now - inf.get("started", now)
                if age > WATCHDOG_HARD_S:
                    hard.append(w)
                elif age > WATCHDOG_SOFT_S:
                    soft.append(w)
            if hard:
                logger.critical("Watchdog: a turn is hard-hung (>%.0fs) — %s", WATCHDOG_HARD_S,
                                "exiting for container restart" if WATCHDOG_HARD_EXIT else "restarting worker")
                if WATCHDOG_HARD_EXIT:
                    os._exit(1)
                for w in hard:
                    w.restart_req.set()   # targeted at the actually-stuck worker
            elif soft:
                if not self._degraded:
                    self._degraded = True
                    logger.warning("Watchdog: a turn is stuck (>%.0fs) — degraded.", WATCHDOG_SOFT_S)
                    for w in soft:
                        w.restart_req.set()
            else:
                self._degraded = False

    def _start_new_chat(self, w: Worker, temporary: bool = False, force: bool = False) -> None:
        assert w.session is not None
        page = w.session._page
        current = page.url
        target = "https://chatgpt.com/?temporary-chat=true" if temporary else "https://chatgpt.com"
        if not force and not temporary and current.rstrip("/") == "https://chatgpt.com":
            return
        if (not force and temporary and not FRESH_TEMPORARY_CHAT
                and "temporary-chat=true" in current and "/c/" not in current):
            return
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            browser._ensure_composer(page)
            try:
                page.evaluate(browser.FETCH_INTERCEPT_JS)
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
