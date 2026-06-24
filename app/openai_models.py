from __future__ import annotations

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


def is_meta_request(messages: list[ChatMessage]) -> bool:
    for message in messages:
        content = content_to_text(message.content)
        if not content:
            continue
        lower = content.lower()
        if message.role == "system" and any(
            keyword in lower
            for keyword in (
                "generate a concise",
                "3-5 word title",
                "follow-up question",
                "suggest 3-5",
                "summarizing the chat",
                "json object with a",
                "follow_ups",
            )
        ):
            return True
        if message.role == "user" and any(
            keyword in lower for keyword in ("### task:", "### guidelines", "suggest 3-5 relevant")
        ):
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