"""Browser client used by the RipGPT OpenAI-compatible API."""

import json
import time
import re
import os
import logging

from playwright.sync_api import sync_playwright

# ── Configuration ──────────────────────────────────────────────────────────────
HEADLESS = os.environ.get("HEADLESS", "false").lower() in ("1", "true", "yes")
VERBOSE  = os.environ.get("VERBOSE", "true").lower() in ("1", "true", "yes")

# Authentication = your chatgpt.com session cookie (no email/OTP login anymore).
# Get it from DevTools → Application → Cookies → https://chatgpt.com → copy the
# value of "__Secure-next-auth.session-token". Treat it like a password (.env).
# It expires after a while; recopy it when the session drops. Leave empty to run
# in anonymous (logged-out) mode.
CHATGPT_SESSION_TOKEN = os.environ.get("CHATGPT_SESSION_TOKEN", "").strip()

# Optional: extra raw cookies "name=value; name2=value2" — e.g. a chunked token
# (__Secure-next-auth.session-token.0 / .1) or cf_clearance to satisfy Cloudflare.
CHATGPT_COOKIES = os.environ.get("CHATGPT_COOKIES", "").strip()

SESSION_COOKIE_NAME = "__Secure-next-auth.session-token"
# ──────────────────────────────────────────────────────────────────────────────


# Cloudflare cookies are bound to the User-Agent + IP that earned them, so injecting
# the ones from your Chrome into this (possibly different) browser/host usually
# backfires — CF sees a mismatch and re-challenges. We skip them by default and let
# the browser earn fresh ones. Set INJECT_CF_COOKIES=true to force-inject them.
_CF_COOKIE_NAMES = {"cf_clearance", "__cf_bm", "__cflb", "_cfuvid"}
INJECT_CF_COOKIES = os.environ.get("INJECT_CF_COOKIES", "false").lower() in ("1", "true", "yes")


def _parse_cookie_string(raw: str) -> list[tuple]:
    """Parse a full 'name=value; name2=value2' cookie string (a leading 'Cookie:' is OK).

    Splits on the FIRST '=' only, so base64/JWT values (which contain '=' and '.')
    survive intact — this is what makes the chunked session-token.0/.1 work.
    """
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1]
    pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def _build_auth_cookies() -> list[dict]:
    """Build Playwright cookies from env.

    Use CHATGPT_COOKIES (the full cookie string) when your session token is chunked
    into __Secure-next-auth.session-token.0/.1 — a single CHATGPT_SESSION_TOKEN can't
    represent that. Cookies are added by url so Playwright sets domain/path/secure
    correctly, including the __Secure-/__Host- prefixes.
    """
    pairs: list[tuple] = []
    if CHATGPT_SESSION_TOKEN:
        pairs.append((SESSION_COOKIE_NAME, CHATGPT_SESSION_TOKEN))
    if CHATGPT_COOKIES:
        pairs.extend(_parse_cookie_string(CHATGPT_COOKIES))

    cookies = []
    skipped_cf = 0
    for name, value in pairs:
        if name in _CF_COOKIE_NAMES and not INJECT_CF_COOKIES:
            skipped_cf += 1
            continue
        cookies.append({"name": name, "value": value, "url": "https://chatgpt.com"})
    if skipped_cf:
        _log(f"[browser] Skipped {skipped_cf} Cloudflare cookie(s) (set INJECT_CF_COOKIES=true to keep).")
    return cookies


def _has_auth() -> bool:
    return bool(CHATGPT_SESSION_TOKEN or CHATGPT_COOKIES)

logger = logging.getLogger("ripgpt.browser")

USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:136.0) "
    "Gecko/20100101 Firefox/136.0"
)

try:
    from playwright_stealth import Stealth
    def apply_stealth(page):
        Stealth().apply_stealth_sync(page)
except ImportError:
    try:
        from playwright_stealth import stealth_sync as apply_stealth
    except ImportError:
        def apply_stealth(page):
            pass


