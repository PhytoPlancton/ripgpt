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


@dataclass(slots=True)
class SessionRequest:
    prompt: str
    temporary: bool
    holder: dict | queue.Queue
    done_event: threading.Event
    stream: bool = False
    model_slug: str | None = None


class BrowserSessionService:
    def __init__(self, startup_timeout: float = 300.0):
        self._startup_timeout = startup_timeout
        self._session: browser.ChatSession | None = None
        self._request_queue: queue.Queue[SessionRequest | None] = queue.Queue()
        self._ready = threading.Event()
        self._worker: threading.Thread | None = None
        self._startup_error: Exception | None = None

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

    def stop(self) -> None:
        if not self._worker:
            return
        self._request_queue.put(None)
        self._worker.join(timeout=30)
        self._worker = None
        self._ready.clear()

    def ask(self, prompt: str, temporary: bool = False, model_slug: str | None = None, timeout: float = 330) -> str:
        self._ensure_ready()
        result: dict[str, str] = {}
        done_event = threading.Event()
        self._request_queue.put(SessionRequest(prompt=prompt, temporary=temporary, holder=result, done_event=done_event, model_slug=model_slug))
        if not done_event.wait(timeout):
            raise TimeoutError("Browser session timed out waiting for a response.")
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("answer", "")

    def stream(self, prompt: str, temporary: bool = False, model_slug: str | None = None) -> queue.Queue:
        self._ensure_ready()
        chunk_queue: queue.Queue = queue.Queue()
        done_event = threading.Event()
        self._request_queue.put(
            SessionRequest(prompt=prompt, temporary=temporary, holder=chunk_queue, done_event=done_event, stream=True, model_slug=model_slug)
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

            try:
                self._start_new_chat(temporary=request.temporary)
                if request.stream:
                    assert isinstance(request.holder, queue.Queue)
                    self._session.send(request.prompt, request.model_slug)
                    self._stream_answer_via_dom(self._session._page, request.holder)
                else:
                    assert isinstance(request.holder, dict)
                    answer = self._session.ask(request.prompt, request.model_slug)
                    request.holder["answer"] = answer
            except Exception as exc:
                if request.stream:
                    assert isinstance(request.holder, queue.Queue)
                    request.holder.put({"error": str(exc)})
                    request.holder.put(None)
                else:
                    assert isinstance(request.holder, dict)
                    request.holder["error"] = str(exc)
                logger.error("Session error: %s", exc)
            finally:
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