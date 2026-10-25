"""
auth.py
A minimal access gate for the internal deployment: one shared password that the
team is given, plus the user identifying themselves with a company email address.

What this is, and is not:
  * The password is the actual control.
  * The email is **recorded, not verified** - anyone holding the password can
    type any address on the allowed domain. It tells you who is using the app;
    it is not proof of identity.
  * This is meant to sit alongside the app being reachable only on the internal
    network, not to replace SSO.

The gate is **off unless APP_PASSWORD is set**, so local development and any
deployment without the variable behave exactly as before.

Swapping in a stronger scheme later (SSO, email OTP) only means changing this
file - the rest of the app just asks whether a user is in the session.
"""

from __future__ import annotations

import hmac
import re
import time

from . import config

DEFAULT_DOMAIN = "example.com"
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60

# Logical store holding the allowlist, so it can be edited from the app (and in
# the Zoho Sheet) without a redeploy.
ACCESS_STORE = "access"
ACCESS_KEY = "allowed_users"
_ALLOWLIST_TTL = 60  # seconds; keeps login fast without a sheet read every time

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# IP -> (failure count, time of last failure). In-memory, so with several
# gunicorn workers each holds its own view: this slows down casual guessing
# rather than being a hard rate limit.
_failures: dict[str, tuple[int, float]] = {}


def _cfg() -> dict:
    return config.section("auth") or {}


def is_enabled() -> bool:
    """Auth is opt-in: no APP_PASSWORD means no login screen."""
    return bool(_cfg().get("app_password"))


def allowed_domain() -> str:
    return (_cfg().get("email_domain") or DEFAULT_DOMAIN).strip().lstrip("@").lower()


def session_days() -> int:
    try:
        return max(int(_cfg().get("session_days") or 7), 1)
    except (TypeError, ValueError):
        return 7


def cookie_secure() -> bool:
    """Only send the session cookie over HTTPS. Off by default because the
    internal deployment may well be plain HTTP behind a proxy."""
    return str(_cfg().get("cookie_secure", "")).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------- allowlist ---
# Effective list = the Zoho Sheet entry ∪ AUTH_ALLOWED_EMAILS.
#
# The env-var half exists for two reasons: it seeds the very first list (the
# sheet starts empty, and you must already be signed in to edit it), and it is
# the way back in if Zoho is unreachable. Changing it needs server access, so it
# is an administrator escape hatch rather than a hole.
#
# Fail closed: if both are empty, nobody gets in.

_allowlist_cache: tuple[float, frozenset] = (0.0, frozenset())


def _env_allowed() -> set:
    raw = _cfg().get("allowed_emails") or ""
    return {e.strip().lower() for e in re.split(r"[,;\s]+", raw) if e.strip()}


def _stored_allowed() -> set:
    """Emails from the shared store. Returns an empty set if it can't be read -
    the env-var list is then the only way in, by design."""
    try:
        from . import store

        data = store.get_backend().load_all(ACCESS_STORE).get(ACCESS_KEY) or []
        if isinstance(data, dict):          # tolerate {"emails": [...]}
            data = data.get("emails", [])
        return {str(e).strip().lower() for e in data if str(e).strip()}
    except Exception:
        return set()


def allowed_users(refresh: bool = False) -> frozenset:
    global _allowlist_cache
    age = time.time() - _allowlist_cache[0]
    if refresh or age > _ALLOWLIST_TTL or not _allowlist_cache[1]:
        combined = frozenset(_env_allowed() | _stored_allowed())
        _allowlist_cache = (time.time(), combined)
    return _allowlist_cache[1]


def invalidate_allowlist() -> None:
    global _allowlist_cache
    _allowlist_cache = (0.0, frozenset())


def set_allowed_users(emails) -> None:
    """Persist the list (only the sheet half; env entries always apply)."""
    from . import store

    cleaned, seen = [], set()
    for e in emails:
        e = str(e).strip().lower()
        if e and e not in seen and _EMAIL_RE.match(e):
            cleaned.append(e)
            seen.add(e)
    store.get_backend().save_one(ACCESS_STORE, ACCESS_KEY, cleaned)
    invalidate_allowlist()


def stored_users() -> list:
    """Just the editable (sheet) half, for the management screen."""
    return sorted(_stored_allowed())


def env_users() -> list:
    return sorted(_env_allowed())


def locked_out(ip: str) -> int:
    """Seconds remaining before this IP may try again (0 = go ahead)."""
    count, last = _failures.get(ip, (0, 0.0))
    if count < MAX_FAILURES:
        return 0
    remaining = LOCKOUT_SECONDS - (time.time() - last)
    if remaining <= 0:
        _failures.pop(ip, None)
        return 0
    return int(remaining) + 1


def record_failure(ip: str) -> None:
    count, _ = _failures.get(ip, (0, 0.0))
    _failures[ip] = (count + 1, time.time())


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def validate_email(email: str) -> tuple[bool, str]:
    """Shape + domain check, without touching the password or allowlist."""
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return False, f"'{email}' isn't a valid email address."
    domain = allowed_domain()
    if not email.endswith("@" + domain):
        return False, f"'{email}' is not an @{domain} address."
    return True, ""


def check(email: str, password: str) -> tuple[bool, str]:
    """Validate a login attempt. Returns (ok, error_message)."""
    email = (email or "").strip().lower()
    password = password or ""

    if not email or not password:
        return False, "Enter your email address and the team password."
    if not _EMAIL_RE.match(email):
        return False, "That doesn't look like a valid email address."

    domain = allowed_domain()
    if not email.endswith("@" + domain):
        return False, f"Use your @{domain} email address."

    expected = _cfg().get("app_password") or ""
    # Constant-time comparison so the password can't be recovered a character
    # at a time by measuring response times.
    if not hmac.compare_digest(password, expected):
        return False, "Incorrect password."

    # Allowlist is checked *after* the password on purpose: otherwise anyone
    # could probe which addresses are registered without knowing the password.
    allowed = allowed_users()
    if not allowed:
        return False, ("No one is authorised to sign in yet. An administrator needs to add "
                       "email addresses (AUTH_ALLOWED_EMAILS) before the app can be used.")
    if email not in allowed:
        return False, "This email address isn't authorised to use this app."

    return True, ""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()