# Max seconds to wait for a single answer. Reasoning models ("…-thinking") can be
# slow, so this is generous and overridable.
ANSWER_TIMEOUT = float(os.environ.get("ANSWER_TIMEOUT", "300"))
# How long the rendered answer must stop changing (while not generating) before we
# accept it — a transport-independent safety net if OpenAI changes its markers.
DOM_STABLE_SECS = float(os.environ.get("DOM_STABLE_SECS", "2.5"))

# Injected as an init-script. Hooks BOTH window.fetch AND window.WebSocket.
#
# Why WebSocket: since ~2025, POST /backend-api/f/conversation no longer streams the
# answer. It returns a short SSE "stream_handoff" then [DONE]; the real tokens are
# pushed over wss://ws.chatgpt.com, tunnelled as SSE "encoded_item" strings inside
# "conversation-turn-stream" messages (live) and the subscribe reply's "catchups"
# (backlog already produced before we subscribed). We capture both transports.
#
# Globals exposed:
#   window.__sse_chunks    raw fetch SSE text (handoff; or, in legacy/anon, the answer)
#   window.__ws_chunks     encoded_item SSE frames pulled out of WebSocket messages
#   window.__sse_done      fetch conversation body closed (handoff finished — NOT the answer)
#   window.__answer_done   the turn actually completed (authoritative)
#   window.__turn_started  a conversation POST or a turn stream was observed
#
# Idempotent: guarded by __ripgpt_hooked so repeated evaluate() calls never re-wrap
# (re-wrapping fetch used to stack wrappers and duplicate every chunk).
INTERCEPT_JS = """
() => {
    if (window.__ripgpt_hooked) return;
    window.__ripgpt_hooked = true;

    window.__sse_chunks   = window.__sse_chunks || [];
    window.__ws_chunks    = window.__ws_chunks  || [];
    window.__sse_done     = false;
    window.__answer_done  = false;
    window.__turn_started = false;

    const markComplete = (s) => {
        if (typeof s === 'string' &&
            (s.indexOf('"message_stream_complete"') !== -1 ||
             s.indexOf('"conversation-turn-complete"') !== -1)) {
            window.__answer_done = true;
        }
    };

    // payload shape: {type:'conversation-turn-stream', payload:{type:'stream-item', encoded_item:'data: {…}'}}
    //            or: {type:'conversation-turn-complete', payload:{conversation_id}}
    const collectItem = (payload) => {
        if (!payload || typeof payload !== 'object') return;
        window.__turn_started = true;
        if (payload.type === 'conversation-turn-complete') { window.__answer_done = true; return; }
        const inner = payload.payload;
        if (inner && typeof inner === 'object' && typeof inner.encoded_item === 'string') {
            window.__ws_chunks.push(inner.encoded_item);
            markComplete(inner.encoded_item);
        }
    };

    // ── fetch hook (handoff; legacy/anon direct SSE) ──────────────
    const _origFetch = window.fetch.bind(window);
    window.fetch = async function(input, init) {
        const url    = (typeof input === 'string') ? input : ((input && input.url) || '');
        const method = (init && init.method) ? init.method.toUpperCase()
                     : ((input && input.method) ? input.method.toUpperCase() : 'GET');
        const isConvo = (url.includes('/backend-api/f/conversation')
                      || url.includes('/backend-anon/f/conversation')
                      || url.includes('/backend-api/conversation')
                      || url.includes('/backend-anon/conversation'))
                      && !url.includes('/prepare')
                      && method === 'POST';
        // Best-effort model override: rewrite the outgoing "model" field on both the
        // turn and the prepare calls. Far more robust than driving ChatGPT's model menu.
        const isConvoPost = (url.includes('/backend-api/f/conversation')
                          || url.includes('/backend-anon/f/conversation')) && method === 'POST';
        if (isConvoPost && window.__ripgpt_model && init && typeof init.body === 'string') {
            try {
                const b = JSON.parse(init.body);
                if (b && typeof b === 'object' && typeof b.model === 'string') {
                    b.model = window.__ripgpt_model;
                    init = Object.assign({}, init, { body: JSON.stringify(b) });
                }
            } catch (e) {}
        }
        const response = await _origFetch(input, init);
        if (!isConvo) return response;
        window.__turn_started = true;
        try {
            const reader  = response.clone().body.getReader();
            const decoder = new TextDecoder();
            (async () => {
                try {
                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) { window.__sse_done = true; break; }
                        const text = decoder.decode(value, { stream: true });
                        window.__sse_chunks.push(text);
                        markComplete(text);
                    }
                } catch (e) { window.__sse_done = true; }
            })();
        } catch (e) { window.__sse_done = true; }
        return response;
    };

    // ── WebSocket hook (real-time answer delivery) ────────────────
    const _OrigWS = window.WebSocket;
    if (_OrigWS) {
        const Patched = function(url, protocols) {
            const ws = (protocols === undefined) ? new _OrigWS(url) : new _OrigWS(url, protocols);
            try {
                ws.addEventListener('message', (ev) => {
                    const data = ev.data;
                    if (typeof data !== 'string') return;   // ignore binary frames
                    let frames;
                    try { frames = JSON.parse(data); } catch (e) { return; }
                    if (!Array.isArray(frames)) frames = [frames];
                    for (const f of frames) {
                        if (!f || typeof f !== 'object') continue;
                        // subscribe reply with a backlog of already-produced items
                        if (f.reply && Array.isArray(f.reply.catchups)) {
                            for (const c of f.reply.catchups) collectItem(c && c.payload);
                        }
                        // live topic message
                        if (f.type === 'message') collectItem(f.payload);
                    }
                });
            } catch (e) {}
            return ws;
        };
        Patched.prototype = _OrigWS.prototype;
        try {
            Patched.CONNECTING = _OrigWS.CONNECTING; Patched.OPEN = _OrigWS.OPEN;
            Patched.CLOSING = _OrigWS.CLOSING; Patched.CLOSED = _OrigWS.CLOSED;
        } catch (e) {}
        window.WebSocket = Patched;
    }
}
"""

