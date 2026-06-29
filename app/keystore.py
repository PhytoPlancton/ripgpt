"""Persistent store for API keys and a few admin settings (disabled models).

Backed by a single JSON file on the persisted volume (alongside the browser profile),
so it survives restarts with no external service. Keys are stored ONLY as SHA-256
hashes — the plaintext secret is shown to the admin exactly once at creation and never
again. Thread-safe: validated on the API thread while the admin UI mutates concurrently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

# Where to persist. Defaults next to the browser profile (a Docker volume in prod) so
# keys survive restarts; falls back to CWD for local runs without a profile dir.
_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR") or "."
KEYS_FILE = os.environ.get("KEYS_FILE") or os.path.join(_PROFILE_DIR, "keys.json")

KEY_PREFIX = "rip-"
_PERSIST_MIN_INTERVAL = 60.0   # don't rewrite the file more than once a minute for touch()


def generate_key() -> str:
    """A high-entropy bearer token. token_urlsafe(32) → ~43 chars, 256 bits."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class KeyStore:
    def __init__(self, path: str = KEYS_FILE):
        self._lock = threading.Lock()
        self._path = path
        self._by_id: dict[str, dict] = {}
        self._by_hash: dict[str, dict] = {}
        self._disabled_models: set[str] = set()
        self._last_persist = 0.0
        self._load()

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            # Corrupt file: start empty rather than crash, but don't overwrite it yet.
            return
        for rec in data.get("keys", []):
            if not isinstance(rec, dict) or "id" not in rec or "key_hash" not in rec:
                continue
            self._by_id[rec["id"]] = rec
            self._by_hash[rec["key_hash"]] = rec
        settings = data.get("settings") or {}
        self._disabled_models = set(settings.get("disabled_models") or [])

    def _save_locked(self) -> None:
        data = {
            "keys": list(self._by_id.values()),
            "settings": {"disabled_models": sorted(self._disabled_models)},
        }
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            # 0600 so the hashes/metadata aren't world-readable on a shared host.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._path)   # atomic
            self._last_persist = time.time()
        except Exception:
            pass

    # ── public API ───────────────────────────────────────────────────────────
    def seed_from_env(self, api_key: str | None) -> None:
        """Import the legacy single API_KEY env var as a revocable named key.

        Keeps existing integrations working after the switch to a multi-key store.
        Idempotent: re-seeding the same key is a no-op.
        """
        if not api_key:
            return
        h = hash_key(api_key)
        with self._lock:
            if h in self._by_hash:
                return
            rec = {
                "id": "legacy",
                "name": "legacy (API_KEY env)",
                "key_hash": h,
                "prefix": api_key[:6],
                "created": int(time.time()),
                "last_used": None,
                "revoked": False,
            }
            self._by_id["legacy"] = rec
            self._by_hash[h] = rec
            self._save_locked()

    def create(self, name: str) -> tuple[str, dict]:
        """Create a new key. Returns (plaintext_secret, public_record).

        The plaintext is returned ONCE; only its hash is persisted.
        """
        plaintext = generate_key()
        h = hash_key(plaintext)
        rid = secrets.token_hex(8)
        rec = {
            "id": rid,
            "name": (name or "key").strip()[:80],
            "key_hash": h,
            "prefix": plaintext[: len(KEY_PREFIX) + 6],
            "created": int(time.time()),
            "last_used": None,
            "revoked": False,
        }
        with self._lock:
            self._by_id[rid] = rec
            self._by_hash[h] = rec
            self._save_locked()
        return plaintext, self._public(rec)

    def revoke(self, rid: str) -> bool:
        with self._lock:
            rec = self._by_id.get(rid)
            if not rec or rec.get("revoked"):
                return False
            rec["revoked"] = True
            self._save_locked()
            return True

    def list(self) -> list[dict]:
        with self._lock:
            return [self._public(r) for r in sorted(
                self._by_id.values(), key=lambda r: r.get("created", 0))]

    def validate(self, presented: str | None) -> dict | None:
        """Return a copy of the matching non-revoked key record, or None.

        Lookup is by SHA-256 hash (exact dict match); the token itself is 256-bit
        random so there is nothing to brute-force or time-attack here.
        """
        if not presented:
            return None
        h = hash_key(presented)
        with self._lock:
            rec = self._by_hash.get(h)
            if rec is None or rec.get("revoked"):
                return None
            return dict(rec)

    def touch(self, rid: str) -> None:
        """Record last-used. Persists at most once a minute to avoid disk thrash."""
        now = time.time()
        with self._lock:
            rec = self._by_id.get(rid)
            if not rec:
                return
            rec["last_used"] = int(now)
            if now - self._last_persist >= _PERSIST_MIN_INTERVAL:
                self._save_locked()

    # ── model enable/disable ─────────────────────────────────────────────────
    def disabled_models(self) -> set[str]:
        with self._lock:
            return set(self._disabled_models)

    def is_model_enabled(self, model_id: str) -> bool:
        with self._lock:
            return model_id not in self._disabled_models

    def set_model_disabled(self, model_id: str, disabled: bool) -> None:
        with self._lock:
            if disabled:
                self._disabled_models.add(model_id)
            else:
                self._disabled_models.discard(model_id)
            self._save_locked()

    @staticmethod
    def _public(rec: dict) -> dict:
        """Record safe to return over the API — never includes the hash."""
        return {
            "id": rec["id"],
            "name": rec.get("name", ""),
            "prefix": rec.get("prefix", ""),
            "created": rec.get("created"),
            "last_used": rec.get("last_used"),
            "revoked": bool(rec.get("revoked")),
        }


KEYS = KeyStore()
