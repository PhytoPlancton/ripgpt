"""Persistent registry of extra ChatGPT accounts added from the admin UI.

SECURITY: this stores ONLY metadata (id, label, added-timestamp) — never the cookie.
A pasted cookie is used transiently to bootstrap that account's browser profile, then
discarded; the persistent profile (a volume dir per account) keeps the auto-renewed session,
exactly like the main account. So a fresh redeploy re-logs-in from the profile with no cookie;
if a profile is ever lost the account shows logged-out and the admin re-connects it (paste again).

Backed by accounts.json on the profile volume (0600), mirroring keystore.py.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time

_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR") or "."
ACCOUNTS_FILE = os.environ.get("ACCOUNTS_FILE") or os.path.join(_PROFILE_DIR, "accounts.json")


class AccountStore:
    def __init__(self, path: str = ACCOUNTS_FILE):
        self._lock = threading.Lock()
        self._path = path
        self._accounts: list[dict] = []   # [{id, label, added}]
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            return
        accts = data.get("accounts") if isinstance(data, dict) else None
        if isinstance(accts, list):
            self._accounts = [a for a in accts if isinstance(a, dict) and a.get("id")]

    def _save_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"accounts": self._accounts}, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    def add(self, label: str) -> dict:
        rec = {"id": secrets.token_hex(6), "label": (label or "compte").strip()[:60],
               "added": int(time.time())}
        with self._lock:
            self._accounts.append(rec)
            self._save_locked()
        return dict(rec)

    def remove(self, account_id: str) -> bool:
        with self._lock:
            before = len(self._accounts)
            self._accounts = [a for a in self._accounts if a.get("id") != account_id]
            if len(self._accounts) != before:
                self._save_locked()
                return True
            return False

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(a) for a in self._accounts]


ACCOUNTS = AccountStore()