# Back-compat alias: existing call sites reference FETCH_INTERCEPT_JS.
FETCH_INTERCEPT_JS = INTERCEPT_JS

RESET_SSE_JS = """
() => {
    window.__sse_chunks   = [];
    window.__ws_chunks    = [];
    window.__sse_done     = false;
    window.__answer_done  = false;
    window.__turn_started = false;
}
"""


def _log(msg):
    if VERBOSE:
        logger.info(msg)


def _clean_entity_markers(text):
    text = re.sub(r'\ue200', '', text)
    def replace_entity(m):
        try:
            arr = json.loads(m.group(1))
            return str(arr[1]) if len(arr) > 1 else ""
        except Exception:
            return ""
    text = re.sub(r'entity\ue202(\[.*?\])\ue201', replace_entity, text)
    text = re.sub(r'[\ue200-\ue2ff]', '', text)
    return text


def _extract_parts_from_message(msg):
    if not isinstance(msg, dict):
        return ""
    # Handle multiple author formats: {"role": "assistant"} or just "assistant"
    author = msg.get("author", {})
    if isinstance(author, dict):
        role = author.get("role", "")
    elif isinstance(author, str):
        role = author
    else:
        role = ""
    if role != "assistant":
        return ""
    content = msg.get("content", {})
    if not isinstance(content, dict):
        return ""
    if content.get("content_type") != "text":
        return ""
    parts = content.get("parts", [])
    if parts and isinstance(parts[0], str):
        return parts[0]
    return ""


def _is_text_parts_path(p) -> bool:
    """True if a JSON-Pointer targets the visible text of a message: .../content/parts/0."""
    return isinstance(p, str) and (p.endswith("/content/parts/0") or p.endswith("/parts/0"))


