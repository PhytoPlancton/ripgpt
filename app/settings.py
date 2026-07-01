"""Runtime-editable settings for ripgpt, persisted on the data volume.

Mirrors app/keystore.py: a small thread-safe JSON store on BROWSER_PROFILE_DIR (atomic
0600 write, self-healing load). Holds the ban-governor / admission knobs so they can be
changed from the admin UI with NO restart and NO .env editing, plus a PRIVATE admin
credential override (the file value wins over ADMIN_USER / ADMIN_PASSWORD_HASH env once set).

Leaf module: imported BY ratelimit.py / api.py / auth.py; it imports none of them (no cycle).
The env vars are read ONCE here (first-run seed) — the old per-module env parsing moves here.
"""

from __future__ import annotations

import json
import math
import os
import threading

_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR") or "."
SETTINGS_FILE = os.environ.get("SETTINGS_FILE") or os.path.join(_PROFILE_DIR, "settings.json")

# Public runtime knobs. env = first-run seed source; default = code fallback; min/max are
# hard bounds enforced on every write (and clamped on self-heal load).
SCHEMA: dict[str, dict] = {
    "rate_per_min": {
        "type": "int", "env": "RATE_PER_MIN", "default": 20, "min": 0, "max": 600,
        "zero_disables": True, "label": "Requêtes / minute", "help": "0 = illimité"},
    "rate_per_hour": {
        "type": "int", "env": "RATE_PER_HOUR", "default": 300, "min": 0, "max": 20000,
        "zero_disables": True, "label": "Requêtes / heure", "help": "0 = illimité"},
    "rate_per_day": {
        "type": "int", "env": "RATE_PER_DAY", "default": 2000, "min": 0, "max": 200000,
        "zero_disables": True, "label": "Requêtes / jour", "help": "garde-fou anti-ban · 0 = illimité"},
    "rate_min_interval_s": {
        "type": "float", "env": "RATE_MIN_INTERVAL_S", "default": 0.0, "min": 0.0, "max": 300.0,
        "zero_disables": True, "label": "Intervalle min (s)", "help": "délai mini entre 2 requêtes · 0 = off"},
    "breaker_threshold": {
        "type": "int", "env": "RATE_BREAKER_THRESHOLD", "default": 4, "min": 1, "max": 100,
        "zero_disables": False, "label": "Seuil disjoncteur", "help": "signaux d'affilée avant cooldown"},
    "breaker_cooldown_s": {
        "type": "float", "env": "RATE_BREAKER_COOLDOWN_S", "default": 600.0, "min": 1.0, "max": 86400.0,
        "zero_disables": False, "label": "Cooldown (s)", "help": "pause après déclenchement du disjoncteur"},
    "max_queue_depth": {
        "type": "int", "env": "MAX_QUEUE_DEPTH", "default": 12, "min": 1, "max": 1000,
        "zero_disables": False, "label": "File d'attente max", "help": "au-delà → 503 surcharge"},
}


def _coerce(meta: dict, value):
    if meta["type"] == "int":
        return int(value)
    v = float(value)
    if math.isnan(v) or math.isinf(v):
        raise ValueError("not a finite number")
    return v


def _clamp(meta: dict, value):
    return max(meta["min"], min(meta["max"], value))


class SettingsStore:
    def __init__(self, path: str = SETTINGS_FILE):
        self._lock = threading.Lock()
        self._path = path
        self._settings = {k: m["default"] for k, m in SCHEMA.items()}
        self._loaded_keys: set[str] = set()   # keys that came from the file (not defaults)
        self._admin: dict = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            return   # corrupt → keep defaults, don't overwrite
        stored = data.get("settings") or {}
        for k, meta in SCHEMA.items():
            if k in stored:
                try:
                    self._settings[k] = _clamp(meta, _coerce(meta, stored[k]))  # self-heal (clamp)
                    self._loaded_keys.add(k)
                except Exception:
                    pass
        adm = data.get("admin")
        if isinstance(adm, dict):
            self._admin = adm

    def _save_locked(self) -> None:
        data = {"version": 1, "settings": dict(self._settings), "admin": dict(self._admin)}
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ── first-run seed from env (env parsing lives HERE now) ─────────────────────
    def seed_from_env(self) -> None:
        changed = not os.path.exists(self._path)
        with self._lock:
            for k, meta in SCHEMA.items():
                if k in self._loaded_keys:
                    continue
                raw = os.environ.get(meta["env"])
                if raw is None or raw == "":
                    continue
                try:
                    self._settings[k] = _clamp(meta, _coerce(meta, raw))
                    self._loaded_keys.add(k)
                    changed = True
                except Exception:
                    pass
            if changed:
                self._save_locked()

    # ── public read ──────────────────────────────────────────────────────────────
    def get(self, name: str):
        with self._lock:
            return self._settings[name]

    def all(self) -> dict:
        with self._lock:
            return dict(self._settings)   # 7 public keys only — never the admin block

    @staticmethod
    def bounds() -> dict:
        return {k: dict(m) for k, m in SCHEMA.items()}

    # ── public write ─────────────────────────────────────────────────────────────
    def update(self, patch: dict) -> tuple[dict, list[str]]:
        """Validate + apply a partial patch. Transactional: any bad value → apply none."""
        errors: list[str] = []
        staged: dict = {}
        if not isinstance(patch, dict):
            return self.all(), ["Invalid payload."]
        for k, v in patch.items():
            if k not in SCHEMA:
                errors.append(f"{k}: unknown setting")
                continue
            meta = SCHEMA[k]
            try:
                cv = _coerce(meta, v)
            except Exception:
                errors.append(f"{meta['label']}: not a valid {meta['type']}")
                continue
            if cv < meta["min"] or cv > meta["max"]:
                errors.append(f"{meta['label']}: must be between {meta['min']} and {meta['max']}")
                continue
            staged[k] = cv
        if errors:
            return self.all(), errors
        with self._lock:
            self._settings.update(staged)
            self._loaded_keys.update(staged.keys())
            self._save_locked()
            return dict(self._settings), []

    # ── admin credential override (private — never surfaced by all()/bounds()) ────
    def admin_creds(self) -> tuple[str | None, str | None]:
        with self._lock:
            return self._admin.get("user"), self._admin.get("password_hash")

    def set_admin(self, user: str | None, password_hash: str) -> None:
        with self._lock:
            self._admin = {"user": (user or "").strip() or None, "password_hash": password_hash}
            self._save_locked()


SETTINGS = SettingsStore()
