from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import queue
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Iterable

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, Response

from app.metrics import METRICS, classify_error
from app.dashboard import DASHBOARD_HTML, LOGIN_HTML, SETUP_HTML
from app.imagestore import IMAGES, ext_for
from app.keystore import KEYS
from app.ratelimit import RATE
from app import auth
from app.auth import (
    SESSION_COOKIE, CSRF_COOKIE, SESSION_TTL,
    admin_configured, check_login, make_session, read_session,
    issue_csrf, verify_csrf, client_ip, cookie_secure,
    login_locked, record_login_fail, reset_login_fails,
    bump_token_version,
)
from app.openai_models import (
    ChatCompletionRequest,
    CompletionRequest,
    apply_stop_sequences,
    extract_file_attachments,
    extract_last_user_message,
    FileInputError,
    generate_meta_response,
    is_meta_request,
    normalize_stop_sequences,
    prompt_from_completion_request,
    prompt_to_attachment_if_large,
    serialize_messages,
)
from app.session_service import BrowserSessionService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ripgpt.api")

API_KEY = os.environ.get("API_KEY", "")
# Hard ceiling on request body size (file uploads are base64, so ~135 MB covers the
# 100 MB cumulative file cap with overhead). Rejected at the edge before buffering.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(160 * 1024 * 1024)))
# Admission control: a single browser serves all turns serially, so an unbounded queue
# lets one caller starve everyone for minutes. Shed load with 503 + Retry-After once the
# backlog is this deep.
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "12"))


def _overloaded_response():
    return JSONResponse(
        status_code=503,
        content={"error": {"message": "Server busy — too many requests queued. Retry shortly.",
                           "type": "server_error", "code": "overloaded"}},
        headers={"Retry-After": "10"},
    )


def _queue_overloaded() -> bool:
    try:
        return SERVICE.queue_depth() >= MAX_QUEUE_DEPTH
    except Exception:
        return False


def _rate_limited_response(retry_after: int, reason: str):
    # Ban-protection: over a cap → 429; in an anti-ban cooldown → 503. Both carry Retry-After
    # so a well-behaved client (e.g. examples/batch.py) backs off instead of hammering ChatGPT.
    if reason == "cooldown":
        status, code, etype = 503, "cooldown", "server_error"
        msg = f"Anti-ban cooldown active — retry after {retry_after}s."
    else:
        status, code, etype = 429, "rate_limited", "invalid_request_error"
        msg = f"Rate limit ({reason}) — retry after {retry_after}s."
    return JSONResponse(status_code=status,
                        content={"error": {"message": msg, "type": etype, "code": code}},
                        headers={"Retry-After": str(retry_after)})


def _bearer(request: Request) -> str:
    auth_h = request.headers.get("Authorization", "")
    return auth_h[len("Bearer "):] if auth_h.startswith("Bearer ") else ""


def _authorize_request(request: Request) -> str:
    """Validate the client's API key against the key store and return its key_id.

    Fail-closed: an unknown/revoked/missing key is always rejected. There is no
    "empty API_KEY disables auth" escape hatch — if the store has no keys, nothing
    authenticates (the operator seeds one via API_KEY env or the admin console).
    """
    rec = KEYS.validate(_bearer(request))
    if not rec:
        raise HTTPException(status_code=401, detail={
            "message": "Invalid or missing API key.",
            "type": "invalid_request_error", "code": "invalid_api_key"})
    KEYS.touch(rec["id"])
    return rec["id"]


# Back-compat alias for the read-only model endpoints (they don't need the key_id).
def _require_api_key(request: Request) -> None:
    _authorize_request(request)


# ── admin session helpers ─────────────────────────────────────────────────────
def _admin_uid(request: Request) -> str | None:
    return read_session(request.cookies.get(SESSION_COOKIE))


def _require_admin(request: Request) -> str:
    uid = _admin_uid(request)
    if not uid:
        raise HTTPException(status_code=401, detail={
            "message": "Admin login required.",
            "type": "invalid_request_error", "code": "login_required"})
    return uid


def _check_csrf(request: Request) -> None:
    if not verify_csrf(request.cookies.get(CSRF_COOKIE), request.headers.get(auth.CSRF_HEADER)):
        raise HTTPException(status_code=403, detail={
            "message": "CSRF check failed.",
            "type": "invalid_request_error", "code": "csrf_failed"})


def _set_admin_cookies(resp: Response, request: Request, uid: str) -> None:
    secure = cookie_secure(request)
    resp.set_cookie(SESSION_COOKIE, make_session(uid), max_age=SESSION_TTL,
                    httponly=True, secure=secure, samesite="lax", path="/")
    # CSRF token must be readable by JS (double-submit), so NOT HttpOnly.
    resp.set_cookie(CSRF_COOKIE, issue_csrf(), max_age=SESSION_TTL,
                    httponly=False, secure=secure, samesite="lax", path="/")

