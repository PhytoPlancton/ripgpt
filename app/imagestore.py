"""Tiny in-process image store.

Generated images are served via a short URL (GET /images/{id}) instead of being
inlined as multi-MB base64 data URLs — clients like OpenWebUI render a giant
inline data URL as raw text instead of an image. Bytes live in memory with a TTL
and a hard cap so a long-running proxy never grows without bound.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict

_MAX_IMAGES = int(os.environ.get("IMAGE_STORE_MAX", "40"))
_TTL_SECONDS = int(os.environ.get("IMAGE_STORE_TTL", "10800"))  # 3h

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/webp": "webp", "image/gif": "gif"}


def ext_for(mime: str) -> str:
    return _EXT.get((mime or "").lower(), "png")


class ImageStore:
    def __init__(self, max_items: int = _MAX_IMAGES, ttl: float = _TTL_SECONDS):
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        self._max = max(1, max_items)
        self._ttl = ttl

    def put(self, data: bytes, mime: str) -> str:
        iid = uuid.uuid4().hex
        with self._lock:
            self._evict_locked()
            self._items[iid] = {"data": data, "mime": mime, "ts": time.time()}
        return iid

    def get(self, iid: str):
        with self._lock:
            it = self._items.get(iid)
            if not it:
                return None
            if time.time() - it["ts"] > self._ttl:
                self._items.pop(iid, None)
                return None
            return it["data"], it["mime"]

    def _evict_locked(self) -> None:
        now = time.time()
        stale = [k for k, v in self._items.items() if now - v["ts"] > self._ttl]
        for k in stale:
            self._items.pop(k, None)
        while len(self._items) >= self._max:
            self._items.popitem(last=False)   # drop oldest


IMAGES = ImageStore()
