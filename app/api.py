from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import time
import uuid
from contextlib import asynccontextmanager
from typing import Iterable

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

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

MODEL_NAME = os.environ.get("DEFAULT_MODEL", "chatgpt-temporary")
MODEL_NAME_PERSISTENT = os.environ.get("PERSISTENT_MODEL", "chatgpt")
SERVICE = BrowserSessionService()


def _make_model_card(model_id: str) -> dict:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "ripgpt",
    }


def _error_response(message: str, status_code: int = 400, error_type: str = "invalid_request_error", code: str | None = None):
    error: dict[str, str] = {"message": message, "type": error_type}
    if code is not None:
        error["code"] = code
    return JSONResponse(status_code=status_code, content={"error": error})


def _resolve_model(model: str) -> str | None:
    if model == MODEL_NAME:
        return MODEL_NAME
    if model == MODEL_NAME_PERSISTENT:
        return MODEL_NAME_PERSISTENT
    return None


def _count_tokens(text: str) -> int:
    return len(text.split())


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
        return {"object": "list", "data": [_make_model_card(MODEL_NAME), _make_model_card(MODEL_NAME_PERSISTENT)]}

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
        temporary = resolved_model != MODEL_NAME_PERSISTENT

        if is_meta_request(payload.messages):
            meta_answer = generate_meta_response(payload.messages)
            return _make_chat_completion_response(completion_id, created, resolved_model, meta_answer, "")

        prompt = extract_last_user_message(payload.messages)
        if not prompt:
            return _error_response("No user message provided.")

        if payload.stream:
            return StreamingResponse(
                _stream_chat_completion(
                    prompt=prompt,
                    completion_id=completion_id,
                    created=created,
                    model=resolved_model,
                    temporary=temporary,
                    stop=payload.stop,
                    include_usage=bool(payload.stream_options and payload.stream_options.include_usage),
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            answer = SERVICE.ask(prompt, temporary=temporary)
        except Exception as exc:
            return _error_response(str(exc), status_code=500, error_type="server_error")

        answer = apply_stop_sequences(answer, payload.stop)
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
        temporary = resolved_model != MODEL_NAME_PERSISTENT

        if payload.stream:
            return StreamingResponse(
                _stream_completion(
                    prompt=prompt,
                    completion_id=completion_id,
                    created=created,
                    model=resolved_model,
                    temporary=temporary,
                    stop=payload.stop,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            answer = SERVICE.ask(prompt, temporary=temporary)
        except Exception as exc:
            return _error_response(str(exc), status_code=500, error_type="server_error")

        answer = apply_stop_sequences(answer, payload.stop)
        return _make_completion_response(completion_id, created, resolved_model, answer, prompt)

    @app.get("/health")
    async def health():
        return {"status": "ok", "session_ready": SERVICE.is_ready()}

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

    try:
        answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary)
    except Exception as exc:
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

    answer = apply_stop_sequences(answer, stop)
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
    stop: str | list[str] | None,
):
    try:
        answer = await asyncio.to_thread(SERVICE.ask, prompt, temporary)
    except Exception as exc:
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

    answer = apply_stop_sequences(answer, stop)
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