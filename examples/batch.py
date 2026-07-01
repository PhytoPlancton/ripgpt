#!/usr/bin/env python3
"""Batch personalization for ripgpt — the single biggest throughput lever.

ripgpt drives ONE ChatGPT browser serially, so one API call per item is slow. This packs
many items into ONE call ("here are 25 prospects as JSON, return 25 personalized outputs as
a JSON array"), which multiplies effective throughput ~15-30x with ZERO change to the proxy.

It runs ABOVE the proxy (plain HTTP to /v1/chat/completions), so the running deployment is
untouched. Serial by design (concurrency=1) + backoff on 429/503, because the backend is a
single shared browser.

Usage:
    export RIPGPT_API_KEY=rip-xxxxx
    python batch.py \
        --input prospects.json \
        --output results.json \
        --instruction "Écris une accroche d'email de prospection (2 phrases, ton direct)." \
        --model gpt-5.5-instant --batch-size 25

Input: a JSON array of objects (or strings), or a CSV (with a header row → objects).
Output: the same items with an added "output" field (null if it couldn't be produced).

Only dependency: requests  (pip install requests)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

import requests

# ── model I/O ────────────────────────────────────────────────────────────────
_SYSTEM = (
    "You are a batch processor. You will receive a task and a JSON array of input items, "
    "each with an integer \"i\". Apply the task to EACH item and reply with ONLY a JSON array "
    "of objects {\"i\": <same index>, \"text\": <result string>}. Return one object per input "
    "item, same indices, no commentary, no markdown fences — just the raw JSON array."
)


def build_prompt(instruction: str, indexed_items: list[dict]) -> str:
    return (
        f"TASK (apply to every item):\n{instruction}\n\n"
        f"INPUT ITEMS (JSON):\n{json.dumps(indexed_items, ensure_ascii=False)}\n\n"
        f"Reply with ONLY the JSON array of {{\"i\", \"text\"}} objects, one per item."
    )


def extract_json_array(text: str):
    """Tolerantly pull a JSON array out of a model reply (handles ``` fences / prose)."""
    if not text:
        return None
    t = text.strip()
    if "```" in t:                      # strip a ```json ... ``` fence if present
        parts = t.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("["):
                t = p
                break
    start = t.find("[")
    end = t.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        val = json.loads(t[start:end + 1])
        return val if isinstance(val, list) else None
    except Exception:
        return None


def call_model(base_url: str, api_key: str, model: str, prompt: str,
               timeout: float, max_retries: int = 5) -> str:
    """POST one chat completion, with exponential backoff on 429/503/transient."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": prompt},
    ]}
    for attempt in range(max_retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code in (429, 503):
            wait = r.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.isdigit() else min(2 ** attempt, 30))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError("ripgpt busy — exhausted retries")


# ── batching orchestration ───────────────────────────────────────────────────
def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield i, seq[i:i + size]


def run(items, instruction, *, base_url, api_key, model, batch_size, timeout,
        sleep, repair_rounds, log=print):
    """Return a list `outputs` aligned to `items` (outputs[k] is str or None)."""
    outputs: list[str | None] = [None] * len(items)

    def item_payload(idx):
        it = items[idx]
        return {"i": idx, "item": it}

    pending = list(range(len(items)))
    total_calls = 0
    for rnd in range(repair_rounds + 1):
        if not pending:
            break
        if rnd:
            log(f"  repair pass {rnd}: {len(pending)} item(s) still missing")
        next_pending: list[int] = []
        for _, idx_batch in _chunks(pending, batch_size):
            payload = [item_payload(i) for i in idx_batch]
            prompt = build_prompt(instruction, payload)
            total_calls += 1
            try:
                reply = call_model(base_url, api_key, model, prompt, timeout)
            except Exception as e:
                log(f"  batch failed ({e}) — will retry those items")
                next_pending.extend(idx_batch)
                continue
            arr = extract_json_array(reply)
            got = {}
            if arr:
                for obj in arr:
                    if isinstance(obj, dict) and "i" in obj:
                        try:
                            got[int(obj["i"])] = str(obj.get("text", "")).strip()
                        except Exception:
                            pass
            for i in idx_batch:
                if got.get(i):
                    outputs[i] = got[i]
                else:
                    next_pending.append(i)
            done = sum(1 for o in outputs if o is not None)
            log(f"  {done}/{len(items)} done  (call #{total_calls})")
            if sleep:
                time.sleep(sleep)
        pending = next_pending

    return outputs, total_calls


# ── i/o + cli ────────────────────────────────────────────────────────────────
def load_items(path: str):
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise SystemExit("Input JSON must be an array.")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch personalization via ripgpt.")
    ap.add_argument("--input", required=True, help="JSON array or CSV of items")
    ap.add_argument("--output", required=True, help="output JSON path")
    ap.add_argument("--instruction", required=True, help="task applied to each item")
    ap.add_argument("--model", default="gpt-5.5-instant")
    ap.add_argument("--base-url", default=os.environ.get("RIPGPT_BASE_URL", "https://ripgpt.nmt.ovh/v1"))
    ap.add_argument("--batch-size", type=int, default=25, help="items per ChatGPT call (20-30 ideal)")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--sleep", type=float, default=2.0, help="pause between calls (be gentle)")
    ap.add_argument("--repair-rounds", type=int, default=2, help="extra passes to fill missing items")
    args = ap.parse_args()

    api_key = os.environ.get("RIPGPT_API_KEY")
    if not api_key:
        raise SystemExit("Set RIPGPT_API_KEY (never hardcode your key).")

    items = load_items(args.input)
    if not items:
        raise SystemExit("No items in input.")
    print(f"{len(items)} items · batch {args.batch_size} · model {args.model} "
          f"· ~{-(-len(items)//args.batch_size)} call(s)")

    t0 = time.time()
    outputs, calls = run(items, args.instruction, base_url=args.base_url, api_key=api_key,
                         model=args.model, batch_size=args.batch_size, timeout=args.timeout,
                         sleep=args.sleep, repair_rounds=args.repair_rounds)
    results = []
    for it, out in zip(items, outputs):
        row = dict(it) if isinstance(it, dict) else {"input": it}
        row["output"] = out
        results.append(row)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    done = sum(1 for o in outputs if o is not None)
    dt = time.time() - t0
    print(f"\nDone: {done}/{len(items)} in {calls} call(s), {dt:.0f}s "
          f"({dt/max(1,len(items)):.1f}s/item). Missing: {len(items)-done}. -> {args.output}")
    return 0 if done == len(items) else 2


if __name__ == "__main__":
    raise SystemExit(main())
