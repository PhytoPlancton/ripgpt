<div align="center">

# 🏴‍☠️ ripgpt

### Your ChatGPT subscription, wearing an OpenAI-API trench coat.

*Drive your own logged-in ChatGPT session through a headless browser and expose it as a drop-in **OpenAI-compatible API** — plus a slick web UI on top.*

![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-2EAD33?logo=playwright&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-100%25%20unofficial-412991?logo=openai&logoColor=white)
![status](https://img.shields.io/badge/works%20on-my%20machine-success)

</div>

---

> [!WARNING]
> **For personal & educational use with your _own_ ChatGPT account.** ripgpt automates
> the chatgpt.com web app using your own session cookie — it ships no credentials and
> bypasses no paywall. It is **not affiliated with OpenAI**, and automating the web app
> may run against OpenAI's Terms of Service. You own what you do with it. 🫡

---

## ✨ What it does

- 🔌 **OpenAI drop-in** — `POST /v1/chat/completions` & `/v1/completions`. Point any OpenAI SDK at it.
- 🧠 **Real ChatGPT models** — talks to whatever model your account has (GPT‑5.x, thinking, …).
- 🍪 **Cookie auth** — paste your session cookie, no email/OTP dance.
- 🖥️ **Batteries included** — a full [Open WebUI](https://github.com/open-webui/open-webui) chat interface wired up out of the box.
- 🥷 **Stealth browser** — headless Firefox + `playwright-stealth`, riding your real session.
- 📡 **Streaming** that actually works against ChatGPT's 2025 architecture (see below).

## 🧠 How it actually works (the fun part)

Wrapping ChatGPT used to be easy: `POST` a message, read the streamed answer back. **Not anymore.**
Since 2025 the answer doesn't come back on the HTTP request at all — it's a *hand-off* to a WebSocket.
ripgpt is built around that discovery:

```text
   You ──▶  POST /v1/chat/completions            (OpenAI-compatible, on :8850)
             │
             ▼
   ┌───────────────────────────────────────────────────────────┐
   │  ripgpt (FastAPI)                                           │
   │  drives a headless, stealthed Firefox logged into           │
   │  chatgpt.com with YOUR session cookie, and types the prompt │
   └───────────────────────────────────────────────────────────┘
             │  sentinel proof-of-work ➜ conduit token ➜ send turn
             ▼
   chatgpt.com  ── POST /backend-api/f/conversation ──▶  "stream_handoff"
             │                                                  │
             │   ⚠️ the answer is NOT returned here anymore     │
             ▼                                                  ▼
   wss://ws.chatgpt.com  ◀── subscribe(conversation-turn-…) ───┘
             │  tokens arrive as "v1" JSON-patch deltas, tunnelled in WS frames
             ▼
   ripgpt re-assembles them  ──▶  OpenAI-style JSON / SSE  ──▶  You
```

So under the hood ripgpt hooks **both** `window.fetch` *and* `window.WebSocket`, rebuilds the
`v1` delta stream (snapshots, `append` ops, and bare `{"v":"…"}` tokens), and hands you back a
clean OpenAI payload. The browser is non-negotiable: it's what mints ChatGPT's anti-abuse
**sentinel** proof-of-work + Cloudflare tokens that a raw HTTP client can't fake.

## 🚀 Quick start

```bash
git clone git@github.com:PhytoPlancton/ripgpt.git
cd ripgpt
cp .env.example .env      # then fill it in (see below)
docker compose up --build
```

| Service | URL | What |
|---|---|---|
| **Open WebUI** | http://localhost:3001 | ChatGPT-like web interface |
| **ripgpt API** | http://localhost:8850 | the OpenAI-compatible endpoint |
| **Console** | http://localhost:8850/ | live monitoring dashboard (see below) |

First visit to Open WebUI asks you to create a **local** account, then pick the `chatgpt` model and go.

## 📊 Monitoring console

Open **http://localhost:8850/** and paste your API key. A live dashboard (refreshes every
4s, pauses when the tab is hidden) shows exactly what matters for a single-browser proxy:

- **Health strip** — session state · queue depth · 15-min error rate · **wedge risk**
  (consecutive empty/timeout = ChatGPT anti-abuse biting; your #1 failure mode).
- **Requests over time** (ok vs error), **latency p50/p95 per model**, and a **recent-requests
  table** (model routed, status, latency coloured vs that model's p95, ~tokens).
- Loud banners on *logged-out* and *wedge*, a one-click **restart session** button, and a
  Three.js particle sphere that breathes with traffic and turns red when unhealthy. 🟢→🔴

Powered by `GET /stats` (JSON, behind the API key). Deliberately **omits** vanity metrics
(RPS, exact tokens, cost, uptime SLAs, mean latency).

## 🔑 Authentication (your session cookie)

Auth is your chatgpt.com cookie, injected into the browser at startup.

- **Single token** → set `CHATGPT_SESSION_TOKEN` to the value of `__Secure-next-auth.session-token`.
- **Chunked token** (you see `…session-token.0` **and** `.1`) → leave `CHATGPT_SESSION_TOKEN` empty and
  paste the **whole cookie string** into `CHATGPT_COOKIES`. Easiest grab:
  *DevTools → Network → any `chatgpt.com` request → Request Headers → copy the entire `Cookie:` value.*

> [!TIP]
> Treat the cookie like a password (it lives only in `.env`, which is git-ignored). It expires after a
> while — when the logs say `looks logged-out`, paste a fresh one and `docker compose restart api`.

Cloudflare cookies are skipped by default (they're bound to the UA+IP that made them); the browser earns
its own. If CF blocks you, run locally with `HEADLESS=false`, or set `INJECT_CF_COOKIES=true`.

## 🔌 Using the API

```bash
curl http://localhost:8850/v1/chat/completions \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"hello"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8850/v1", api_key="ripgpt-local")
resp = client.chat.completions.create(
    model="auto",   # or gpt-5.5-thinking, o3, …
    messages=[{"role": "user", "content": "Tell me a fun fact about the Roman Empire"}],
)
print(resp.choices[0].message.content)
```

### Models

The requested model is **forced per request** by rewriting the outgoing conversation
request (robust, no fragile UI-menu clicking) — verified end-to-end via the model the
server reports back.

| Model | Behaviour |
|---|---|
| `auto` | picks from your prompt — a fast model for simple asks, a thinking model when it smells like code / math / reasoning / long input |
| `gpt-5.5` · `gpt-5.5-instant` · `gpt-5.5-thinking` | the GPT‑5.5 tiers |
| `gpt-5.4-thinking` · `gpt-5.3` · `o3` | other models |
| `chatgpt` | uses whatever model is selected in ChatGPT, and **saves** the chat to your history (the rest are temporary) |

> [!IMPORTANT]
> ripgpt drives a **single** browser through ChatGPT's real anti-abuse. Firing requests
> too fast trips rate-limits and wedges the session (empty replies / endless "Thinking").
> Keep it to a calm, sequential pace. If it gets stuck: `docker compose restart api`.

> Tip: `docker logs llmonline-api-1 | grep model_slug` shows which model actually answered.

## ⚙️ Configuration (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `API_KEY` | `ripgpt-local` | Bearer key clients must send (also reused by Open WebUI). Empty = no auth. |
| `CHATGPT_SESSION_TOKEN` | — | Single session-token cookie value. |
| `CHATGPT_COOKIES` | — | Full cookie string (use this for chunked tokens). |
| `INJECT_CF_COOKIES` | `false` | Also inject Cloudflare cookies (same machine only). |
| `WEBUI_AUTH` | `true` | Open WebUI login screen — `false` for no-login local use. |
| `OPENWEBUI_PORT` | `3001` | Host port for the web UI. |
| `HEADLESS` | `true` | Run Firefox headless. Flip to `false` to watch / debug Cloudflare. |
| `ANSWER_TIMEOUT` | `300` | Max seconds to wait for an answer (thinking models are slow). |
| `DOM_STABLE_SECS` | `2.5` | Fallback "answer finished" detector if markers change. |

## 🩹 Troubleshooting

<details>
<summary><b>Open WebUI just spins / "Thinking" forever</b></summary>

Start a **new** chat after fixing config (stuck messages from a bad run won't recover). ripgpt drives a
single browser, so Open WebUI's background calls (title/tags/autocomplete) are disabled in compose to
avoid thrashing.
</details>

<details>
<summary><b>Logs say <code>looks logged-out</code></b></summary>

Your cookie expired. Grab a fresh `Cookie:` string → `CHATGPT_COOKIES` in `.env` → `docker compose restart api`.
</details>

<details>
<summary><b>Cloudflare keeps challenging</b></summary>

Run locally with `HEADLESS=false` to solve it once, and/or `INJECT_CF_COOKIES=true` if running on the same
machine/UA you copied the cookies from.
</details>

<details>
<summary><b>Docker errors / <code>input/output error</code></b></summary>

Check your host disk — a full disk corrupts Docker's VM. Keep some GB free. (Ask me how I know. 💀)
</details>

## 🧱 Project structure

```
app/
├── api.py              # FastAPI: OpenAI-compatible routes + SSE
├── browser.py          # Playwright session, fetch+WS interceptors, v1 delta parser
├── session_service.py  # single-browser worker queue + health/relogin
└── openai_models.py    # request/response models
Dockerfile · docker-compose.yml · .env.example
```

## 📜 License

Private project. Do whatever you want with it — just don't be the reason this stops working. 😉

<div align="center">
<sub>Built with curiosity, a HAR file, and a dangerously full disk.</sub>
</div>