def _parse_sse_answer(raw):
    """Reconstruct the assistant answer from the "v1" delta stream.

    The stream is shared by the fetch SSE handoff and the WebSocket "encoded_item"
    frames. Each ``data:`` line is one of:
      * a snapshot      {"v": {"message": {...}}}                 (whole message)
      * a rooted add    {"p": "", "o": "add", "v": {"message":…}} (seeds the message)
      * an explicit op  {"p": "/message/content/parts/0", "o": "append", "v": "tok"}
      * a batched op    {"v": [ {p,o,v}, … ]}
      * an IMPLICIT op  {"v": "tok"}   ← appends to the last-referenced path (modern, common)
      * a typed control {"type": "…"}  ← metadata / markers, ignored for text

    We keep the authoritative finished snapshot if one is emitted, otherwise we use
    the incrementally rebuilt text. A fresh rooted ``add`` resets the buffer, so for
    reasoning models the final answer message naturally wins over the thinking pass.
    """
    finished_snapshots: list[str] = []
    in_progress_snapshots: list[str] = []

    text = ""            # incrementally rebuilt visible text
    cursor_on_text = False  # last path pointed at .../parts/0 (so implicit {"v":…} appends here)

    def apply_op(p, o, v):
        nonlocal text, cursor_on_text
        # Rooted add of a whole message → reset buffer to its current text
        if p == "" and o == "add" and isinstance(v, dict) and "message" in v:
            seeded = _extract_parts_from_message(v["message"])
            text = seeded if isinstance(seeded, str) else ""
            cursor_on_text = True
            return
        if _is_text_parts_path(p):
            cursor_on_text = True
            if o in ("append", "") and isinstance(v, str):
                text += v
            elif o in ("replace", "add") and isinstance(v, str):
                text = v
            return
        # Implicit op: no path, no op, scalar value → append to the text cursor
        if (p in ("", None)) and (o in ("", None)) and isinstance(v, str):
            if cursor_on_text:
                text += v
            return
        # Any other concrete path (status, metadata, …) moves the cursor off text
        if isinstance(p, str) and p not in ("", None):
            cursor_on_text = False

    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        data_str = line[len("data: "):]
        if data_str.strip() in ("[DONE]", '"v1"'):
            continue
        try:
            event = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type"):
            continue  # typed control frame (server_ste_metadata, *_complete, …)

        v = event.get("v")
        o = event.get("o", "")
        p = event.get("p", "")

        # Snapshot: capture as authoritative if finished, and seed the buffer.
        if isinstance(v, dict) and "message" in v:
            candidate = _extract_parts_from_message(v["message"])
            if candidate:
                status = v["message"].get("status", "") if isinstance(v["message"], dict) else ""
                if status == "finished_successfully":
                    finished_snapshots.append(candidate)
                elif status == "in_progress":
                    in_progress_snapshots.append(candidate)
            apply_op(p if isinstance(p, str) else "", o or "add", v)
            continue

        # Batched list of ops
        if isinstance(v, list):
            for patch in v:
                if isinstance(patch, dict):
                    apply_op(patch.get("p", ""), patch.get("o", ""), patch.get("v"))
            continue

        # Scalar / explicit single op
        apply_op(p, o, v)

    if finished_snapshots:
        return _clean_entity_markers(finished_snapshots[-1]).strip()
    if text.strip():
        return _clean_entity_markers(text).strip()
    if in_progress_snapshots:
        return _clean_entity_markers(in_progress_snapshots[-1]).strip()
    return ""