SERVICE = BrowserSessionService()

# Models exposed to clients. Each maps to a ChatGPT model slug (forced per request by
# rewriting the outgoing request body — see browser.INTERCEPT_JS) and whether to use a
# temporary chat. slug=None → no override (ChatGPT's currently selected model).
# "auto" chooses a slug from the prompt (see _auto_pick_slug).
MODELS: dict[str, dict] = {
    "auto":             {"slug": None, "temporary": True, "auto": True},
    "gpt-5.5":          {"slug": "gpt-5-5",          "temporary": True},
    "gpt-5.5-instant":  {"slug": "gpt-5-5-instant",  "temporary": True},
    "gpt-5.5-thinking": {"slug": "gpt-5-5-thinking", "temporary": True},
    "gpt-5.4-thinking": {"slug": "gpt-5-4-thinking", "temporary": True},
    "gpt-5.3":          {"slug": "gpt-5-3",          "temporary": True},
    "o3":               {"slug": "o3",               "temporary": True},
    # persistent (saved-history) option, uses whatever model ChatGPT has selected
    "chatgpt":          {"slug": None, "temporary": False},
    # image generation: ChatGPT disables it in temporary chats, so this one is
    # persistent. The image backend (GPT Image) is shared across chat models; we
    # pin GPT-5.5 as the orchestrator so the image tool is reliably available.
    "gpt-image":        {"slug": "gpt-5-5", "temporary": False, "image": True},
}

# "auto" routing: a complex/technical ask → reasoning model; otherwise a fast one.
_AUTO_THINKING = os.environ.get("AUTO_THINKING_MODEL", "gpt-5-5-thinking")
_AUTO_FAST = os.environ.get("AUTO_FAST_MODEL", "gpt-5-5-instant")
_THINK_HINTS = (
    "code", "function", "bug", "error", "traceback", "stack trace", "regex", "sql",
    "algorithm", "optimi", "complexity", "prove", "proof", "theorem", "math",
    "calculate", "equation", "derive", "step by step", "step-by-step", "reasoning",
    "analyze", "explain in detail", "trade-off", "architecture", "debug", "refactor",
    "démontre", "calcule", "étape par étape", "raisonne", "analyse", "explique en détail",
    "pourquoi", "résous", "preuve", "équation", "compare", "comparer",
)


def _auto_pick_slug(prompt: str) -> str:
    text = prompt or ""
    low = text.lower()
    if len(text) > 700 or "```" in text or any(h in low for h in _THINK_HINTS):
        return _AUTO_THINKING
    return _AUTO_FAST


def _make_model_card(model_id: str) -> dict:
    return {"id": model_id, "object": "model", "created": 0, "owned_by": "ripgpt"}


def _error_response(message: str, status_code: int = 400, error_type: str = "invalid_request_error", code: str | None = None):
    error: dict[str, str] = {"message": message, "type": error_type}
    if code is not None:
        error["code"] = code
    return JSONResponse(status_code=status_code, content={"error": error})


def _html(content: str, status_code: int = 200):
    # Frame-protection so the admin console can't be clickjacked, plus a tight referrer.
    return HTMLResponse(content, status_code=status_code, headers={
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
        "Referrer-Policy": "no-referrer",
    })


def _safe_upstream_error(exc) -> str:
    """Log the full exception server-side; return a sanitized client message.

    Raw browser/Playwright exception text leaks internal selectors, timeouts, paths and
    env/cookie variable names — useful reconnaissance for an authenticated attacker. We
    expose only a stable category + an opaque ref id and keep the detail in the logs.
    Accepts an Exception or a raw error string.
    """
    ref = uuid.uuid4().hex[:12]
    log.error("upstream error ref=%s: %s", ref, exc, exc_info=isinstance(exc, BaseException))
    return f"Upstream request failed ({classify_error(str(exc))}). ref={ref}"


def _resolve_model(model: str) -> str | None:
    return model if model in MODELS else None


def _model_config(model: str, prompt: str) -> tuple[bool, str | None, bool]:
    """Return (temporary, slug, image) for a resolved model, resolving 'auto' from the prompt."""
    cfg = MODELS[model]
    slug = _auto_pick_slug(prompt) if cfg.get("auto") else cfg.get("slug")
    return cfg["temporary"], slug, bool(cfg.get("image"))


try:
    import tiktoken
    _ENCODER = tiktoken.get_encoding("o200k_base")   # GPT-4o/5-era vocab
    log.info("tiktoken loaded (o200k_base) for token counting.")
