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
from app.dashboard import DASHBOARD_HTML
from app.imagestore import IMAGES, ext_for
from app.openai_models import (
    ChatCompletionRequest,
    CompletionRequest,
    apply_stop_sequences,
    extract_last_user_message,
    generate_meta_response,
    is_meta_request,
    normalize_stop_sequences,
    prompt_from_completion_request,
)
from app.session_service import BrowserSessionService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ripgpt.api")

API_KEY = os.environ.get("API_KEY", "")


def _require_api_key(request: Request) -> None:
    if not API_KEY:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != API_KEY:
        raise HTTPException(status_code=401, detail={"message": "Invalid or missing API key.", "type": "invalid_request_error", "code": "invalid_api_key"})

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


def _resolve_model(model: str) -> str | None:
    return model if model in MODELS else None


def _model_config(model: str, prompt: str) -> tuple[bool, str | None, bool]:
    """Return (temporary, slug, image) for a resolved model, resolving 'auto' from the prompt."""
    cfg = MODELS[model]
    slug = _auto_pick_slug(prompt) if cfg.get("auto") else cfg.get("slug")
    return cfg["temporary"], slug, bool(cfg.get("image"))


def _count_tokens(text: str) -> int:
    return len(text.split())


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


def _externalize_images(answer: str, base_url: str) -> str:
    """Replace inline base64 image data URLs with short ripgpt-hosted URLs.

    Multi-MB data URLs render as raw text in OpenWebUI; a short URL renders as an image.
    Only real raster images are hosted; everything else is left inline (see _sniff).
    """
    if not answer or "data:image" not in answer:
        return answer

    def repl(m: "re.Match") -> str:
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)))
        except Exception:
            return m.group(0)
        sniffed = _sniff_image_mime(raw)
        if not sniffed:
            return m.group(0)   # not a genuine raster image — leave inline, never host
        iid = IMAGES.put(raw, sniffed)   # trust the sniffed type, not the claimed mime
        return f"{base_url}/images/{iid}.{ext_for(sniffed)}"

    return _DATA_URL_RE.sub(repl, answer)


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
    SERVICE.start()
    if not SERVICE.wait_until_ready():
        raise RuntimeError("Browser session did not become ready in time.")
    if not SERVICE.is_ready():
        raise RuntimeError("Browser session failed to initialize.")
    try:
        yield
    finally:
        SERVICE.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="RipGPT API", lifespan=_lifespan)

    @app.get("/v1/models")
    @app.get("/models")
    async def list_models(request: Request):
        _require_api_key(request)
        return {"object": "list", "data": [_make_model_card(m) for m in MODELS]}

    @app.get("/v1/models/{model}")
    @app.get("/models/{model}")
    async def retrieve_model(model: str, request: Request):
        _require_api_key(request)
        resolved = _resolve_model(model)
        if resolved is None:
            return _error_response(f"The model '{model}' does not exist.", status_code=404, error_type="invalid_request_error", code="model_not_found")
        return _make_model_card(resolved)

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat_completions(payload: ChatCompletionRequest, request: Request):
        _require_api_key(request)
        resolved_model = _resolve_model(payload.model)
        if resolved_model is None:
            return _error_response(f"The model '{payload.model}' does not exist.", status_code=404, error_type="invalid_request_error", code="model_not_found")
        if payload.n != 1:
            return _error_response("Only n=1 is supported.")

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        if is_meta_request(payload.messages):
            meta_answer = generate_meta_response(payload.messages)
            return _make_chat_completion_response(completion_id, created, resolved_model, meta_answer, "")

        prompt = extract_last_user_message(payload.messages)
        if not prompt:
            return _error_response("No user message provided.")

        temporary, slug, image = _model_config(resolved_model, prompt)

        if SERVICE.is_paused():
            return _error_response("Proxy is paused.", status_code=503, error_type="server_error", code="paused")

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
                    base_url=_base_url(request),
                    stop=payload.stop,
                    include_usage=bool(payload.stream_options and payload.stream_options.include_usage),
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        t0 = time.time()
        try:
            answer = SERVICE.ask(prompt, temporary=temporary, model_slug=slug, image=image)
        except Exception as exc:
            METRICS.record(model_req=resolved_model, model_res=slug, status="error",
                           error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000))
            return _error_response(str(exc), status_code=500, error_type="server_error")

        latency_ms = int((time.time() - t0) * 1000)
        answer = _externalize_images(answer, _base_url(request))
        answer = apply_stop_sequences(answer, payload.stop)
        ok = bool(answer.strip())
        METRICS.record(model_req=resolved_model, model_res=slug, status="ok" if ok else "error",
                       error_class=None if ok else "empty_reply", latency_ms=latency_ms,
                       ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer))
        return _make_chat_completion_response(completion_id, created, resolved_model, answer, prompt)

    @app.post("/v1/completions")
    @app.post("/completions")
    async def completions(payload: CompletionRequest, request: Request):
        _require_api_key(request)
        resolved_model = _resolve_model(payload.model)
        if resolved_model is None:
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
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        t0 = time.time()
        try:
            answer = SERVICE.ask(prompt, temporary=temporary, model_slug=slug, image=image)
        except Exception as exc:
            METRICS.record(model_req=resolved_model, model_res=slug, status="error",
                           error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000))
            return _error_response(str(exc), status_code=500, error_type="server_error")

        latency_ms = int((time.time() - t0) * 1000)
        answer = _externalize_images(answer, _base_url(request))
        answer = apply_stop_sequences(answer, payload.stop)
        ok = bool(answer.strip())
        METRICS.record(model_req=resolved_model, model_res=slug, status="ok" if ok else "error",
                       error_class=None if ok else "empty_reply", latency_ms=latency_ms,
                       ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer))
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

    @app.get("/")
    @app.get("/dashboard")
    async def dashboard():
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/stats")
    async def stats(request: Request):
        _require_api_key(request)
        snap = METRICS.snapshot()
        snap["live"] = SERVICE.live_state()
        return snap

    @app.post("/admin/restart-session")
    async def admin_restart(request: Request):
        _require_api_key(request)
        ok = await asyncio.to_thread(SERVICE.request_restart)
        return {"restarted": bool(ok)}

    @app.post("/admin/pause")
    async def admin_pause(request: Request):
        _require_api_key(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        SERVICE.set_paused(bool(body.get("paused", not SERVICE.is_paused())))
        return {"paused": SERVICE.is_paused()}

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
    base_url: str,
    stop: str | list[str] | None,
    include_usage: bool,
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

    t0 = time.time()
    try:
        answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary, model_slug, image)
    except Exception as exc:
        METRICS.record(model_req=model, model_res=model_slug, status="error",
                       error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000))
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": f"[Error: {exc}]"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        return

    answer = _externalize_images(answer, base_url)
    answer = apply_stop_sequences(answer, stop)
    _ok = bool(answer.strip())
    METRICS.record(model_req=model, model_res=model_slug, status="ok" if _ok else "error",
                   error_class=None if _ok else "empty_reply", latency_ms=int((time.time() - t0) * 1000),
                   ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer))
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
):
    t0 = time.time()
    try:
        answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary, model_slug, image)
    except Exception as exc:
        METRICS.record(model_req=model, model_res=model_slug, status="error",
                       error_class=classify_error(str(exc)), latency_ms=int((time.time() - t0) * 1000))
        error_chunk = {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [{"text": f"[Error: {exc}]", "index": 0, "logprobs": None, "finish_reason": None}],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        yield "data: [DONE]\n\n"
        return

    answer = _externalize_images(answer, base_url)
    answer = apply_stop_sequences(answer, stop)
    _ok = bool(answer.strip())
    METRICS.record(model_req=model, model_res=model_slug, status="ok" if _ok else "error",
                   error_class=None if _ok else "empty_reply", latency_ms=int((time.time() - t0) * 1000),
                   ptoks=_count_tokens(prompt), ctoks=_count_tokens(answer))
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
    log.info("API_KEY=%s", "set" if os.environ.get("API_KEY") else "NOT SET (auth disabled)")
    log.info("Starting RipGPT API on port %s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)