def _read_answer_from_dom(page) -> str:
    """Read the last assistant message from the final rendered DOM as Markdown."""
    try:
        text = page.evaluate(r"""() => {
            const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
            if (!msgs.length) return '';
            const last = msgs[msgs.length - 1];
            const root = last.querySelector('.markdown') || last;

            function normalize(text) {
                return (text || '').replace(/\u00a0/g, ' ');
            }

            function clean(text) {
                return normalize(text).replace(/\n{3,}/g, '\n\n').trim();
            }

            function inlineMd(node) {
                if (!node) return '';
                if (node.nodeType === Node.TEXT_NODE) {
                    return normalize(node.textContent);
                }
                if (node.nodeType !== Node.ELEMENT_NODE) return '';

                const tag = node.tagName.toLowerCase();
                if (tag === 'br') return '\n';
                if (tag === 'strong' || tag === 'b') return '**' + Array.from(node.childNodes).map(inlineMd).join('') + '**';
                if (tag === 'em' || tag === 'i') return '*' + Array.from(node.childNodes).map(inlineMd).join('') + '*';
                if (tag === 'code') return '`' + normalize(node.textContent) + '`';
                if (tag === 'a') {
                    const href = node.getAttribute('href') || '';
                    const label = Array.from(node.childNodes).map(inlineMd).join('');
                    return href ? `[${label}](${href})` : label;
                }
                return Array.from(node.childNodes).map(inlineMd).join('');
            }

            function blockMd(node) {
                if (!node) return '';
                if (node.nodeType === Node.TEXT_NODE) {
                    return normalize(node.textContent);
                }
                if (node.nodeType !== Node.ELEMENT_NODE) return '';

                const tag = node.tagName.toLowerCase();

                if (/^h([1-6])$/.test(tag)) {
                    const level = Number(tag[1]);
                    return '\n' + '#'.repeat(level) + ' ' + Array.from(node.childNodes).map(inlineMd).join('').trim() + '\n';
                }

                if (tag === 'p') {
                    return '\n' + Array.from(node.childNodes).map(inlineMd).join('').trim() + '\n';
                }

                if (tag === 'ul') {
                    return '\n' + Array.from(node.children)
                        .filter(child => child.tagName && child.tagName.toLowerCase() === 'li')
                        .map(li => '- ' + Array.from(li.childNodes).map(inlineMd).join('').trim())
                        .join('\n') + '\n';
                }

                if (tag === 'ol') {
                    const start = Number(node.getAttribute('start') || '1');
                    return '\n' + Array.from(node.children)
                        .filter(child => child.tagName && child.tagName.toLowerCase() === 'li')
                        .map((li, i) => `${start + i}. ${Array.from(li.childNodes).map(inlineMd).join('').trim()}`)
                        .join('\n') + '\n';
                }

                if (tag === 'blockquote') {
                    const text = Array.from(node.childNodes).map(blockMd).join('').trim();
                    return '\n' + text.split('\n').map(line => '> ' + line).join('\n') + '\n';
                }

                if (tag === 'hr') {
                    return '\n---\n';
                }

                if (tag === 'pre') {
                    let language = '';
                    const header = node.querySelector('.text-sm.font-medium, .text-sm');
                    if (header) {
                        const label = normalize(header.textContent).trim().toLowerCase();
                        if (label === 'html' || label === 'css' || label === 'javascript' || label === 'js' || label === 'python' || label === 'json' || label === 'bash') {
                            language = label === 'js' ? 'javascript' : label;
                        }
                    }

                    const content = node.querySelector('.cm-content') || node.querySelector('[class*="cm-content"]');
                    let code = '';

                    function readCode(n) {
                        if (!n) return;
                        if (n.nodeType === Node.TEXT_NODE) {
                            code += normalize(n.textContent);
                            return;
                        }
                        if (n.nodeType !== Node.ELEMENT_NODE) return;
                        if (n.tagName.toLowerCase() === 'br') {
                            code += '\n';
                            return;
                        }
                        Array.from(n.childNodes).forEach(readCode);
                    }

                    readCode(content || node);
                    code = code.replace(/\n+$/, '');
                    return '\n```' + language + '\n' + code + '\n```\n';
                }

                if (tag === 'table') {
                    const rows = Array.from(node.querySelectorAll('tr'));
                    if (!rows.length) return '';
                    let out = '\n';
                    rows.forEach((row, idx) => {
                        const cells = Array.from(row.querySelectorAll('th, td')).map(cell => Array.from(cell.childNodes).map(inlineMd).join('').trim());
                        out += '| ' + cells.join(' | ') + ' |\n';
                        if (idx === 0) {
                            out += '| ' + cells.map(() => '---').join(' | ') + ' |\n';
                        }
                    });
                    return out;
                }

                return Array.from(node.childNodes).map(blockMd).join('');
            }

            const result = Array.from(root.childNodes).map(blockMd).join('');
            return clean(result);
        }""")
        return (text or "").strip()
    except Exception:
        return ""


