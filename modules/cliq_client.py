"""
cliq_client.py
Posts a summary message and uploads the generated report files (Excel/PDF/PNG)
to a Zoho Cliq channel.

Configured via environment (see modules/config.py):
    CLIQ_CLIENT_ID=...
    CLIQ_CLIENT_SECRET=...
    CLIQ_REFRESH_TOKEN=...   # self-client with ZohoCliq.Channels.READ + Chats.ALL
                             # + Messages.CREATE + Webhooks.CREATE. The /files
                             # endpoints are gated on Webhooks.CREATE - without it
                             # uploads 401 oauthtoken_scope_invalid while the
                             # message posts fine.
    CLIQ_LOCATION=in         # Zoho data center (in / com / eu / ...)

Design mirrors the storage layer: OAuth refresh-token flow, and every API error
surfaces Zoho's response body so a wrong endpoint/scope is easy to diagnose. The
message is posted first (best-documented call); each file upload is attempted
independently and failures are returned as warnings rather than aborting.
"""

from __future__ import annotations

import time

from . import config


def _cfg():
    return config.section("cliq")


def is_configured() -> bool:
    return _cfg() is not None


class CliqClient:
    def __init__(self, cfg: dict):
        self.client_id = cfg["client_id"]
        self.client_secret = cfg["client_secret"]
        self.refresh_token = cfg["refresh_token"]
        dc = str(cfg.get("location", "in")).strip().lstrip(".") or "in"
        self.accounts_host = cfg.get("accounts_domain") or f"https://accounts.zoho.{dc}"
        self.api_host = cfg.get("cliq_domain") or f"https://cliq.zoho.{dc}"
        self._token = None
        self._token_exp = 0.0

    def _access_token(self) -> str:
        import requests

        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = requests.post(
            f"{self.accounts_host}/oauth/v2/token",
            data={
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        # raise_for_status() would throw away Zoho's response body, which is the
        # only part that says *why* (invalid_client vs invalid_code vs a
        # mismatched client) - so surface it instead of a bare "400".
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Zoho token request failed (HTTP {resp.status_code}): {(resp.text or '')[:300]}")
        body = resp.json()
        if "access_token" not in body:
            raise RuntimeError(f"Cliq token error: {body}")
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}

    def resolve_chat_id(self, channel_unique_name: str) -> str:
        """A channel's messages/files are posted via its chat_id. Look it up
        from the channel's unique name (needs ZohoCliq.Channels.READ)."""
        import requests

        url = f"{self.api_host}/api/v2/channelsbyname/{channel_unique_name}"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Cliq channel lookup failed (HTTP {resp.status_code}): {(resp.text or '')[:400]}")
        body = resp.json()
        # Channel details are nested under "data" (e.g. {"type":"channel","data":{"chat_id":...}}).
        chat_id = None
        for container in (body.get("data"), body.get("channel"), body):
            if isinstance(container, dict) and container.get("chat_id"):
                chat_id = container["chat_id"]
                break
        if not chat_id:
            raise RuntimeError(
                f"No chat_id found for channel '{channel_unique_name}' (response: {str(body)[:200]})."
            )
        return chat_id

    def post_message(self, chat_id: str, text: str) -> None:
        import requests

        url = f"{self.api_host}/api/v2/chats/{chat_id}/message"
        resp = requests.post(url, headers=self._headers(), json={"text": text}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"Cliq message failed (HTTP {resp.status_code}): {(resp.text or '')[:400]}")

    def upload_file(self, chat_id: str, filename: str, data: bytes, mime: str) -> None:
        import requests

        url = f"{self.api_host}/api/v2/chats/{chat_id}/files"
        resp = requests.post(
            url, headers=self._headers(),
            files={"file": (filename, data, mime)}, timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Cliq upload '{filename}' failed (HTTP {resp.status_code}): {(resp.text or '')[:400]}")


def post_report(channel: str, files: list, message: str = "", cfg: dict | None = None) -> list:
    """channel = channel unique name. files = list of (filename, bytes, mime).
    Resolves the channel's chat_id, optionally posts a message (skipped when
    empty), then uploads each file. Returns a list of warning strings (empty =
    all succeeded). A fresh client is built each call so an updated
    refresh_token/secret takes effect immediately."""
    cfg = cfg or _cfg()
    if cfg is None:
        raise RuntimeError(
            "Zoho Cliq is not configured. Set CLIQ_CLIENT_ID / CLIQ_CLIENT_SECRET / CLIQ_REFRESH_TOKEN."
        )
    if not channel:
        raise RuntimeError("No Zoho Cliq channel is set for this project.")
    client = CliqClient(cfg)
    chat_id = client.resolve_chat_id(channel)
    if message:
        client.post_message(chat_id, message)
    warnings = []
    for filename, data, mime in files:
        try:
            client.upload_file(chat_id, filename, data, mime)
        except Exception as exc:
            warnings.append(str(exc))
    return warnings
