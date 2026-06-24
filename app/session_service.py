from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass

from . import browser


logger = logging.getLogger("ripgpt.session")

# How often (in seconds) to verify the ChatGPT session is still active.
# Defaults to 15 minutes; override via SESSION_CHECK_INTERVAL env var.
SESSION_CHECK_INTERVAL = int(os.environ.get("SESSION_CHECK_INTERVAL", "900"))

# File turns (upload + ChatGPT ingest of large docs) can take minutes — longer budget.
# Must exceed browser FILE_UPLOAD_TIMEOUT(180) + FILE_ANSWER_TIMEOUT(540) so the client
# wait never expires while the worker is still legitimately processing.
FILE_TURN_TIMEOUT = int(os.environ.get("FILE_TURN_TIMEOUT", "840"))
DEFAULT_TURN_TIMEOUT = 330


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
        self._session: browser.ChatSession | None = None
        self._request_queue: queue.Queue[SessionRequest | None] = queue.Queue()
        self._ready = threading.Event()
        self._worker: threading.Thread | None = None
        self._startup_error: Exception | None = None
        # ── monitoring state ──
        self._proxy_start_ts = time.time()
        self._browser_start_ts: float | None = None
        self._restart_count = 0
        self._in_flight: dict | None = None
        self._paused = False

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        self._startup_error = None
        self._ready.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="ripgpt-browser", daemon=True)
        self._worker.start()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout or self._startup_timeout)

    def is_ready(self) -> bool:
        return self._ready.is_set() and self._startup_error is None

    # ── monitoring / control ──────────────────────────────────────────────
    def queue_depth(self) -> int:
        return self._request_queue.qsize()

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)

    def request_restart(self, timeout: float = 120) -> bool:
        """Ask the worker to recreate the browser session (one-click recovery)."""
        self._ensure_ready()
        done = threading.Event()
        self._request_queue.put(SessionRequest(prompt="", temporary=False, holder={}, done_event=done, control="restart"))
        return done.wait(timeout)

    def _session_state(self) -> str:
        if self._startup_error is not None:
            return "browser_dead"
        if not self._ready.is_set() or self._session is None:
            return "starting"
        if getattr(self._session, "logged_out", False):
            return "logged_out"
        return "logged_in"

    def live_state(self) -> dict:
        now = time.time()
        inf = self._in_flight
        in_flight = None
        if inf:
            in_flight = {"model": inf.get("model"), "age_s": round(now - inf.get("started", now), 1)}
        return {
            "session_state": self._session_state(),
            "queue_depth": self.queue_depth(),
            "in_flight": in_flight,
            "proxy_uptime_s": round(now - self._proxy_start_ts),
            "browser_uptime_s": round(now - self._browser_start_ts) if self._browser_start_ts else None,
            "restart_count": self._restart_count,
            "paused": self._paused,
        }

    def _do_restart(self) -> None:
        logger.info("Restarting browser session (requested)...")
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass
        try:
            self._session = browser.ChatSession()
            self._browser_start_ts = time.time()
            self._restart_count += 1
            self._startup_error = None
            logger.info("Browser session restarted.")
        except Exception as exc:
            self._startup_error = exc
            logger.exception("Browser restart failed.")

    def _run_turn(self, request: "SessionRequest") -> None:
        """Execute one chat turn (stream or not). Raises on failure (e.g. wedged composer)."""
        self._start_new_chat(temporary=request.temporary)
        if request.stream:
            assert isinstance(request.holder, queue.Queue)
            self._session.send(request.prompt, request.model_slug, image=request.image, files=request.files)
            self._stream_answer_via_dom(self._session._page, request.holder)
        else:
            assert isinstance(request.holder, dict)
            request.holder["answer"] = self._session.ask(request.prompt, request.model_slug, image=request.image, files=request.files)

    def _put_error(self, request: "SessionRequest", exc: Exception) -> None:
        if request.stream:
            assert isinstance(request.holder, queue.Queue)
            request.holder.put({"error": str(exc)})
            request.holder.put(None)
        else:
            assert isinstance(request.holder, dict)
            request.holder["error"] = str(exc)

    def stop(self) -> None:
        if not self._worker:
            return
        self._request_queue.put(None)
        self._worker.join(timeout=30)
        self._worker = None
        self._ready.clear()

    def ask(self, prompt: str, temporary: bool = False, model_slug: str | None = None, image: bool = False, files: list | None = None, timeout: float | None = None) -> str:
        self._ensure_ready()
        if timeout is None:
            timeout = FILE_TURN_TIMEOUT if files else DEFAULT_TURN_TIMEOUT
        result: dict[str, str] = {}
        done_event = threading.Event()
        self._request_queue.put(SessionRequest(prompt=prompt, temporary=temporary, holder=result, done_event=done_event, model_slug=model_slug, image=image, files=files))
        if not done_event.wait(timeout):
            raise TimeoutError("Browser session timed out waiting for a response.")
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("answer", "")

    def stream(self, prompt: str, temporary: bool = False, model_slug: str | None = None, image: bool = False, files: list | None = None) -> queue.Queue:
        self._ensure_ready()
        chunk_queue: queue.Queue = queue.Queue()
        done_event = threading.Event()
        self._request_queue.put(
            SessionRequest(prompt=prompt, temporary=temporary, holder=chunk_queue, done_event=done_event, stream=True, model_slug=model_slug, image=image, files=files)
        )
        return chunk_queue

    def _ensure_ready(self) -> None:
        if not self.wait_until_ready():
            raise TimeoutError("Browser session did not become ready in time.")
        if self._startup_error is not None:
            raise RuntimeError(f"Browser session failed to start: {self._startup_error}")

    def _check_and_restore_session(self) -> None:
        if self._session is None:
            return
        logger.info("Checking session health...")
        try:
            if not self._session.is_alive():
                logger.warning("Session expired — re-logging in...")
                self._session.relogin()
                logger.info("Session restored successfully.")
            else:
                logger.info("Session is alive.")
        except Exception as exc:
            logger.error("Session health check/restore failed: %s", exc)

    def _worker_loop(self) -> None:
        logger.info("Starting browser session...")
        try:
            self._session = browser.ChatSession()
            self._browser_start_ts = time.time()
        except Exception as exc:
            self._startup_error = exc
            logger.exception("Browser session startup failed.")
            self._ready.set()
            return

        self._ready.set()
        logger.info("Browser session ready — accepting requests.")

        last_check = time.time()

        while True:
            # Block until a request arrives or the health-check interval elapses
            time_until_check = max(0.1, SESSION_CHECK_INTERVAL - (time.time() - last_check))
            try:
                request = self._request_queue.get(timeout=time_until_check)
            except queue.Empty:
                # Interval elapsed with no requests — run health check
                self._check_and_restore_session()
                last_check = time.time()
                continue

            if request is None:
                break

            if request.control == "restart":
                self._do_restart()
                request.done_event.set()
                last_check = time.time()
                continue

            self._in_flight = {"model": request.model_slug or "auto", "started": time.time()}
            try:
                self._run_turn(request)
            except Exception as exc:
                # Self-heal: a wedged composer (timeout waiting for #prompt-textarea) means
                # the browser is stuck — recreate it and retry the turn once, instead of
                # requiring a manual `docker compose restart api`.
                # A composer wedge (#prompt-textarea timeout) is a real stuck-browser
                # state even on file turns — recover and retry once (re-upload is safe;
                # legit slow upload/ingest waits don't raise, so they won't false-trip).
                wedged = ("prompt-textarea" in str(exc)) or ("Timeout" in str(exc)) or ("composer" in str(exc).lower())
                if wedged:
                    logger.warning("Session wedged (%s) — recreating browser and retrying once.", str(exc)[:80])
                    self._do_restart()
                    try:
                        self._run_turn(request)
                    except Exception as exc2:
                        self._put_error(request, exc2)
                        logger.error("Retry after restart failed: %s", exc2)
                else:
                    self._put_error(request, exc)
                    logger.error("Session error: %s", exc)
            finally:
                self._in_flight = None
                request.done_event.set()
                # Session was just used — reset the health-check timer
                last_check = time.time()

        logger.info("Shutting down browser session...")
        if self._session is not None:
            self._session.close()

    def _start_new_chat(self, temporary: bool = False) -> None:
        assert self._session is not None
        page = self._session._page
        current = page.url
        target = "https://chatgpt.com/?temporary-chat=true" if temporary else "https://chatgpt.com"

        if not temporary and current.rstrip("/") == "https://chatgpt.com":
            return
        if temporary and "temporary-chat=true" in current and "/c/" not in current:
            return

        try:
            page.goto(target, wait_until="domcontentloaded", timeout=30_000)
            page.evaluate(browser.FETCH_INTERCEPT_JS)
            browser._ensure_composer(page)
        except Exception as exc:
            logger.warning("Could not start new chat: %s", exc)

    def _stream_answer_via_dom(self, page, chunk_queue: queue.Queue) -> None:
        sent = ""
        previous_safe = ""
        deadline = time.time() + browser.ANSWER_TIMEOUT
        last_markdown = ""
        last_change = time.time()
        started = False
        time.sleep(0.5)

        while time.time() < deadline:
            # Real completion comes from the WebSocket interceptor; the fetch handoff
            # (__sse_done) fires too early now that answers stream over the socket.
            done = bool(page.evaluate("() => !!window.__answer_done"))
            started = started or bool(page.evaluate("() => !!window.__turn_started"))
            current_markdown = browser._read_answer_from_dom(page)
            safe_prefix = self._stream_safe_prefix(current_markdown, done=False)

            if previous_safe:
                stable_length = self._common_prefix_len(previous_safe, safe_prefix)
                if stable_length > len(sent):
                    chunk_queue.put(previous_safe[len(sent):stable_length])
                    sent = previous_safe[:stable_length]

            previous_safe = safe_prefix

            # DOM-stability fallback if completion markers ever change again.
            if current_markdown != last_markdown:
                last_markdown = current_markdown
                last_change = time.time()
            if not done and started and current_markdown and not browser._is_generating(page) \
                    and (time.time() - last_change) > browser.DOM_STABLE_SECS:
                done = True

            if done:
                time.sleep(0.4)
                final_markdown = browser._read_answer_from_dom(page)
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