def _is_generating(page) -> bool:
    """True while ChatGPT is still producing a turn (the send button is a Stop button)."""
    try:
        return bool(page.evaluate(
            """() => {
                const sels = ['button[data-testid="stop-button"]',
                              'button[aria-label="Stop streaming"]',
                              'button[aria-label="Stop generating"]',
                              'button[aria-label*="Stop"]'];
                for (const s of sels) { if (document.querySelector(s)) return true; }
                return false;
            }"""
        ))
    except Exception:
        return False


def _dismiss_dialogs(page):
    """Best-effort: close cookie banners / onboarding / upsell modals hiding the composer.

    ChatGPT often opens a blurred overlay (data-testid=modal-beacon) that intercepts the
    click on the composer. Escape closes most of them; we also click common close /
    affirmative buttons.
    """
    try:
        page.keyboard.press("Escape")
        time.sleep(0.15)
    except Exception:
        pass
    for sel in ['[data-testid="close-button"]', 'button[aria-label="Close"]',
                'button[aria-label="Fermer"]', 'button[aria-label*="close" i]']:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=300):
                b.click(timeout=1500)
                time.sleep(0.2)
        except Exception:
            pass
    labels = ["Reject non-essential", "Accept all", "Accept", "Stay logged out",
              "Got it", "Okay, let's go", "Okay", "Maybe later", "Not now", "Dismiss", "Close", "No thanks"]
    for t in labels:
        try:
            btn = page.locator(f'button:has-text("{t}")').first
            if btn.is_visible(timeout=300):
                btn.click(timeout=1500)
                time.sleep(0.2)
        except Exception:
            pass


def _focus_composer(page):
    """Dismiss any modal covering the composer, then click it — with retries.

    Fixes 'modal-beacon subtree intercepts pointer events' on a fresh session.
    """
    comp = page.locator("#prompt-textarea")
    for _ in range(3):
        _dismiss_dialogs(page)
        try:
            comp.click(timeout=8000)
            return
        except Exception:
            time.sleep(0.4)
    # last resort: focus via JS (bypasses the overlay intercept)
    try:
        page.evaluate("() => { const e = document.querySelector('#prompt-textarea'); if (e) e.focus(); }")
    except Exception:
        pass


def _ensure_composer(page, timeout=30000):
    """Wait for the chat composer; dismiss dialogs and reload once if it doesn't show."""
    try:
        page.locator("#prompt-textarea").wait_for(state="visible", timeout=timeout)
        return
    except Exception:
        pass
    _dismiss_dialogs(page)
    try:
        page.locator("#prompt-textarea").wait_for(state="visible", timeout=8000)
        return
    except Exception:
        pass
    # Last resort: reload home and retry once.
    try:
        page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=30000)
        page.evaluate(FETCH_INTERCEPT_JS)
        _dismiss_dialogs(page)
    except Exception:
        pass
    page.locator("#prompt-textarea").wait_for(state="visible", timeout=15000)