except Exception as _exc:  # pragma: no cover - offline / missing vocab
    _ENCODER = None
    logging.getLogger("ripgpt.api").warning("tiktoken unavailable (%s) — approximating tokens.", _exc)


def _count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENCODER is not None:
        return len(_ENCODER.encode(text))
    return max(1, len(text) // 4)   # ~4 chars/token approximation


# Public origin used to build image URLs. OpenWebUI calls us server-to-server, so the
# request Host is the internal docker address — that URL is unreachable from the user's
# browser. PUBLIC_BASE_URL (e.g. http://localhost:8850 or https://ripgpt.nmt.ovh) wins;
# we fall back to the request origin for callers that hit ripgpt directly.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

_DATA_URL_RE = re.compile(r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")


def _base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _sniff_image_mime(raw: bytes) -> str | None:
    """Detect a raster image type from its magic bytes — None for anything else.

    We host (and later serve from our own origin) ONLY genuine raster images. SVG and
    spoofed/arbitrary bytes are deliberately rejected: serving attacker-controlled
    image/svg+xml from the public /images route would execute script on our origin.
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _externalize_images(answer: str, base_url: str) -> tuple[str, int]:
    """Replace inline base64 image data URLs with short ripgpt-hosted URLs.

    Multi-MB data URLs render as raw text in OpenWebUI; a short URL renders as an image.
    Only real raster images are hosted; everything else is left inline (see _sniff).
    Returns (rewritten_answer, n_images_hosted).
    """
    if not answer or "data:image" not in answer:
        return answer, 0

    count = 0

    def repl(m: "re.Match") -> str:
        nonlocal count
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
        except Exception:
            return m.group(0)
        sniffed = _sniff_image_mime(raw)
        if not sniffed:
            return m.group(0)   # not a genuine raster image — leave inline, never host
        iid = IMAGES.put(raw, sniffed)   # trust the sniffed type, not the claimed mime
        count += 1
        return f"{base_url}/images/{iid}.{ext_for(sniffed)}"

    return _DATA_URL_RE.sub(repl, answer), count


def _make_usage(prompt: str, content: str) -> dict[str, int]:
    prompt_tokens = _count_tokens(prompt)
    completion_tokens = _count_tokens(content)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _make_chat_completion_response(completion_id: str, created: int, model: str, content: str, prompt: str) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": _make_usage(prompt, content),
    }


def _make_completion_response(completion_id: str, created: int, model: str, content: str, prompt: str) -> dict:
    return {
        "id": completion_id,
        "object": "text_completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "text": content,
                "index": 0,
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": _make_usage(prompt, content),
    }


def _iter_markdown_chunks(text: str) -> Iterable[str]:
    if not text:
        return

    in_code_block = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            yield line
            continue

        if in_code_block:
            yield line
            continue

        if not stripped:
            yield line
            continue

        words = line.split(" ")
        current = ""
        for index, word in enumerate(words):
            separator = "" if index == 0 else " "
            piece = separator + word
            if len(current) + len(piece) > 48 and current:
                yield current
                current = word
            else:
                current += piece

        if current:
            yield current


def _consume_stop_buffer(buffer: str, stop_sequences: list[str]) -> tuple[str, str, bool]:
    if not stop_sequences:
        return buffer, "", False

    stop_index: int | None = None
    for sequence in stop_sequences:
        index = buffer.find(sequence)
        if index == -1:
            continue
        if stop_index is None or index < stop_index:
            stop_index = index

    if stop_index is not None:
        return buffer[:stop_index], "", True

    max_stop_length = max(len(sequence) for sequence in stop_sequences)
    if max_stop_length <= 1 or len(buffer) <= max_stop_length - 1:
        return "", buffer, False

    split_index = len(buffer) - (max_stop_length - 1)
    return buffer[:split_index], buffer[split_index:], False


@asynccontextmanager
async def _lifespan(_: FastAPI):
    # Seed the legacy single API_KEY into the multi-key store so existing integrations
    # keep working; thereafter keys are managed from the admin console.
    KEYS.seed_from_env(API_KEY)
    if not KEYS.list():
        log.warning("No API keys configured — ALL /v1 requests will be rejected (401). "
                    "Set API_KEY in .env to seed one, or create keys in the admin console.")
    if not admin_configured():
        log.warning("ADMIN_USER / ADMIN_PASSWORD_HASH not set — admin console is LOCKED "
                    "(no login possible). Run `python -m app.adminpw` to generate a hash.")
    elif not auth.TRUST_PROXY_HEADERS and not PUBLIC_BASE_URL.lower().startswith("https"):
        log.warning("Admin is configured but PUBLIC_BASE_URL is not https and "
                    "TRUST_PROXY_HEADERS is off — admin cookies may be issued WITHOUT the "
                    "Secure flag. On a public HTTPS deploy set PUBLIC_BASE_URL=https://… "
                    "(and TRUST_PROXY_HEADERS=true behind a trusted tunnel).")
    SERVICE.start()
    if not SERVICE.wait_until_ready():
        raise RuntimeError("Browser session did not become ready in time.")
    if not SERVICE.is_ready():
        raise RuntimeError("Browser session failed to initialize.")
    try:
        yield
    finally:
        try:
            METRICS.save()   # flush all-time counters so a restart keeps usage/cost
        except Exception:
            pass
        SERVICE.stop()


class _BodyLimitMiddleware:
    """ASGI middleware enforcing a hard request-body byte ceiling.

    Buffers the body up to max_bytes and rejects with 413 once exceeded, regardless of
    how the size is declared. This closes the chunked/absent-Content-Length bypass of a
    header-only check, which could otherwise OOM the single-worker proxy pre-auth.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        # Fast path: honestly-declared oversize length.
        for name, value in scope.get("headers") or []:
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                return await self._reject(send)

        body = bytearray()
        over = False
        disconnected = False
        while True:
            message = await receive()
            mtype = message.get("type")
            if mtype == "http.request":
                body += message.get("body", b"")
                if len(body) > self.max_bytes:
                    over = True
                    break
                if not message.get("more_body", False):
                    break
            elif mtype == "http.disconnect":
                disconnected = True
                break
            else:
                break
        if over:
            return await self._reject(send)

        replayed = False

        async def replay():
            nonlocal replayed
            if disconnected:
                return {"type": "http.disconnect"}
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            # After replaying the buffered body, forward to the real receive() so genuine
            # client disconnects propagate. Synthesizing http.disconnect here would make
            # Starlette's StreamingResponse think the client hung up and cancel SSE output.
            return await receive()

        return await self.app(scope, replay, send)

    async def _reject(self, send):
        payload = json.dumps({"error": {
            "message": "Request body too large.",
            "type": "invalid_request_error", "code": "payload_too_large"}}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})


def create_app() -> FastAPI:
    app = FastAPI(title="RipGPT API", lifespan=_lifespan)
    # Hard body-size cap enforced by counting bytes (handles chunked / absent
    # Content-Length, which the old header-only check let through → OOM risk).
    app.add_middleware(_BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    @app.get("/v1/models")
    @app.get("/models")
    async def list_models(request: Request):
        _require_api_key(request)
        disabled = KEYS.disabled_models()
        return {"object": "list",
                "data": [_make_model_card(m) for m in MODELS if m not in disabled]}

    @app.get("/v1/models/{model}")
    @app.get("/models/{model}")
    async def retrieve_model(model: str, request: Request):
        _require_api_key(request)
        resolved = _resolve_model(model)
        if resolved is None or not KEYS.is_model_enabled(resolved):
            return _error_response(f"The model '{model}' does not exist.", status_code=404, error_type="invalid_request_error", code="model_not_found")
        return _make_model_card(resolved)

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat_completions(payload: ChatCompletionRequest, request: Request):
        key_id = _authorize_request(request)
        if os.environ.get("DEBUG_PAYLOAD"):
            for i, m in enumerate(payload.messages):
                c = m.content
                if isinstance(c, str):
                    log.info("[payload] msg[%d] role=%s str len=%d head=%r tail=%r", i, m.role, len(c), c[:220], c[-120:])
                elif isinstance(c, list):
                    kinds = [it.get("type") for it in c if isinstance(it, dict)]
                    log.info("[payload] msg[%d] role=%s list parts=%s", i, m.role, kinds)
                    for it in c:
                        if isinstance(it, dict) and it.get("type") == "text":
                            t = it.get("text", "")
                            log.info("[payload]   text len=%d head=%r tail=%r", len(t), t[:220], t[-120:])
                        elif isinstance(it, dict):
                            log.info("[payload]   part=%s keys=%s", it.get("type"), list(it.keys()))
        resolved_model = _resolve_model(payload.model)
        if resolved_model is None or not KEYS.is_model_enabled(resolved_model):
            return _error_response(f"The model '{payload.model}' does not exist.", status_code=404, error_type="invalid_request_error", code="model_not_found")
        if payload.n != 1:
            return _error_response("Only n=1 is supported.")

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if is_meta_request(payload.messages):
            meta_answer = generate_meta_response(payload.messages)
            return _make_chat_completion_response(completion_id, created, resolved_model, meta_answer, "")

        try:
            files = extract_file_attachments(payload.messages)
        except FileInputError as fe:
            return _error_response(str(fe), status_code=400, error_type="invalid_request_error", code=fe.code)

        prompt = serialize_messages(payload.messages)
        if not files:
            # OpenWebUI (full-context) path: lift a big injected document into a .md upload
            # instead of typing/truncating it.
            prompt, files = prompt_to_attachment_if_large(prompt)
        if not prompt and not files:
            return _error_response("No user message provided.")

        temporary, slug, image = _model_config(resolved_model, prompt)
        if files:
            temporary = False   # attachments are disabled in temporary chats
            if not prompt:
                prompt = "Please analyse the attached file(s)."

        if SERVICE.is_paused():
            return _error_response("Proxy is paused.", status_code=503, error_type="server_error", code="paused")
        if _queue_overloaded():
            return _overloaded_response()
        _rl_ok, _rl_wait, _rl_reason = RATE.allow()
        if not _rl_ok:
            return _rate_limited_response(_rl_wait, _rl_reason)

        if payload.stream:
            return StreamingResponse(
                _stream_chat_completion(
                    prompt=prompt,
                    completion_id=completion_id,
                    created=created,
                    model=resolved_model,
                    temporary=temporary,
                    model_slug=slug,
                    image=image,
                    files=files,
                    base_url=_base_url(request),
                    stop=payload.stop,
                    include_usage=bool(payload.stream_options and payload.stream_options.include_usage),
                    key_id=key_id,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        t0 = time.time()
        try:
            answer = SERVICE.ask(prompt, temporary=temporary, model_slug=slug, image=image, files=files)
        except Exception as exc:
            METRICS.record(model_req=resolved_model, model_res=slug, status="error",
                           error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000),
                           key_id=key_id)
            return _error_response(_safe_upstream_error(exc), status_code=500, error_type="server_error")

        latency_ms = int((time.time() - t0) * 1000)
        answer, n_img = _externalize_images(answer, _base_url(request))
        answer = apply_stop_sequences(answer, payload.stop)
        ok = bool(answer.strip())
        METRICS.record(model_req=resolved_model, model_res=slug, status="ok" if ok else "error",
                       error_class=None if ok else "empty_reply", latency_ms=latency_ms,
                       ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer), images=n_img,
                       key_id=key_id)
        return _make_chat_completion_response(completion_id, created, resolved_model, answer, prompt)

    @app.post("/v1/completions")
    @app.post("/completions")
    async def completions(payload: CompletionRequest, request: Request):
        key_id = _authorize_request(request)
        resolved_model = _resolve_model(payload.model)
        if resolved_model is None or not KEYS.is_model_enabled(resolved_model):
            return _error_response(f"The model '{payload.model}' does not exist.", status_code=404, error_type="invalid_request_error", code="model_not_found")
        if payload.n != 1:
            return _error_response("Only n=1 is supported.")

        prompt = prompt_from_completion_request(payload.prompt)
        if not prompt:
            return _error_response("No prompt provided.")

        completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        temporary, slug, image = _model_config(resolved_model, prompt)

        if SERVICE.is_paused():
            return _error_response("Proxy is paused.", status_code=503, error_type="server_error", code="paused")
        if _queue_overloaded():
            return _overloaded_response()
        _rl_ok, _rl_wait, _rl_reason = RATE.allow()
        if not _rl_ok:
            return _rate_limited_response(_rl_wait, _rl_reason)

        if payload.stream:
            return StreamingResponse(
                _stream_completion(
                    prompt=prompt,
                    completion_id=completion_id,
                    created=created,
                    model=resolved_model,
                    temporary=temporary,
                    model_slug=slug,
                    image=image,
                    base_url=_base_url(request),
                    stop=payload.stop,
                    key_id=key_id,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        t0 = time.time()
        try:
            answer = SERVICE.ask(prompt, temporary=temporary, model_slug=slug, image=image)
        except Exception as exc:
            METRICS.record(model_req=resolved_model, model_res=slug, status="error",
                           error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000),
                           key_id=key_id)
            return _error_response(_safe_upstream_error(exc), status_code=500, error_type="server_error")

        latency_ms = int((time.time() - t0) * 1000)
        answer, n_img = _externalize_images(answer, _base_url(request))
        answer = apply_stop_sequences(answer, payload.stop)
        ok = bool(answer.strip())
        METRICS.record(model_req=resolved_model, model_res=slug, status="ok" if ok else "error",
                       error_class=None if ok else "empty_reply", latency_ms=latency_ms,
                       ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer), images=n_img,
                       key_id=key_id)
        return _make_completion_response(completion_id, created, resolved_model, answer, prompt)

    @app.get("/health")
    async def health():
        return {"status": "ok", "session_ready": SERVICE.is_ready()}

    @app.get("/images/{iid}")
    async def get_image(iid: str):
        # No API key: an <img> tag can't send Authorization headers. The 32-char
        # uuid is the unguessable capability; images are ephemeral (TTL + cap).
        got = IMAGES.get(iid.split(".")[0])
        if not got:
            return _error_response("Image not found or expired.", status_code=404,
                                   error_type="invalid_request_error", code="not_found")
        data, mime = got
        # Stored mime is always a sniffed raster type; nosniff + inline disposition are
        # defence-in-depth so the browser never treats served bytes as an active document.
        # Cache no longer than the in-memory TTL so dead URLs aren't trusted for 24h.
        return Response(content=data, media_type=mime, headers={
            "Cache-Control": "public, max-age=10800",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{iid.split(".")[0]}.{ext_for(mime)}"',
        })

    # ── admin console (gated behind login) ────────────────────────────────────
    @app.get("/")
    @app.get("/dashboard")
    async def dashboard(request: Request):
        # Fail-closed: with no admin creds the console is locked behind a setup notice;
        # with creds but no valid session, the login page; only a live session sees data.
        if not admin_configured():
            return _html(SETUP_HTML)
        if not _admin_uid(request):
            return _html(LOGIN_HTML, status_code=401)
        return _html(DASHBOARD_HTML)

    @app.get("/login")
    async def login_page(request: Request):
        if not admin_configured():
            return _html(SETUP_HTML)
        if _admin_uid(request):
            return _html(DASHBOARD_HTML)
        return _html(LOGIN_HTML)

    @app.post("/admin/login")
    async def admin_login(request: Request):
        if not admin_configured():
            return _error_response("Admin console is not configured.", status_code=503,
                                   error_type="server_error", code="admin_not_configured")
        ip = client_ip(request)
        rem = login_locked(ip)
        if rem > 0:
            return _error_response(f"Too many attempts. Try again in {int(rem)}s.",
                                   status_code=429, error_type="invalid_request_error",
                                   code="rate_limited")
        try:
            body = await request.json()
        except Exception:
            body = {}
        user = str(body.get("username", ""))
        pw = str(body.get("password", ""))
        if not check_login(user, pw):
            record_login_fail(ip)
            return _error_response("Invalid username or password.", status_code=401,
                                   error_type="invalid_request_error", code="invalid_credentials")
        reset_login_fails(ip)
        resp = JSONResponse({"ok": True})
        _set_admin_cookies(resp, request, user)
        return resp

    @app.post("/admin/logout")
    async def admin_logout(request: Request):
        _require_admin(request)
        _check_csrf(request)
        # Stateless tokens can't be individually revoked; bumping the token_version
        # invalidates ALL outstanding sessions server-side (single-admin tool, so a
        # global logout is the desired behaviour — a stolen cookie stops working now).
        bump_token_version()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        resp.delete_cookie(CSRF_COOKIE, path="/")
        return resp

    @app.get("/stats")
    async def stats(request: Request):
        # Session-only: the snapshot includes per-key usage of ALL keys, so it must not
        # be reachable with a single data-plane API key (cross-tenant disclosure).
        _require_admin(request)
        snap = METRICS.snapshot()
        snap["live"] = SERVICE.live_state()
        snap["rate"] = RATE.snapshot()
        return snap

    @app.post("/admin/restart-session")
    async def admin_restart(request: Request):
        # Operational kill/restart controls are admin-session only (a data-plane key must
        # not be able to restart or pause the shared browser for everyone).
        _require_admin(request)
        _check_csrf(request)
        ok = await asyncio.to_thread(SERVICE.request_restart)
        return {"restarted": bool(ok)}

    @app.post("/admin/pause")
    async def admin_pause(request: Request):
        _require_admin(request)
        _check_csrf(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        SERVICE.set_paused(bool(body.get("paused", not SERVICE.is_paused())))
        return {"paused": SERVICE.is_paused()}

    # ── API key management (session-only + CSRF on mutations) ──────────────────
    @app.get("/admin/keys")
    async def admin_keys_list(request: Request):
        _require_admin(request)
        return {"keys": KEYS.list()}

    @app.post("/admin/keys")
    async def admin_keys_create(request: Request):
        _require_admin(request)
        _check_csrf(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str(body.get("name", "")).strip()
        secret, record = KEYS.create(name)
        # The plaintext secret is returned exactly once here and never stored.
        return {"key": secret, "record": record}

    @app.post("/admin/keys/{kid}/revoke")
    async def admin_keys_revoke(kid: str, request: Request):
        _require_admin(request)
        _check_csrf(request)
        return {"revoked": KEYS.revoke(kid)}

    # ── model enable/disable ───────────────────────────────────────────────────
    @app.get("/admin/models")
    async def admin_models_list(request: Request):
        _require_admin(request)
        disabled = KEYS.disabled_models()
        return {"models": [
            {"id": m, "enabled": m not in disabled,
             "image": bool(cfg.get("image")), "temporary": bool(cfg.get("temporary"))}
            for m, cfg in MODELS.items()
        ]}

    @app.post("/admin/models/toggle")
    async def admin_models_toggle(request: Request):
        _require_admin(request)
        _check_csrf(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = str(body.get("model", ""))
        enabled = bool(body.get("enabled", True))
        if model not in MODELS:
            return _error_response(f"Unknown model '{model}'.", status_code=404,
                                   error_type="invalid_request_error", code="model_not_found")
        KEYS.set_model_disabled(model, not enabled)
        return {"model": model, "enabled": enabled}

    # ── test prompt from the console ───────────────────────────────────────────
    @app.post("/admin/test")
    async def admin_test(request: Request):
        _require_admin(request)
        _check_csrf(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        model = str(body.get("model", "auto"))
        prompt = str(body.get("prompt", "")).strip()
        resolved = _resolve_model(model)
        if resolved is None or not KEYS.is_model_enabled(resolved):
            return _error_response(f"The model '{model}' is unavailable.", status_code=404,
                                   error_type="invalid_request_error", code="model_not_found")
        if not prompt:
            return _error_response("No prompt provided.")
        if len(prompt) > 8000:
            return _error_response("Test prompt too long (max 8000 chars).", status_code=413,
                                   error_type="invalid_request_error", code="payload_too_large")
        if SERVICE.is_paused():
            return _error_response("Proxy is paused.", status_code=503,
                                   error_type="server_error", code="paused")
        _rl_ok, _rl_wait, _rl_reason = RATE.allow()
        if not _rl_ok:
            return _rate_limited_response(_rl_wait, _rl_reason)
        temporary, slug, image = _model_config(resolved, prompt)
        t0 = time.time()
        try:
            answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary, slug, image, None)
        except Exception as exc:
            METRICS.record(model_req=resolved, model_res=slug, status="error",
                           error_class=classify_error(str(exc)),
                           latency_ms=int((time.time() - t0) * 1000), key_id="console")
            return _error_response(_safe_upstream_error(exc), status_code=500, error_type="server_error")
        latency_ms = int((time.time() - t0) * 1000)
        answer, n_img = _externalize_images(answer, _base_url(request))
        ok = bool(answer.strip())
        METRICS.record(model_req=resolved, model_res=slug, status="ok" if ok else "error",
                       error_class=None if ok else "empty_reply", latency_ms=latency_ms,
                       ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer), images=n_img,
                       key_id="console")
        return {"model": resolved, "answer": answer, "latency_ms": latency_ms, "images": n_img}

    # ── usage export ───────────────────────────────────────────────────────────
    @app.get("/admin/usage.csv")
    async def admin_usage_csv(request: Request):
        _require_admin(request)
        import csv
        import io
        snap = METRICS.snapshot()
        names = {k["id"]: k.get("name", "") for k in KEYS.list()}

        def _safe_cell(v):
            # Neutralise spreadsheet formula injection: a leading =,+,-,@ (or tab/CR) in a
            # text cell can execute when the CSV is opened in Excel/Sheets.
            s = "" if v is None else str(v)
            return ("'" + s) if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["section", "name", "requests", "ok", "err", "prompt_tokens",
                    "completion_tokens", "images", "est_api_cost_usd", "last_used_unix"])
        for m in snap.get("by_model_usage", []):
            w.writerow(["model", _safe_cell(m["model"]), m["requests"], m["ok"], m["err"],
                        m["ptoks"], m["ctoks"], m.get("images", 0),
                        m.get("cost", 0.0), m.get("last_ts") or ""])
        for k in snap.get("by_key_usage", []):
            label = names.get(k["key_id"], k["key_id"])
            w.writerow(["key", _safe_cell(f"{label} ({k['key_id']})"), k["requests"], k["ok"], k["err"],
                        k["ptoks"], k["ctoks"], k.get("images", 0),
                        k.get("cost", 0.0), k.get("last_ts") or ""])
        return Response(content=buf.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="ripgpt-usage.csv"'})

    return app


def _chunk_text(text: str, size: int = 24):
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def _stream_chat_completion(
    prompt: str,
    completion_id: str,
    created: int,
    model: str,
    temporary: bool,
    model_slug: str | None,
    image: bool,
    files: list | None,
    base_url: str,
    stop: str | list[str] | None,
    include_usage: bool,
    key_id: str | None = None,
):
    # OpenWebUI and most clients send stream=true. ripgpt's reliable path is the
    # non-streaming browser capture (answer delivered over the WebSocket), so we fetch
    # the full answer that way and re-chunk it into SSE deltas — robust, streaming-feel.
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"

    def _delta(content: str) -> str:
        return "data: " + json.dumps({
            "id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }) + "\n\n"

    def _tail(answer_text: str):
        out = ["data: " + json.dumps({
            "id": completion_id, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }) + "\n\n"]
        if include_usage:
            out.append("data: " + json.dumps({
                "id": completion_id, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [], "usage": _make_usage(prompt, answer_text),
            }) + "\n\n")
        out.append("data: [DONE]\n\n")
        return out

    t0 = time.time()

    # Plain text → stream LIVE from the DOM (reads only the .markdown answer, skips the
    # "Thinking" phase, and ignores any lingering previous answer via a baseline). Image/
    # file turns can't token-stream (image is appended only at the end) → full capture below.
    if not image and not files:
        try:
            chunk_queue = await asyncio.to_thread(SERVICE.stream, prompt, temporary, model_slug)
        except Exception as exc:
            METRICS.record(model_req=model, model_res=model_slug, status="error",
                           error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000),
                           key_id=key_id)
            yield _delta(f"[Error: {_safe_upstream_error(exc)}]")
            yield "data: [DONE]\n\n"
            return
        parts: list[str] = []
        while True:
            item = await asyncio.to_thread(chunk_queue.get)
            if item is None:
                break
            if isinstance(item, dict) and item.get("error"):
                METRICS.record(model_req=model, model_res=model_slug, status="error",
                               error_class=classify_error(str(item["error"])), latency_ms=int((time.time() - t0) * 1000),
                               key_id=key_id)
                yield _delta(f"[Error: {_safe_upstream_error(item['error'])}]")
                yield "data: [DONE]\n\n"
                return
            parts.append(item)
            yield _delta(item)
        answer = "".join(parts)
        _ok = bool(answer.strip())
        METRICS.record(model_req=model, model_res=model_slug, status="ok" if _ok else "error",
                       error_class=None if _ok else "empty_reply", latency_ms=int((time.time() - t0) * 1000),
                       ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer), key_id=key_id)
        for frame in _tail(answer):
            yield frame
        return

    t0 = time.time()
    try:
        answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary, model_slug, image, files)
    except Exception as exc:
        METRICS.record(model_req=model, model_res=model_slug, status="error",
                       error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000),
                       key_id=key_id)
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": f"[Error: {_safe_upstream_error(exc)}]"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        return

    answer, n_img = _externalize_images(answer, base_url)
    answer = apply_stop_sequences(answer, stop)
    _ok = bool(answer.strip())
    METRICS.record(model_req=model, model_res=model_slug, status="ok" if _ok else "error",
                   error_class=None if _ok else "empty_reply", latency_ms=int((time.time() - t0) * 1000),
                   ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer), images=n_img,
                   key_id=key_id)
    for piece in _chunk_text(answer):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"

    done_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"

    if include_usage:
        usage_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": _make_usage(prompt, answer),
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n"

    yield "data: [DONE]\n\n"


async def _stream_completion(
    prompt: str,
    completion_id: str,
    created: int,
    model: str,
    temporary: bool,
    model_slug: str | None,
    image: bool,
    base_url: str,
    stop: str | list[str] | None,
    key_id: str | None = None,
):
    t0 = time.time()
    try:
        answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary, model_slug, image)
    except Exception as exc:
        METRICS.record(model_req=model, model_res=model_slug, status="error",
                       error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000),
                       key_id=key_id)
        error_chunk = {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [{"text": f"[Error: {_safe_upstream_error(exc)}]", "index": 0, "logprobs": None, "finish_reason": None}],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        return

    answer, n_img = _externalize_images(answer, base_url)
    answer = apply_stop_sequences(answer, stop)
    _ok = bool(answer.strip())
    METRICS.record(model_req=model, model_res=model_slug, status="ok" if _ok else "error",
                   error_class=None if _ok else "empty_reply", latency_ms=int((time.time() - t0) * 1000),
                   ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer), images=n_img,
                   key_id=key_id)
    for piece in _iter_markdown_chunks(answer):
        chunk = {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [{"text": piece, "index": 0, "logprobs": None, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"

    done_chunk = {
        "id": completion_id,
        "object": "text_completion",
        "created": created,
        "model": model,
        "choices": [{"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(done_chunk)}\n\n"
    yield "data: [DONE]\n\n"


app = create_app()


def run() -> None:
    import uvicorn

    port = int(os.environ.get("API_PORT", "8850"))
    log.info("CHATGPT_SESSION_TOKEN=%s", "set" if os.environ.get("CHATGPT_SESSION_TOKEN") else "NOT SET (anonymous mode)")
    log.info("API_KEY env=%s (seeds the key store)", "set" if os.environ.get("API_KEY") else "NOT SET")
    log.info("Admin console=%s", "configured" if admin_configured() else "LOCKED (set ADMIN_USER/ADMIN_PASSWORD_HASH)")
    log.info("Starting RipGPT API on port %s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)