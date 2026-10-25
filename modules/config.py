"""
config.py
Configuration for Jira, Zoho Cliq and the Zoho Sheet store.

Replaces the Streamlit `st.secrets` lookups the app used to rely on. Values come
from environment variables, optionally seeded from a local `.env` file (never
committed). This keeps credentials out of the repo and works the same whether the
app runs under Flask, in Docker, or from a cron script.

Unlike the previous implementation, a *partial* configuration is reported rather
than silently ignored - a missing variable used to make the app quietly fall back
to local files instead of the Zoho Sheet.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
_ENV_LOADED = False

# section -> {config key: env var}
SCHEMA = {
    "jira": {
        "base_url": "JIRA_BASE_URL",
        "email": "JIRA_EMAIL",
        "api_token": "JIRA_API_TOKEN",
        "target_start_field": "JIRA_TARGET_START_FIELD",   # optional
        "target_end_field": "JIRA_TARGET_END_FIELD",       # optional
    },
    "cliq": {
        "client_id": "CLIQ_CLIENT_ID",
        "client_secret": "CLIQ_CLIENT_SECRET",
        "refresh_token": "CLIQ_REFRESH_TOKEN",
        "location": "CLIQ_LOCATION",                       # optional, default in client
    },
    "zoho": {
        "client_id": "ZOHO_CLIENT_ID",
        "client_secret": "ZOHO_CLIENT_SECRET",
        "refresh_token": "ZOHO_REFRESH_TOKEN",
        "resource_id": "ZOHO_RESOURCE_ID",
        "location": "ZOHO_LOCATION",                       # optional
        "worksheet_name": "ZOHO_WORKSHEET_NAME",           # optional
    },
    # All optional - setting APP_PASSWORD is what switches the login gate on.
    "auth": {
        "app_password": "APP_PASSWORD",
        "email_domain": "AUTH_EMAIL_DOMAIN",
        "allowed_emails": "AUTH_ALLOWED_EMAILS",
        "session_days": "SESSION_DAYS",
        "cookie_secure": "SESSION_COOKIE_SECURE",
    },
}

REQUIRED = {
    "jira": ["base_url", "email", "api_token"],
    "cliq": ["client_id", "client_secret", "refresh_token"],
    "zoho": ["client_id", "client_secret", "refresh_token", "resource_id"],
}


def load_env(path: str | os.PathLike | None = None) -> None:
    """Seed os.environ from a .env file (KEY=value, # comments). Existing
    environment variables always win, so real env config beats the file."""
    global _ENV_LOADED
    if _ENV_LOADED and path is None:
        return
    env_path = Path(path) if path else APP_DIR / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    if path is None:
        _ENV_LOADED = True


def _clean(raw: str) -> str:
    """Tidy a value that arrived through an env var / Kubernetes Secret.

    Secrets are hand-assembled often enough that values pick up surrounding
    quotes or a trailing newline - `KEY="value"` in a --from-env-file, a YAML
    block scalar, an `echo` into a file. None of the settings here may legally
    contain a quote at the edges or any newline, and sending one produces a
    baffling failure: Zoho answers a token request with an HTML error page
    rather than a JSON error, so the app reports HTTP 400 with no reason.
    """
    val = (raw or "").strip()
    # Matching surrounding quotes are never part of a real value.
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1].strip()
    # Newlines and tabs cannot appear in any of these settings.
    for code in (13, 10, 9):          # CR, LF, TAB
        val = val.replace(chr(code), "")
    return val


def section(name: str) -> dict | None:
    """Full config for a section, or None when the required keys aren't all set."""
    load_env()
    mapping = SCHEMA.get(name, {})
    values = {}
    for key, env_var in mapping.items():
        val = _clean(os.environ.get(env_var, ""))
        if val:
            values[key] = val
    if all(values.get(k) for k in REQUIRED.get(name, [])):
        return values
    return None


def missing(name: str) -> list:
    """Which required env vars are absent - used for a clear startup message."""
    load_env()
    mapping = SCHEMA.get(name, {})
    return [mapping[k] for k in REQUIRED.get(name, []) if not _clean(os.environ.get(mapping[k], ""))]


def status() -> dict:
    """{section: (configured?, [missing env vars])} for the UI/health check."""
    return {name: (section(name) is not None, missing(name)) for name in SCHEMA}


DEV_SECRET_KEY = "sprint-report-generator-dev-key"


def secret_key(auth_enabled: bool) -> tuple[str, str | None]:
    """The key used to sign session cookies. Returns (key, warning).

    The session cookie is what keeps someone logged in, so a predictable key
    means a forged cookie walks straight past the login screen. When the gate is
    on we therefore refuse to fall back to the committed development key and
    generate a random one instead - the app still runs, but everyone is signed
    out whenever it restarts, which is a visible nudge to set a real value.
    """
    load_env()
    key = os.environ.get("SECRET_KEY", "").strip()
    if key and key != DEV_SECRET_KEY:
        return key, None
    if not auth_enabled:
        return DEV_SECRET_KEY, None
    import secrets

    return secrets.token_hex(32), (
        "SECRET_KEY is not set, so a random one was generated: everyone will be "
        "signed out each time the app restarts. Set SECRET_KEY to a long random "
        "string in your environment."
    )