def _wait_for_answer(page):
    """Wait until the turn truly completes, then return the answer.

    Completion is signalled by window.__answer_done — set by the WebSocket interceptor
    on conversation-turn-complete / message_stream_complete (and by the fetch hook in
    legacy/anon direct-SSE mode). A DOM-stability check is the transport-independent
    safety net if OpenAI changes those markers again.
    """
    deadline = time.time() + ANSWER_TIMEOUT
    last_dom = ""
    last_change = time.time()
    started = False

    while time.time() < deadline:
        state = page.evaluate(
            """() => ({
                answer_done: !!window.__answer_done,
                started: !!window.__turn_started,
                ws: (window.__ws_chunks || []).length,
                sse: (window.__sse_chunks || []).length
            })"""
        )
        started = started or bool(state.get("started"))

        if state.get("answer_done"):
            _log("[*] answer_done — transport completion signal received.")
            break

        # DOM-stability fallback: answer stopped growing and we're no longer generating.
        dom_now = _read_answer_from_dom(page)
        if dom_now != last_dom:
            last_dom = dom_now
            last_change = time.time()
        if started and dom_now and not _is_generating(page) and (time.time() - last_change) > DOM_STABLE_SECS:
            _log("[*] DOM stable and not generating — accepting answer.")
            break

        if VERBOSE and (state.get("ws") or state.get("sse")):
            _log(f"    ... ws={state['ws']} sse={state['sse']} chunks")
        time.sleep(0.5)
    else:
        _log("[*] Timed out waiting for completion — using whatever was captured.")

    # Merge both transports: fetch SSE (handoff/legacy) + WebSocket encoded_items.
    chunks = page.evaluate("() => (window.__sse_chunks || []).concat(window.__ws_chunks || [])")
    raw = "".join(chunks)
    _log(f"[*] Captured {len(raw)} chars across fetch + websocket transports.")
    _m = re.search(r'"model_slug":\s*"([^"]+)"', raw)
    if _m:
        _log(f"[*] model_slug actually used: {_m.group(1)}")
    answer = _parse_sse_answer(raw)

    # The final rendered DOM is authoritative: the v1 stream can include transient
    # rewrites and reasoning passes that don't map cleanly to the visible answer.
    time.sleep(0.4)
    dom_answer = _read_answer_from_dom(page)

    if dom_answer:
        if answer and answer != dom_answer:
            _log(f"[*] Preferring final DOM answer ({len(answer)} -> {len(dom_answer)} chars).")
        answer = dom_answer
    elif not answer:
        _log("[*] DOM answer empty and stream parse empty.")

    return answer


