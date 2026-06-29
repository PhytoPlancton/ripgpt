"""Admin authentication: password hashing, signed session cookies, CSRF, rate-limit.

Standard-library only (hashlib/hmac/secrets) — no bcrypt/itsdangerous wheels to build in
the slim Docker image. Design notes:

* Password: PBKDF2-HMAC-SHA256, salted, stored as `pbkdf2$iters$salt_b64$hash_b64`.
* Session: an HMAC-SHA256 signed, expiring token in an HttpOnly cookie. Stateless —
  nothing to store server-side. A bumpable token_version allows "log out everywhere".
* CSRF: double-submit. A readable cookie value must match an X-CSRF-Token header on
  state-changing requests (SameSite=Lax is the first line of defence; this is the second).
* Login rate-limit: per-IP lockout after repeated failures, in-memory.

The signing secret and token_version persist next to the browser profile so sessions and
the password survive restarts. If no creds are configured the admin area is fail-closed
(no login possible) rather than fail-open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

SESSION_COOKIE = "ripgpt_admin"
CSRF_COOKIE = "ripgpt_csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_TTL = int(os.environ.get("ADMIN_SESSION_TTL", str(12 * 3600)))   # 12h

ADMIN_USER = (os.environ.get("ADMIN_USER") or "").strip()
ADMIN_PASSWORD_HASH = (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip()

_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR") or "."
_SECRET_PATH = os.path.join(_PROFILE_DIR, ".session_secret")
_TV_PATH = os.path.join(_PROFILE_DIR, ".admin_token_version")

_PBKDF2_ITERS = 210_000

# Only honour proxy-set client headers (CF-Connecting-IP, X-Forwarded-For/-Proto) when
# the operator explicitly opts in — i.e. the app is reachable ONLY through a trusted
# proxy/tunnel that overwrites them. Default OFF so a directly-reachable port can't be
# used to spoof the client IP (rate-limit bypass) or the scheme (cookie Secure downgrade).
TRUST_PROXY_HEADERS = (os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower()
                       in ("1", "true", "yes", "on"))

# Login throttle.
MAX_FAILS = 6
LOCKOUT_SECONDS = 300
_FAILS_MAX_ENTRIES = 4096          # hard cap so rotating keys can't grow memory


# ── signing secret (persisted, or from env) ──────────────────────────────────
def _load_or_create_secret() -> bytes:
    env = (os.environ.get("SESSION_SECRET") or "").strip()
    if env:
        return env.encode("utf-8")
    try:
        if os.path.exists(_SECRET_PATH):
            with open(_SECRET_PATH, "rb") as fh:
                data = fh.read()
            if data:
                return data
    except Exception:
        pass
    secret = secrets.token_bytes(32)
    try:
        os.makedirs(os.path.dirname(_SECRET_PATH) or ".", exist_ok=True)
        fd = os.open(_SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secret)
    except Exception:
        # Fall back to an ephemeral secret — sessions won't survive a restart, but the
        # app still runs. Better than crashing on a read-only filesystem.
        pass
    return secret


_SECRET = _load_or_create_secret()


# ── password ──────────────────────────────────────────────────────────────────
def hash_password(password: str, iterations: int = _PBKDF2_ITERS) -> str:
    # Use "." as the field separator (NOT "$"): a "$" in an .env value gets mangled by
    # docker-compose interpolation. base64 never contains ".", so this stays unambiguous.
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2.{}.{}.{}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        # Accept both the "." format and the legacy "$" format (defensive).
        sep = "." if stored.startswith("pbkdf2.") else "$"
        algo, iters, salt_b64, hash_b64 = stored.split(sep)
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def admin_configured() -> bool:
    return bool(ADMIN_USER and ADMIN_PASSWORD_HASH)


def check_login(user: str, password: str) -> bool:
    if not admin_configured():
        return False
    # Compare both fields in constant time; evaluate the password hash regardless of the
    # username result so timing doesn't reveal whether the username was correct.
    user_ok = hmac.compare_digest((user or "").strip(), ADMIN_USER)
    pass_ok = verify_password(password or "", ADMIN_PASSWORD_HASH)
    return user_ok and pass_ok


# ── token_version (global logout) ────────────────────────────────────────────
def _token_version() -> int:
    try:
        with open(_TV_PATH, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or "1")
    except Exception:
        return 1


def bump_token_version() -> None:
    nv = _token_version() + 1
    try:
        os.makedirs(os.path.dirname(_TV_PATH) or ".", exist_ok=True)
        with open(_TV_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(nv))
    except Exception:
        pass


# ── session token ─────────────────────────────────────────────────────────────
def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_dec(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_session(uid: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    payload = "{}|{}|{}".format(uid, _token_version(), exp).encode("utf-8")
    sig = hmac.new(_SECRET, payload, hashlib.sha256).digest()
    return _b64u(payload) + "." + _b64u(sig)


def read_session(cookie: str | None) -> str | None:
    """Return the uid from a valid, unexpired, correctly-signed session, else None."""
    if not cookie or "." not in cookie:
        return None
    try:
        body_b64, sig_b64 = cookie.split(".", 1)
        payload = _b64u_dec(body_b64)
        sig = _b64u_dec(sig_b64)
        expected = hmac.new(_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        uid, tv, exp = payload.decode("utf-8").split("|")
        if int(exp) < int(time.time()):
            return None
        if int(tv) != _token_version():
            return None
        return uid
    except Exception:
        return None


# ── CSRF (double submit) ──────────────────────────────────────────────────────
def issue_csrf() -> str:
    return secrets.token_urlsafe(24)


def verify_csrf(cookie_val: str | None, header_val: str | None) -> bool:
    return bool(cookie_val) and bool(header_val) and hmac.compare_digest(cookie_val, header_val)


# ── request helpers ───────────────────────────────────────────────────────────
def client_ip(request) -> str:
    # Only trust proxy-set headers when explicitly enabled (the app sits behind a trusted
    # tunnel that overwrites them). Otherwise key on the real socket peer — a directly
    # reachable port must not let a client spoof its IP to dodge the login lockout.
    if TRUST_PROXY_HEADERS:
        h = request.headers
        spoofable = (h.get("cf-connecting-ip")
                     or h.get("x-forwarded-for", "").split(",")[0].strip())
        if spoofable:
            return spoofable
    return request.client.host if request.client else "unknown"


def cookie_secure(request) -> bool:
    """Decide the cookie Secure flag without trusting a spoofable header by default.

    When TRUST_PROXY_HEADERS is on, honour X-Forwarded-Proto (TLS terminates at the
    tunnel). Otherwise derive it from the operator-set PUBLIC_BASE_URL scheme (a client
    can't forge it); if that's unset, fall back to the request's own scheme so a direct
    HTTPS origin still gets Secure cookies and plain-http localhost still works."""
    if TRUST_PROXY_HEADERS:
        xfp = request.headers.get("x-forwarded-proto", "")
        if xfp:
            return xfp.split(",")[0].strip().lower() == "https"
    base = (os.environ.get("PUBLIC_BASE_URL", "") or "").lower()
    if base:
        return base.startswith("https")
    try:
        return request.url.scheme == "https"
    except Exception:
        return False


# ── login rate-limit (in-memory, per-IP) ──────────────────────────────────────
# Per-IP lockout: MAX_FAILS wrong attempts → LOCKOUT_SECONDS cool-off. The IP key is the
# real socket peer by default (unspoofable); behind a trusted tunnel set TRUST_PROXY_HEADERS
# so it's the real client IP (CF-Connecting-IP) rather than the shared tunnel peer — that
# way an attacker's failures lock the attacker, not the admin. The dict is bounded + swept
# so rotating keys can't grow memory without limit. (No global ceiling: a global cap would
# let one attacker 429 the admin out, a worse failure mode than slow per-IP guessing against
# a strong PBKDF2-hashed password.)
_rl_lock = threading.Lock()
_fails: dict[str, tuple[int, float]] = {}   # ip -> (count, lockout_until)


def _prune_locked(now: float) -> None:
    # Drop entries whose lockout has expired and whose count is 0 (nothing to remember).
    dead = [k for k, (c, until) in _fails.items() if until <= now and c == 0]
    for k in dead:
        _fails.pop(k, None)
    # Hard cap: if still oversized, evict arbitrary entries (memory safety over fairness).
    while len(_fails) > _FAILS_MAX_ENTRIES:
        _fails.pop(next(iter(_fails)), None)


def login_locked(ip: str) -> float:
    """Return seconds remaining in this IP's lockout (0 if not locked)."""
    now = time.time()
    with _rl_lock:
        _count, until = _fails.get(ip, (0, 0.0))
        rem = until - now
        return rem if rem > 0 else 0.0


def record_login_fail(ip: str) -> None:
    now = time.time()
    with _rl_lock:
        count, until = _fails.get(ip, (0, 0.0))
        count += 1
        if count >= MAX_FAILS:
            until = now + LOCKOUT_SECONDS
            count = 0   # reset the counter; the lockout window now governs
        _fails[ip] = (count, until)
        _prune_locked(now)   # bound size after insert so the invariant len<=cap holds


def reset_login_fails(ip: str) -> None:
    with _rl_lock:
        _fails.pop(ip, None)
