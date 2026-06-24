from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: str | list[dict[str, Any]] | None = ""
    name: str | None = None
    tool_call_id: str | None = None

    model_config = ConfigDict(extra="allow")


class StreamOptions(BaseModel):
    include_usage: bool = False

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    n: int = 1
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    user: str | None = None
    stream_options: StreamOptions | None = None
    response_format: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")


class CompletionRequest(BaseModel):
    model: str
    prompt: str | list[str]
    stream: bool = False
    n: int = 1
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: str | list[str] | None = None
    suffix: str | None = None
    user: str | None = None

    model_config = ConfigDict(extra="allow")


def content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return " ".join(parts).strip()


def extract_last_user_message(messages: list[ChatMessage]) -> str:
    last_user_message = ""
    for message in messages:
        if message.role == "user":
            last_user_message = content_to_text(message.content)
    return last_user_message.strip()


# ChatGPT has no system role and no native multi-turn for our one-shot fresh chats, so
# we fold the whole conversation (system + history) into a single transcript prompt.
MAX_PROMPT_CHARS = 12000


def serialize_messages(messages: list[ChatMessage]) -> str:
    """Flatten system + conversation history into one prompt for ChatGPT.

    Single user message with no system/history → returned verbatim (back-compat).
    Otherwise: system folded as a preamble, prior turns labelled, newest kept within
    a char budget (oldest middle turns dropped first; system + latest always kept).
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for m in messages:
        text = content_to_text(m.content)
        if not text:
            continue
        if m.role in ("system", "developer"):
            system_parts.append(text)
        elif m.role in ("user", "assistant"):
            turns.append((m.role, text))
        # tool messages are not representable in the ChatGPT UI — skip.
    if not turns:
        return ""

    system = "\n\n".join(system_parts).strip()
    if not system and len(turns) == 1 and turns[0][0] == "user":
        return turns[0][1]   # plain single-turn prompt — unchanged behaviour

    label = {"user": "User", "assistant": "Assistant"}
    rendered = [f"{label[r]}: {t}" for r, t in turns]
    budget = MAX_PROMPT_CHARS - (len(system) + 200)
    kept: list[str] = []
    total = 0
    for line in reversed(rendered):          # keep newest turns first
        if kept and total + len(line) > budget:
            break
        kept.append(line)
        total += len(line)
    kept.reverse()
    body = "\n\n".join(kept)
    if system:
        return f"System: {system}\n\n{body}"
    return body


# OpenWebUI (Bypass Embedding / Full Context) injects the whole document as ONE user
# string: "### Task: …<context><source>…doc text…</source></context>\n\n<the real query>".
# Typing tens of thousands of chars is unviable + would truncate, so we lift the document
# text out into a .md file to upload, and type only the user's actual query.
_CONTEXT_RE = re.compile(r"<context>(.*?)</context>", re.DOTALL)
PROMPT_TO_FILE_THRESHOLD = 8000


def prompt_to_attachment_if_large(prompt: str, threshold: int = PROMPT_TO_FILE_THRESHOLD):
    """Return (typed_prompt, [(filename, mime, bytes)]).

    If the prompt embeds a large <context>…</context> block, move that document text to a
    .md attachment and reduce the typed prompt to the user's trailing query. Otherwise the
    prompt is returned unchanged with no files."""
    if not prompt or len(prompt) < threshold:
        return prompt, []
    m = _CONTEXT_RE.search(prompt)
    if not m:
        return prompt, []
    doc = re.sub(r"</?source[^>]*>", "", m.group(1)).strip()   # keep source text, drop tags
    if len(doc) < 500:
        return prompt, []
    query = prompt[m.end():].strip()                            # the real question follows </context>
    if not query:
        query = "Utilise le(s) document(s) joint(s) pour répondre."
    return query, [("document.md", "text/markdown", doc.encode("utf-8"))]


def last_user_attachment_kind(messages: list[ChatMessage]) -> str | None:
    """Return the non-text input type in the latest user message (image_url/file/…), else None."""
    for m in reversed(messages):
        if m.role == "user":
            if isinstance(m.content, list):
                for item in m.content:
                    if isinstance(item, dict):
                        t = item.get("type")
                        if t and t != "text":
                            return str(t)
            return None
    return None


# ── File / document upload ────────────────────────────────────────────────────
# Uploaded files are decoded here and handed (name, mime, bytes) to the browser,
# which drops them into ChatGPT's composer (page.set_input_files) for native
# document understanding. Caller validates caps and returns 4xx on violation.

MAX_FILES = 10                              # ChatGPT's per-message ceiling
MAX_FILE_BYTES = 50 * 1024 * 1024           # 50 MB/file (conservative)
MAX_TOTAL_UPLOAD_BYTES = 100 * 1024 * 1024  # cumulative cap across all files in one request

# mime -> extension for files ChatGPT accepts (retrieval + vision)
SUPPORTED_UPLOAD_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "text/plain": "txt", "text/markdown": "md", "text/csv": "csv",
    "application/json": "json", "application/rtf": "rtf", "text/rtf": "rtf",
    "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif",
}


class FileInputError(ValueError):
    """Raised for malformed / unsupported / oversized file inputs (→ 400)."""
    def __init__(self, message: str, code: str = "invalid_file"):
        super().__init__(message)
        self.code = code


def _decode_data_url(data_url: str, max_bytes: int | None = None) -> tuple[str, bytes] | None:
    """data:<mime>[;param=...][;base64],<payload>  ->  (mime, bytes). None if not a data URL.

    max_bytes is checked against the ENCODED length first, so an oversized payload is
    rejected before the decoded buffer is allocated (admission control, not post-hoc)."""
    m = re.match(r"data:([^;,]*)((?:;[\w.+-]+=[^;,]*)*)(;base64)?,(.*)", data_url or "", re.DOTALL)
    if not m:
        return None
    mime = (m.group(1) or "").strip() or "application/octet-stream"
    is_b64 = bool(m.group(3))
    payload = m.group(4)
    approx = (len(payload) * 3) // 4 if is_b64 else len(payload)
    if max_bytes is not None and approx > max_bytes:
        raise FileInputError(f"A file exceeds the {max_bytes // 1024 // 1024} MB limit.", code="file_too_large")
    try:
        data = base64.b64decode(payload) if is_b64 else urllib.parse.unquote_to_bytes(payload)
    except Exception:
        raise FileInputError("Malformed base64 in file/image data URL.", code="invalid_file")
    return mime, data


def _ext_for_upload(mime: str, filename: str) -> str:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    return SUPPORTED_UPLOAD_MIME.get(mime, "bin")


def extract_file_attachments(messages: list[ChatMessage]) -> list[tuple[str, str, bytes]]:
    """Decode file/image attachments from the latest user message.

    Returns a list of (filename, mime, data). Raises FileInputError on malformed,
    unsupported-type, oversized, too-many, or remote-URL inputs (v1 can't fetch URLs).
    """
    user_msgs = [m for m in messages if m.role == "user" and isinstance(m.content, list)]
    if not user_msgs:
        return []

    # Admission control: a single message can't exceed MAX_FILES on its own.
    latest = [it for it in user_msgs[-1].content
              if isinstance(it, dict) and it.get("type") in ("file", "image_url")]
    if len(latest) > MAX_FILES:
        raise FileInputError(f"Too many files ({len(latest)}). Maximum is {MAX_FILES} per message.", code="too_many_files")

    # Collect attachments from ALL user turns (newest-first), so a follow-up question
    # re-uploads images/files attached earlier — ripgpt opens a fresh chat per request
    # and can't replay binary history as text, so we must resend them every turn.
    collected: list[tuple[str, str, bytes]] = []
    seen: set[bytes] = set()
    total = 0
    for m in reversed(user_msgs):
        parts = [it for it in m.content
                 if isinstance(it, dict) and it.get("type") in ("file", "image_url")]
        for item in parts:
            filename, mime, data = _decode_attachment_part(item)
            if mime not in SUPPORTED_UPLOAD_MIME:
                raise FileInputError(f"Unsupported file type '{mime}'.", code="unsupported_file_type")
            h = hashlib.md5(data).digest()
            if h in seen:                       # same image resent across turns — once is enough
                continue
            if len(collected) >= MAX_FILES or total + len(data) > MAX_TOTAL_UPLOAD_BYTES:
                break
            seen.add(h)
            total += len(data)
            collected.append((filename, mime, data))
        if len(collected) >= MAX_FILES:
            break
    collected.reverse()                          # restore chronological order
    return collected


def _decode_attachment_part(item: dict) -> tuple[str, str, bytes]:
    """Decode one file/image_url content part → (filename, mime, bytes). Raises on bad input."""
    if item.get("type") == "file":
        f = item.get("file") or {}
        data_url = f.get("file_data") or ""
        filename = f.get("filename") or "document"
        if not data_url.startswith("data:"):
            raise FileInputError("File inputs must be inline base64 data URLs (file_data); the Files API is not supported yet.", code="unsupported_file")
        decoded = _decode_data_url(data_url, max_bytes=MAX_FILE_BYTES)
    else:  # image_url
        url = (item.get("image_url") or {}).get("url", "")
        if not url.startswith("data:"):
            raise FileInputError("Remote image URLs are not supported yet — send the image as a base64 data URL.", code="unsupported_file")
        decoded = _decode_data_url(url, max_bytes=MAX_FILE_BYTES)
        filename = None
    if not decoded:
        raise FileInputError("Could not decode file/image data URL.", code="invalid_file")
    mime, data = decoded
    if filename is None:
        filename = f"image.{_ext_for_upload(mime, '')}"
    elif "." not in filename:
        filename = f"{filename}.{_ext_for_upload(mime, filename)}"
    return filename, mime, data


def is_meta_request(messages: list[ChatMessage]) -> bool:
    """True only for OpenWebUI background tasks (title / follow-ups / tags) we can answer
    with a canned reply instead of a ChatGPT turn.

    IMPORTANT: OpenWebUI's RAG / full-context document-answer prompt ALSO begins with
    "### Task:\nRespond to the user query using the provided context" — that is a REAL
    question and must NOT be short-circuited. So we never treat a message carrying a
    <context> block (or that explicit answer instruction) as meta, and we match only the
    distinctive background-task verbs, not generic "### task:" headers.
    """
    _BG_TASKS = (
        "generate a concise", "3-5 word title", "concise, 3-5 word",
        "follow-up question", "suggest 3-5", "summarizing the chat",
        "json object with a", "follow_ups", "broad tags categorizing",
        "generate 1-3 broad tags",
    )
    for message in messages:
        content = content_to_text(message.content)
        if not content:
            continue
        lower = content.lower()
        if "<context>" in lower or "respond to the user query using the provided context" in lower:
            return False   # a real (document-grounded) answer request
        if any(keyword in lower for keyword in _BG_TASKS):
            return True
    return False


def generate_meta_response(messages: list[ChatMessage]) -> str:
    combined = " ".join(content_to_text(message.content) for message in messages).lower()
    if "title" in combined and ("json" in combined or "concise" in combined):
        return '{"title": "Chat"}'
    if "follow" in combined or "follow_ups" in combined:
        return '{"follow_ups": []}'
    return "OK"


def prompt_from_completion_request(prompt: str | list[str]) -> str:
    if isinstance(prompt, str):
        return prompt.strip()
    return "\n\n".join(item for item in prompt if isinstance(item, str) and item).strip()


def normalize_stop_sequences(stop: str | list[str] | None) -> list[str]:
    if stop is None:
        return []
    if isinstance(stop, str):
        return [stop] if stop else []
    return [item for item in stop if item]


def apply_stop_sequences(text: str, stop: str | list[str] | None) -> str:
    sequences = normalize_stop_sequences(stop)
    if not text or not sequences:
        return text

    stop_index: int | None = None
    for sequence in sequences:
        index = text.find(sequence)
        if index == -1:
            continue
        if stop_index is None or index < stop_index:
            stop_index = index

    if stop_index is None:
        return text
    return text[:stop_index]