class ChatSession:
    """Persistent browser session used by the API worker."""

    def __init__(self):
        self._playwright = sync_playwright().start()
        browser = self._playwright.firefox.launch(headless=HEADLESS)
        self._context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        self._page = self._context.new_page()
        self.logged_out = False  # updated by _ensure_session; read by the metrics layer

        try:
            apply_stealth(self._page)
            _log("[browser] Stealth applied.")
        except Exception as e:
            _log(f"[browser] Stealth error: {e}")

        # Register fetch + WebSocket interceptors as an init script so they are
        # installed before chatgpt.com opens its socket, on every navigation.
        self._context.add_init_script(FETCH_INTERCEPT_JS)
        _log("[browser] fetch + websocket interceptors registered.")

        # Authentication is the chatgpt.com session cookie (no email/OTP login).
        cookies = _build_auth_cookies()
        if cookies:
            self._context.add_cookies(cookies)
            _log(f"[browser] Injected {len(cookies)} auth cookie(s); loading chatgpt.com ...")
        else:
            _log("[browser] No CHATGPT_SESSION_TOKEN — anonymous (logged-out) mode.")

        self._page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
        self._page.evaluate(FETCH_INTERCEPT_JS)  # no-op if the init script already ran
        self._ensure_session()
        _log("[browser] Session ready.")

    def _ensure_session(self) -> None:
        """Confirm the page is usable; raise a clear error on a bad/expired token."""
        if "/auth" in self._page.url or "/login" in self._page.url:
            if _has_auth():
                raise RuntimeError(
                    "ChatGPT rejected the session cookie — CHATGPT_SESSION_TOKEN is "
                    "invalid or expired. Copy a fresh __Secure-next-auth.session-token."
                )
            raise RuntimeError("Not authenticated and no CHATGPT_SESSION_TOKEN set (.env).")
        # Composer is present both logged-in and in anonymous mode.
        _dismiss_dialogs(self._page)
        _ensure_composer(self._page)
        self.logged_out = bool(_has_auth() and self._looks_logged_out())
        if self.logged_out:
            _log("[browser] WARNING: token set but page looks logged-out — "
                 "CHATGPT_SESSION_TOKEN may be expired (running anonymously).")

    def _looks_logged_out(self) -> bool:
        try:
            return bool(self._page.evaluate(
                """() => {
                    const direct = ['[data-testid=\"login-button\"]',
                                    '[data-testid=\"signup-button\"]',
                                    'a[href*=\"/auth/login\"]'];
                    for (const s of direct) { if (document.querySelector(s)) return true; }
                    const els = Array.from(document.querySelectorAll('button, a'));
                    return els.some(e => /log ?in|sign ?up/i.test((e.textContent || '').trim()));
                }"""
            ))
        except Exception:
            return False

    def _apply_model(self, model_slug):
        # Force the model for this turn by rewriting the outgoing request's "model"
        # field (see INTERCEPT_JS). None/empty = no override (ChatGPT's selected model).
        self._page.evaluate("(s) => { window.__ripgpt_model = s || null; }", model_slug or None)

    def ask(self, question, model_slug=None):
        # Re-inject interceptors in case page JS replaced window.fetch / WebSocket
        self._page.evaluate(FETCH_INTERCEPT_JS)
        self._page.evaluate(RESET_SSE_JS)
        _ensure_composer(self._page)
        self._apply_model(model_slug)
        _focus_composer(self._page)
        self._page.keyboard.type(question, delay=20)
        time.sleep(0.3)
        self._page.keyboard.press("Enter")
        return _wait_for_answer(self._page)

    def send(self, question, model_slug=None):
        """Type and send a question without waiting for the answer."""
        self._page.evaluate(FETCH_INTERCEPT_JS)
        self._page.evaluate(RESET_SSE_JS)
        _ensure_composer(self._page)
        self._apply_model(model_slug)
        _focus_composer(self._page)
        self._page.keyboard.type(question, delay=20)
        time.sleep(0.3)
        self._page.keyboard.press("Enter")

    def is_alive(self) -> bool:
        """Lightweight, non-disruptive liveness check.

        It NEVER navigates: the old version did a goto()+evaluate() that raced with
        in-flight work ("Execution context was destroyed"), wrongly concluded the
        session had died, and wedged the browser via a needless relogin. Here, any
        transient error is treated as "alive" — a real request recovers on its own
        through _start_new_chat / _ensure_composer. Only a clear logged-out state
        (auth URL, or visible login buttons) counts as dead.
        """
        try:
            url = self._page.url
            if "/auth" in url or "/login" in url:
                return False
            # Composer already present on the current page → definitely alive.
            if self._page.locator("#prompt-textarea").count() > 0:
                return True
            # Not on a chat page: only declare dead if the page clearly shows logged-out UI.
            return not self._looks_logged_out()
        except Exception as exc:
            _log(f"[session] Health check inconclusive ({exc}); assuming alive.")
            return True

    def relogin(self) -> None:
        """Recover a dropped session by reloading — the cookie persists on the context."""
        _log("[session] Reloading chatgpt.com to recover the session ...")
        self._page.goto("https://chatgpt.com", wait_until="domcontentloaded", timeout=60_000)
        self._page.evaluate(FETCH_INTERCEPT_JS)
        self._ensure_session()
        _log("[session] Session recovered.")

    def close(self):
        try:
            self._context.browser.close()
        except Exception:
            pass
        self._playwright.stop()